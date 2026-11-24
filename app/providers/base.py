"""제공사 어댑터 인터페이스 (부록 B, NFR-06).

PRD 의 `search_reels(...) -> (items, cursor)` 를 확장해, 원본 응답 저장(FR-12)을 위해
`SearchPage`(items, next_cursor, raw, endpoint)를 돌려준다.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

# 현재 실행 id — api_usage 기록에 사용 (NFR-08)
current_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_run_id", default=None)


@dataclass
class ReelItem:
    media_pk: str
    shortcode: str
    author_id: str
    username: str
    caption: str | None
    taken_at: datetime | None
    duration_sec: float | None
    play_count: int | None
    like_count: int | None
    comment_count: int | None
    video_url: str | None  # 가장 높은 해상도 버전
    thumbnail_url: str | None
    source: str  # reels_serp | hashtag_top | hashtag_recent
    # --- 확장 필드 ---
    is_reel: bool = True  # FR-22: 사진·캐러셀은 False
    full_name: str | None = None
    is_verified: bool | None = None
    share_count: int | None = None
    music_title: str | None = None
    music_artist: str | None = None
    hashtags: list[str] = field(default_factory=list)


@dataclass
class SearchPage:
    items: list[ReelItem]
    next_cursor: dict | None
    raw: Any
    endpoint: str
    raw_item_count: int = 0  # 필터링 전 원본 항목 수 (차단 키워드 판별용)


class ProviderError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class ProviderUnavailable(ProviderError):
    """장애·인증 실패 등. 보조 제공사로 전환 대상 (FR-15)."""


class ProviderGone(ProviderUnavailable):
    """HTTP 410 — 엔드포인트 폐기."""


class MediaNotFound(ProviderError):
    """원본 삭제·비공개 (FR-62)."""


class NotSupported(ProviderError):
    pass


UsageRecorder = Callable[[str, str, int | None, float, float], None]  # provider, endpoint, status, units, cost


class SearchProvider(Protocol):
    name: str

    def search_reels(self, keyword: str, cursor: dict | None, want: int | None = None) -> SearchPage: ...

    def search_hashtag(self, tag: str, tab: str, cursor: dict | None) -> SearchPage: ...

    def refresh_media(self, shortcode: str) -> ReelItem: ...  # 만료된 영상 URL 재발급

    def health(self) -> dict: ...


# ---------------------------------------------------------------- 공통 파서
HASHTAG_RE = re.compile(r"#([\w가-힣]+)", re.UNICODE)


def extract_hashtags(text: str | None) -> list[str]:
    return list(dict.fromkeys(HASHTAG_RE.findall(text or "")))


def _to_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return datetime.fromtimestamp(int(v), tz=timezone.utc).replace(tzinfo=None)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OSError):
        return None


def _best(versions: list | None) -> str | None:
    if not versions:
        return None
    best = max(versions, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
    return best.get("url")


def looks_like_media(d: dict) -> bool:
    return isinstance(d, dict) and "code" in d and ("pk" in d or "id" in d) and (
        "media_type" in d or "video_versions" in d or "image_versions2" in d
    )


def find_media_dicts(obj: Any) -> list[dict]:
    """응답 구조가 버전마다 달라 재귀로 미디어 객체를 순서대로 찾는다. 캐러셀 내부로는 내려가지 않는다."""
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if looks_like_media(o):
                out.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def parse_ig_media(m: dict, source: str) -> ReelItem:
    """인스타그램 private/GQL 미디어 객체 → ReelItem."""
    user = m.get("user") or m.get("owner") or {}
    cap = m.get("caption")
    caption = cap.get("text") if isinstance(cap, dict) else cap
    media_type = _to_int(m.get("media_type"))
    product_type = m.get("product_type")
    if product_type:
        is_reel = product_type == "clips" and media_type in (None, 2)
    else:
        is_reel = media_type == 2 or bool(m.get("video_versions"))

    music_title = music_artist = None
    clips = m.get("clips_metadata") or {}
    mi = (clips.get("music_info") or {}).get("music_asset_info") or {}
    if mi:
        music_title, music_artist = mi.get("title"), mi.get("display_artist")
    else:
        osi = clips.get("original_sound_info") or {}
        if osi:
            music_title = osi.get("original_audio_title")
            music_artist = (osi.get("ig_artist") or {}).get("username")

    pk = str(m.get("pk") or str(m.get("id", "")).split("_")[0])
    return ReelItem(
        media_pk=pk,
        shortcode=m.get("code"),
        author_id=str(user.get("pk") or user.get("id") or ""),
        username=user.get("username") or "",
        full_name=user.get("full_name"),
        is_verified=user.get("is_verified"),
        caption=caption,
        hashtags=extract_hashtags(caption),
        taken_at=_to_dt(m.get("taken_at")),
        duration_sec=m.get("video_duration"),
        play_count=_to_int(m.get("play_count") or m.get("ig_play_count") or m.get("view_count")),
        like_count=_to_int(m.get("like_count")),
        comment_count=_to_int(m.get("comment_count")),
        share_count=_to_int(m.get("reshare_count") or m.get("share_count")),
        music_title=music_title,
        music_artist=music_artist,
        video_url=_best(m.get("video_versions")) or m.get("video_url"),
        thumbnail_url=_best((m.get("image_versions2") or {}).get("candidates")) or m.get("thumbnail_url"),
        source=source,
        is_reel=is_reel,
    )


def find_key(obj: Any, key: str):
    """중첩 응답에서 첫 번째로 나오는 key 값을 찾는다."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None
