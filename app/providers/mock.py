"""API 키 없이 전체 파이프라인을 시험하기 위한 가짜 제공사.

키워드 규칙:
- "다이어트" 포함 → 첫 페이지가 비어 `screened` → 해시태그 경로로 수집 (FR-14)
- "소진" 또는 "exhaust" 포함 → 검색 결과 45개에서 끝남 → `search_exhausted` + 해시태그 보충 (FR-13)
- "410" 포함 → 주 제공사가 HTTP 410 반환 → 보조 제공사 전환 시험 (FR-15)
- 그 외 → 150개, 페이지당 12개(사진·캐러셀·중복 섞임)
영상 URL 은 이 서버의 /mock-cdn 을 가리키며 일부는 만료(403)·일시 오류(500)를 흉내낸다.
"""
from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timedelta

from ..config import settings
from ..services.usage import record_usage
from .base import MediaNotFound, ProviderGone, ReelItem, SearchPage, extract_hashtags

PAGE_SIZE = 12
WORDS = ["루틴", "브이로그", "꿀팁", "챌린지", "리뷰", "추천", "하루", "모음", "레시피", "비교"]
SONGS = [("Supernova", "aespa"), ("Magnetic", "ILLIT"), ("원본 오디오", None), ("APT.", "ROSÉ & Bruno Mars")]


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def mock_variant(media_pk: str) -> int:
    return int(media_pk) % 4


VARIANT_DURATION = {0: 3.0, 1: 4.0, 2: 5.0, 3: 6.0}


def mock_url(media_pk: str, kind: str, expired: bool = False) -> str:
    exp = int(time.time()) + (-60 if expired else 3 * 3600)
    ext = "mp4" if kind == "video" else "jpg"
    return f"{settings.public_base_url}/mock-cdn/{media_pk}.{ext}?exp={exp}"


class MockProvider:
    name = "mock"

    def __init__(self, label: str = "mock"):
        self.name = label

    def _total(self, keyword: str) -> int:
        if "소진" in keyword or "exhaust" in keyword.lower():
            return 45
        return 150

    def _make(self, keyword: str, idx: int, source: str) -> ReelItem:
        r = random.Random(_seed(keyword, source, idx))
        pk = str(3_000_000_000_000_000_000 + _seed(keyword, source, idx) % 10**15)
        kind = r.random()
        is_reel = kind > 0.08  # 약 8% 는 사진/캐러셀
        author = r.randint(1, 60)
        word = r.choice(WORDS)
        caption = f"{keyword} {word} #{keyword.replace(' ', '')} #{word} #릴스"
        song = r.choice(SONGS)
        code = hashlib.md5(pk.encode()).hexdigest()[:11]
        variant = int(pk) % 4
        return ReelItem(
            media_pk=pk,
            shortcode=f"M{code}",
            author_id=f"9000{author}",
            username=f"creator_{author:02d}",
            full_name=f"크리에이터 {author}",
            is_verified=author % 9 == 0,
            caption=caption,
            hashtags=extract_hashtags(caption),
            taken_at=datetime(2026, 11, 1) + timedelta(hours=r.randint(0, 500)),
            duration_sec=VARIANT_DURATION[variant] if is_reel else None,
            play_count=int(2_000_000 / (idx + 1) * r.uniform(0.6, 1.4)),
            like_count=int(80_000 / (idx + 1) * r.uniform(0.5, 1.5)),
            comment_count=r.randint(5, 2000),
            share_count=r.randint(0, 5000),
            music_title=song[0],
            music_artist=song[1],
            # 17번째마다 이미 만료된 URL 을 내려줘 재발급(FR-34) 경로를 시험
            video_url=mock_url(pk, "video", expired=(idx % 17 == 5)) if is_reel else None,
            thumbnail_url=mock_url(pk, "thumbnail"),
            source=source,
            is_reel=is_reel,
        )

    def search_reels(self, keyword: str, cursor: dict | None, want: int | None = None) -> SearchPage:
        ep = "/mock/search/reels"
        if "410" in keyword and self.name == "mock":
            record_usage(self.name, ep, 410, 0, 0)
            raise ProviderGone("mock 410 Gone", 410)
        time.sleep(0.2)
        page = (cursor or {}).get("page", 0)
        total = 0 if "다이어트" in keyword else self._total(keyword)
        start = page * PAGE_SIZE
        idxs = list(range(start, min(start + PAGE_SIZE, total)))
        if page > 0 and idxs:
            idxs.insert(2, start - 1)  # 이전 페이지 항목 중복 (FR-21 시험)
        items = [self._make(keyword, i, "reels_serp") for i in idxs]
        nxt = {"page": page + 1} if start + PAGE_SIZE < total else None
        record_usage(self.name, ep, 200, 1, 0)
        raw = {"mock": True, "page": page, "items": [i.__dict__ | {"taken_at": str(i.taken_at)} for i in items]}
        return SearchPage(items, nxt, raw, ep, len(items))

    def search_hashtag(self, tag: str, tab: str, cursor: dict | None) -> SearchPage:
        ep = f"/mock/hashtag/{tab}"
        time.sleep(0.2)
        page = (cursor or {}).get("page", 0)
        total = 60
        src = "hashtag_top" if tab == "top" else "hashtag_recent"
        start = page * PAGE_SIZE
        items = [self._make(tag, i, src) for i in range(start, min(start + PAGE_SIZE, total))]
        record_usage(self.name, ep, 200, 1, 0)
        nxt = {"page": page + 1} if start + PAGE_SIZE < total else None
        raw = {"mock": True, "tab": tab, "page": page, "count": len(items)}
        return SearchPage(items, nxt, raw, ep, len(items))

    def refresh_media(self, shortcode: str) -> ReelItem:
        from ..db import session_scope
        from ..models import Reel
        record_usage(self.name, "/mock/media/by/code", 200, 1, 0)
        with session_scope() as s:
            reel = s.query(Reel).filter_by(shortcode=shortcode).first()
            if reel is None:
                raise MediaNotFound(shortcode, 404)
            pk = reel.media_pk
        return ReelItem(media_pk=pk, shortcode=shortcode, author_id="", username="", caption=None, taken_at=None,
                        duration_sec=None, play_count=None, like_count=None, comment_count=None,
                        video_url=mock_url(pk, "video"), thumbnail_url=mock_url(pk, "thumbnail"), source="refresh")

    def health(self) -> dict:
        return {"ok": True, "detail": "mock provider"}
