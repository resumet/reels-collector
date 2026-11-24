"""Apify memo23/instagram-reels-search-scraper 어댑터 (보조 제공사).

입력: queries[], maxResultsPerQuery, contentType=reels_only
출력: mediaPk, shortCode, videoUrl, thumbnailUrl, caption, playCount, likeCount, commentCount, shareCount,
      videoDuration, takenAt, username, userId, userFullName, userIsVerified, musicTitle, musicArtist, contentKind
커서 기반이 아니므로 한 번에 원하는 수량을 받고 next_cursor=None 을 돌려준다.
무료 플랜은 실행당 처음 3개만 영상 링크를 준다(유료 플랜 필요).
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..services.usage import record_usage
from .base import (
    NotSupported, ProviderError, ProviderGone, ProviderUnavailable, ReelItem, SearchPage, _to_dt, _to_int,
    extract_hashtags,
)

API = "https://api.apify.com/v2"


class ApifyProvider:
    name = "apify"

    def __init__(self):
        if not settings.apify_token:
            raise ProviderUnavailable("APIFY_TOKEN 이 설정되지 않았습니다")

    def _run(self, payload: dict) -> list[dict]:
        ep = f"/acts/{settings.apify_actor}/run-sync-get-dataset-items"
        status = None
        items: list[dict] = []
        try:
            r = httpx.post(f"{API}{ep}", params={"token": settings.apify_token, "timeout": 600},
                           json=payload, timeout=httpx.Timeout(660, connect=20))
            status = r.status_code
            if status == 410:
                raise ProviderGone("Apify actor 410", 410)
            if status in (401, 402, 403) or status >= 500:
                raise ProviderUnavailable(f"Apify HTTP {status}: {r.text[:200]}", status)
            if status >= 400:
                raise ProviderError(f"Apify HTTP {status}: {r.text[:200]}", status)
            items = r.json() if isinstance(r.json(), list) else []
            return items
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"Apify 호출 실패: {e}") from e
        finally:
            n = len(items)
            record_usage(self.name, ep, status, n, n * settings.apify_cost_per_item)

    @staticmethod
    def _parse(d: dict, source: str) -> ReelItem:
        caption = d.get("caption")
        return ReelItem(
            media_pk=str(d.get("mediaPk") or ""),
            shortcode=d.get("shortCode") or "",
            author_id=str(d.get("userId") or ""),
            username=d.get("username") or "",
            full_name=d.get("userFullName"),
            is_verified=d.get("userIsVerified"),
            caption=caption,
            hashtags=d.get("hashtags") or extract_hashtags(caption),
            taken_at=_to_dt(d.get("takenAt")),
            duration_sec=d.get("videoDuration"),
            play_count=_to_int(d.get("playCount")),
            like_count=_to_int(d.get("likeCount")),
            comment_count=_to_int(d.get("commentCount")),
            share_count=_to_int(d.get("shareCount")),
            music_title=d.get("musicTitle"),
            music_artist=d.get("musicArtist"),
            video_url=d.get("videoUrl") or d.get("downloadUrl"),
            thumbnail_url=d.get("thumbnailUrl"),
            source=source,
            is_reel=(d.get("contentKind") or "reel") == "reel",
        )

    def search_reels(self, keyword: str, cursor: dict | None, want: int | None = None) -> SearchPage:
        if cursor:  # 한 번에 모두 받으므로 다음 페이지 없음
            return SearchPage([], None, [], "apify", 0)
        payload = {"queries": [keyword], "maxResultsPerQuery": max(want or 100, 1), "contentType": "reels_only"}
        rows = self._run(payload)
        items = [self._parse(d, "reels_serp") for d in rows if d.get("mediaPk") and d.get("shortCode")]
        return SearchPage(items, None, rows, f"apify:{settings.apify_actor}", len(rows))

    def search_hashtag(self, tag: str, tab: str, cursor: dict | None) -> SearchPage:
        if cursor:
            return SearchPage([], None, [], "apify", 0)
        payload = {"queries": [f"#{tag}"], "maxResultsPerQuery": 100, "contentType": "reels_only",
                   "hashtagFeedType": tab}
        rows = self._run(payload)
        src = "hashtag_top" if tab == "top" else "hashtag_recent"
        items = [self._parse(d, src) for d in rows if d.get("mediaPk") and d.get("shortCode")]
        return SearchPage(items, None, rows, f"apify:{settings.apify_actor}#{tab}", len(rows))

    def refresh_media(self, shortcode: str) -> ReelItem:
        raise NotSupported("Apify 어댑터는 단건 URL 재발급을 지원하지 않습니다")

    def health(self) -> dict:
        try:
            r = httpx.get(f"{API}/acts/{settings.apify_actor}", params={"token": settings.apify_token}, timeout=20)
            return {"ok": r.status_code == 200, "detail": f"actor 조회 HTTP {r.status_code}"}
        except httpx.HTTPError as e:
            return {"ok": False, "detail": str(e)}
