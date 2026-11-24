"""검색 워커: 페이지 이동, 목표 수량에서 중단, 정규화·중복 제거, 페이지마다 다운로드 등록."""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from datetime import timedelta

from sqlalchemy import func, select

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Author, CollectionRun, Keyword, MediaFile, RawPage, Reel, RunResult
from ..providers import get_provider
from ..providers.base import (
    NotSupported, ProviderError, ProviderUnavailable, ReelItem, SearchPage, current_run_id,
)

log = logging.getLogger("collector")
_persist_lock = threading.Lock()  # SQLite 단일 작성자 + 동시 upsert 경합 방지

ACTIVE_STATUSES = ("queued", "searching", "downloading")


class RunTimeout(Exception):
    pass


def _jsonable(o):
    import json
    return json.loads(json.dumps(o, default=str))


def _snapshot(it: ReelItem) -> dict:
    return {"play_count": it.play_count, "like_count": it.like_count, "comment_count": it.comment_count,
            "share_count": it.share_count, "captured_at": utcnow().isoformat()}


def persist_page(run_id: int, page_no: int, page: SearchPage, cursor_in: dict | None,
                 seen: set[str], target: int) -> tuple[list[int], int]:
    """원본 저장(FR-12) + 정규화 저장(FR-20~23). 새로 등록할 다운로드 파일 id 와 이번 페이지의 신규 개수 반환."""
    to_download: list[int] = []
    added = 0
    with _persist_lock, session_scope() as s:
        s.add(RawPage(run_id=run_id, page_no=page_no, endpoint=page.endpoint, cursor_in=cursor_in,
                      payload=_jsonable(page.raw)))
        rank = len(seen)
        now = utcnow()
        for it in page.items:
            if not it.is_reel or not it.media_pk or not it.shortcode:  # FR-22
                continue
            if it.media_pk in seen:  # FR-21 같은 실행 안 중복: 먼저 나온 순위 유지
                continue
            if rank >= target:
                break
            # 작성자
            if it.author_id:
                a = s.get(Author, it.author_id)
                if a is None:
                    s.add(Author(ig_user_id=it.author_id, username=it.username or it.author_id,
                                 full_name=it.full_name, is_verified=it.is_verified))
                else:
                    a.username = it.username or a.username
                    a.full_name = it.full_name or a.full_name
                    a.is_verified = it.is_verified if it.is_verified is not None else a.is_verified
                    a.updated_at = now
                s.flush()
            # 릴스: 최신 지표로 갱신 (FR-23, NFR-05)
            r = s.get(Reel, it.media_pk)
            if r is None:
                r = Reel(media_pk=it.media_pk, shortcode=it.shortcode, first_seen_at=now)
                s.add(r)
            r.author_id = it.author_id or r.author_id
            r.caption = it.caption if it.caption is not None else r.caption
            r.hashtags = it.hashtags or r.hashtags
            r.taken_at = it.taken_at or r.taken_at
            r.duration_sec = it.duration_sec if it.duration_sec is not None else r.duration_sec
            for f in ("play_count", "like_count", "comment_count", "share_count", "music_title", "music_artist"):
                v = getattr(it, f)
                if v is not None:
                    setattr(r, f, v)
            r.is_available = True
            r.last_seen_at = now
            s.flush()

            rank += 1
            seen.add(it.media_pk)
            added += 1
            s.add(RunResult(run_id=run_id, media_pk=it.media_pk, rank=rank, page_no=page_no, source=it.source,
                            metrics_snapshot=_snapshot(it)))
            # 다운로드 작업 (FR-30, FR-33)
            for kind, url in (("video", it.video_url), ("thumbnail", it.thumbnail_url)):
                mf = s.query(MediaFile).filter_by(media_pk=it.media_pk, kind=kind).first()
                if mf is None:
                    mf = MediaFile(media_pk=it.media_pk, kind=kind, status="pending")
                    s.add(mf)
                elif mf.status in ("stored", "downloading"):
                    continue  # 이미 저장됨/진행 중 → 다시 받지 않음
                # failed/deleted 는 이번 실행의 새 URL 로 한 번 더 받는다
                mf.provider = _provider_of(s, run_id)
                mf.source_url = url
                mf.url_fetched_at = now
                mf.status = "pending"
                mf.attempts = 0
                mf.refresh_count = 0
                mf.last_error = None
                mf.next_attempt_at = now
                s.flush()
                to_download.append(mf.id)
        run = s.get(CollectionRun, run_id)
        run.found_count = len(seen)
    return to_download, added


def _provider_of(s, run_id: int) -> str:
    return s.get(CollectionRun, run_id).provider


def _sleep_between_pages():
    time.sleep(random.uniform(settings.page_delay_min, settings.page_delay_max))  # NFR-03


def _update_run(run_id: int, **values):
    with session_scope() as s:
        run = s.get(CollectionRun, run_id)
        for k, v in values.items():
            setattr(run, k, v)


