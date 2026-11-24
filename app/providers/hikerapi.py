"""HikerAPI 어댑터 (주 제공사).

2026-11-24 OpenAPI(v1.8.4) 확인 결과:
- /v2/fbsearch/reels, /v3/fbsearch/reels : deprecated, "Closed by Instagram since 2026-11-13"
  → 대안 /gql/topsearch (query, end_cursor, flat). 기본값을 /gql/topsearch 로 둔다.
- /v2/hashtag/medias/top|recent : deprecated. 인기 탭 대안은 /g2/hashtag/medias (name, page_id),
  최신 탭은 대안 없음.
- 단건 조회: /v2/media/info/by/code (code) — 광고 등 404 시 /v1/media/by/code
응답 필드명은 PoC 대상이라 파서는 미디어 객체를 재귀로 찾는다.
"""
from __future__ import annotations

import time

import httpx

from ..config import settings
from ..services.usage import record_usage
from .base import (
    MediaNotFound, NotSupported, ProviderError, ProviderGone, ProviderUnavailable, ReelItem, SearchPage,
    find_key, find_media_dicts, parse_ig_media,
)


class HikerApiProvider:
    name = "hikerapi"

    def __init__(self):
        if not settings.hikerapi_key:
            raise ProviderUnavailable("HIKERAPI_KEY 가 설정되지 않았습니다")
        self.client = httpx.Client(
            base_url=settings.hikerapi_base_url,
            headers={"x-access-key": settings.hikerapi_key, "accept": "application/json"},
            timeout=httpx.Timeout(60, connect=15),
        )

    # ------------------------------------------------------------ HTTP
    def _get(self, path: str, params: dict) -> dict | list:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        last_exc: Exception | None = None
        for attempt in range(3):
            status = None
            try:
                r = self.client.get(path, params=params)
                status = r.status_code
            except httpx.HTTPError as e:
                last_exc = e
            finally:
                billed = 1 if status and status < 400 else 0
                record_usage(self.name, path, status, billed, billed * settings.hikerapi_cost_per_call)
            if status is None:
                time.sleep(2 * (attempt + 1))
                continue
            if status == 410:
                raise ProviderGone(f"{path} 410 Gone (엔드포인트 폐기)", 410)
            if status == 404:
                raise MediaNotFound(f"{path} 404", 404)
            if status in (401, 402, 403):
                raise ProviderUnavailable(f"{path} 인증/결제 오류 HTTP {status}: {r.text[:200]}", status)
            if status == 429 or status >= 500:
                last_exc = ProviderUnavailable(f"{path} HTTP {status}", status)
                time.sleep(3 * (attempt + 1))
                continue
            if status >= 400:
                raise ProviderError(f"{path} HTTP {status}: {r.text[:200]}", status)
            return r.json()
        raise ProviderUnavailable(f"{path} 재시도 실패: {last_exc}")

    # ------------------------------------------------------------ 검색
    def search_reels(self, keyword: str, cursor: dict | None, want: int | None = None) -> SearchPage:
        ep = settings.hikerapi_search_endpoint
        cursor = cursor or {}
        if "fbsearch" in ep:
            data = self._get(ep, {"query": keyword, "reels_max_id": cursor.get("reels_max_id"),
                                  "rank_token": cursor.get("rank_token")})
            nxt = None
            max_id = find_key(data, "reels_max_id")
            has_more = find_key(data, "has_more")
            if max_id and has_more is not False:
                nxt = {"reels_max_id": max_id, "rank_token": find_key(data, "rank_token") or cursor.get("rank_token")}
        else:  # /gql/topsearch
            data = self._get(ep, {"query": keyword, "end_cursor": cursor.get("end_cursor"), "flat": "true"})
            end = find_key(data, "end_cursor")
            has_next = find_key(data, "has_next_page")
            nxt = {"end_cursor": end} if end and has_next is not False and end != cursor.get("end_cursor") else None
        medias = find_media_dicts(data)
        return SearchPage(items=[parse_ig_media(m, "reels_serp") for m in medias], next_cursor=nxt,
                          raw=data, endpoint=ep, raw_item_count=len(medias))

    def search_hashtag(self, tag: str, tab: str, cursor: dict | None) -> SearchPage:
        if tab != "top":
            raise NotSupported("HikerAPI 해시태그 최신 탭은 2026-11-10 폐기, 대체 경로 없음")
        ep = "/g2/hashtag/medias"
        data = self._get(ep, {"name": tag, "page_id": (cursor or {}).get("page_id")})
        nxt_id = find_key(data, "next_page_id")
        medias = find_media_dicts(data)
        return SearchPage(items=[parse_ig_media(m, "hashtag_top") for m in medias],
                          next_cursor={"page_id": nxt_id} if nxt_id else None,
                          raw=data, endpoint=ep, raw_item_count=len(medias))

    # ------------------------------------------------------------ URL 재발급
    def refresh_media(self, shortcode: str) -> ReelItem:
        try:
            data = self._get("/v2/media/info/by/code", {"code": shortcode})
        except MediaNotFound:
            data = self._get("/v1/media/by/code", {"code": shortcode})
        medias = find_media_dicts(data)
        if not medias:
            raise MediaNotFound(f"{shortcode} 미디어 정보 없음")
        return parse_ig_media(medias[0], "refresh")

    # ------------------------------------------------------------ 헬스체크 (리스크 대응: 시작 시 OpenAPI 확인)
    def health(self) -> dict:
        try:
            r = httpx.get(f"{settings.hikerapi_base_url}/openapi.json", timeout=30)
            spec = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"OpenAPI 조회 실패: {e}"}
        paths = spec.get("paths", {})
        ep = settings.hikerapi_search_endpoint
        op = (paths.get(ep) or {}).get("get")
        if not op:
            return {"ok": False, "detail": f"{ep} 경로가 OpenAPI 에 없음"}
        if op.get("deprecated"):
            return {"ok": False, "detail": f"{ep} deprecated: {op.get('description') or op.get('summary')}"}
        return {"ok": True, "detail": f"{ep} 사용 가능 (spec {spec.get('info', {}).get('version')})"}
