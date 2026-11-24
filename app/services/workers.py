"""로컬용 작업 큐: Redis/Celery 대신 프로세스 내 스레드 풀 2개(검색·다운로드 분리, NFR-02)."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import or_

from ..config import settings
from ..db import session_scope, utcnow
from ..models import CollectionRun, MediaFile

log = logging.getLogger("workers")

search_pool = ThreadPoolExecutor(max_workers=settings.search_workers, thread_name_prefix="search")
download_pool = ThreadPoolExecutor(max_workers=settings.download_workers, thread_name_prefix="download")
_inflight: set[int] = set()
_lock = threading.Lock()


def _run_download(file_id: int):
    from .downloader import download_file
    try:
        download_file(file_id)
    except Exception:  # noqa: BLE001
        log.exception("download %s crashed", file_id)
    finally:
        with _lock:
            _inflight.discard(file_id)


def enqueue_downloads(file_ids: list[int]) -> None:
    with _lock:
        new = [i for i in file_ids if i not in _inflight]
        _inflight.update(new)
    for i in new:
        download_pool.submit(_run_download, i)


def _run_search(run_id: int):
    from .collector import execute_run
    try:
        execute_run(run_id)
    except Exception:  # noqa: BLE001
        log.exception("run %s crashed", run_id)


def enqueue_run(run_id: int) -> None:
    search_pool.submit(_run_search, run_id)


def recover_on_startup() -> None:
    """서버 재시작 시 중단된 작업 복구: 받던 중이던/대기 중이던 파일을 한 번 다시 큐에 넣는다."""
    now = utcnow()
    with session_scope() as s:
        s.query(MediaFile).filter(MediaFile.status.in_(("downloading", "expired_url"))).update(
            {"status": "pending"}, synchronize_session=False)
        pending = [i for (i,) in s.query(MediaFile.id).filter(MediaFile.status == "pending")]
        for run in s.query(CollectionRun).filter(CollectionRun.status == "searching"):
            run.error = (run.error or "") + "서버 재시작으로 검색 중단"
            run.status = "downloading" if run.found_count else "failed"
            if run.status == "failed":
                run.finished_at = now
        queued = [r.id for r in s.query(CollectionRun).filter(CollectionRun.status == "queued")]
    enqueue_downloads(pending)
    for rid in queued:
        enqueue_run(rid)


def has_active_run(s, keyword_id: int) -> bool:
    return s.query(CollectionRun.id).filter(
        CollectionRun.keyword_id == keyword_id,
        or_(CollectionRun.status == "queued", CollectionRun.status == "searching"),
    ).first() is not None


def shutdown():
    search_pool.shutdown(wait=False, cancel_futures=True)
    download_pool.shutdown(wait=False, cancel_futures=True)