def execute_run(run_id: int) -> None:
    from .workers import enqueue_downloads

    token = current_run_id.set(run_id)
    try:
        with session_scope() as s:
            run = s.get(CollectionRun, run_id)
            if run is None or run.status != "queued":
                return
            kw = s.get(Keyword, run.keyword_id)
            keyword, target = kw.keyword, kw.target_count
            provider_name = run.provider
            now = utcnow()
            run.status = "searching"
            run.started_at = now
            run.deadline_at = now + timedelta(minutes=settings.run_timeout_min)
            deadline = run.deadline_at

        seen: set[str] = set()
        page_no = 0
        notes: list[str] = []
        exhausted = screened = False
        switched_from = None

        def check_deadline():
            if utcnow() >= deadline:
                raise RunTimeout()

        # ---------------- 1) 키워드 릴스 검색 (FR-10, FR-11)
        provider = get_provider(provider_name)
        cursor: dict | None = None
        provider_pages = 0
        while len(seen) < target and page_no < settings.max_pages:
            check_deadline()
            try:
                page = provider.search_reels(keyword, cursor, want=target - len(seen))
            except ProviderUnavailable as e:  # 장애·410 → 보조 제공사 (FR-15)
                fb = settings.fallback_provider
                if fb and switched_from is None and fb != provider_name:
                    log.warning("run %s: %s 장애(%s) → %s 전환", run_id, provider_name, e, fb)
                    switched_from = provider_name
                    notes.append(f"{provider_name} 장애로 {fb} 전환: {e}")
                    provider_name = fb
                    provider = get_provider(fb)
                    _update_run(run_id, provider=fb, provider_switched_from=switched_from)
                    cursor, provider_pages = None, 0
                    continue
                raise
            page_no += 1
            provider_pages += 1
            ids, _ = persist_page(run_id, page_no, page, cursor, seen, target)
            enqueue_downloads(ids)
            if provider_pages == 1 and page.raw_item_count == 0 and not seen:  # FR-14
                screened = True
                notes.append("첫 페이지가 비어 민감 키워드 차단(screened)으로 판단")
                break
            cursor = page.next_cursor
            if not cursor:
                if len(seen) < target:
                    exhausted = True  # FR-13
                break
            if len(seen) < target:
                _sleep_between_pages()
        if len(seen) < target and page_no >= settings.max_pages:
            notes.append(f"페이지 상한 {settings.max_pages} 도달")

        _update_run(run_id, search_exhausted=exhausted, screened=screened)

        # ---------------- 2) 해시태그 보충 (FR-13, FR-14)
        if len(seen) < target and (exhausted or screened):
            tag = keyword.replace("#", "").replace(" ", "")
            for tab in ("top", "recent"):
                cursor, tab_pages = None, 0
                while len(seen) < target and tab_pages < settings.max_pages:
                    check_deadline()
                    try:
                        page = provider.search_hashtag(tag, tab, cursor)
                    except NotSupported as e:
                        notes.append(f"해시태그 {tab}: {e}")
                        break
                    except ProviderError as e:
                        notes.append(f"해시태그 {tab} 실패: {e}")
                        break
                    page_no += 1
                    tab_pages += 1
                    ids, _ = persist_page(run_id, page_no, page, cursor, seen, target)
                    enqueue_downloads(ids)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    _sleep_between_pages()

        _update_run(run_id, status="downloading", found_count=len(seen),
                    note="\n".join(notes) or None)
    except RunTimeout:
        _update_run(run_id, status="downloading", note="검색 단계 시간 초과(15분)")
    except Exception as e:  # noqa: BLE001
        log.exception("run %s failed", run_id)
        with session_scope() as s:
            run = s.get(CollectionRun, run_id)
            run.error = f"{type(e).__name__}: {e}"
            if run.found_count > 0:
                run.status = "downloading"  # 이미 찾은 것은 받고 partial 로 끝냄
            else:
                run.status = "failed"
                run.finished_at = utcnow()
    finally:
        current_run_id.reset(token)


# ---------------------------------------------------------------- 진행 집계·종료 판정
def run_progress(s, run_id: int) -> dict:
    q = (select(MediaFile.status, func.count())
         .join(RunResult, RunResult.media_pk == MediaFile.media_pk)
         .where(RunResult.run_id == run_id, MediaFile.kind == "video")
         .group_by(MediaFile.status))
    counts = {"stored": 0, "pending": 0, "downloading": 0, "failed": 0, "deleted": 0}
    for status, c in s.execute(q):
        key = "downloading" if status == "expired_url" else status
        counts[key] = counts.get(key, 0) + c
    return counts


def finalize_runs() -> None:
    """다운로드 단계 실행의 종료 판정 (NFR-01, NFR-04)."""
    now = utcnow()
    with session_scope() as s:
        runs = s.query(CollectionRun).filter(CollectionRun.status.in_(("searching", "downloading"))).all()
        for run in runs:
            p = run_progress(s, run.id)
            run.downloaded_count = p["stored"]
            if run.status != "downloading":
                continue
            outstanding = p["pending"] + p["downloading"]
            timed_out = run.deadline_at is not None and now >= run.deadline_at
            if outstanding and not timed_out:
                continue
            kw = s.get(Keyword, run.keyword_id)
            need = math.ceil(kw.target_count * settings.done_download_ratio)
            if run.found_count >= kw.target_count and run.downloaded_count >= need:
                run.status = "done"
            elif run.found_count == 0:
                run.status = "failed"
                run.error = run.error or "수집된 릴스 없음"
            else:
                run.status = "partial"
                if timed_out and outstanding:
                    run.note = ((run.note or "") + f"\n시간 초과: 다운로드 {outstanding}건 미완료").strip()
            run.finished_at = now
