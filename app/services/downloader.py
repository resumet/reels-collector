"""다운로드 워커 (FR-30~34, 36): 스트리밍 + SHA-256, ffprobe 검증, URL 만료 시 재발급.

실패한 다운로드는 재시도하지 않는다 (FR-35 제외). 같은 키워드를 다시 실행하면 실패한 파일을 새 URL 로 다시 받는다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import update

from ..config import settings
from ..db import session_scope, utcnow
from ..models import MediaFile, Reel
from ..providers import refresh_capable
from ..providers.base import MediaNotFound, NotSupported, ProviderError

log = logging.getLogger("downloader")
MAX_REFRESH = 3
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Mobile Safari/537.36"
FFPROBE = shutil.which("ffprobe")


class ExpiredUrl(Exception):
    pass


class InvalidMedia(Exception):
    pass


def storage_key(media_pk: str, kind: str) -> str:
    return f"reels/{media_pk}/{'video.mp4' if kind == 'video' else 'thumbnail.jpg'}"  # FR-36


def storage_path(key: str) -> Path:
    return settings.storage_dir / key


def claim(file_id: int) -> bool:
    with session_scope() as s:
        res = s.execute(update(MediaFile).where(MediaFile.id == file_id, MediaFile.status == "pending")
                        .values(status="downloading", next_attempt_at=None))
        return res.rowcount == 1


def ffprobe(path: Path) -> dict:
    if not FFPROBE:
        raise InvalidMedia("ffprobe 를 찾을 수 없습니다")
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise InvalidMedia(f"ffprobe 실패: {out.stderr.strip()[:200]}")
    info = json.loads(out.stdout or "{}")
    v = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), None)
    if not v:
        raise InvalidMedia("영상 스트림 없음")
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise InvalidMedia("재생 길이 0")
    return {"width": v.get("width"), "height": v.get("height"), "codec": v.get("codec_name"), "duration_sec": dur}


def _refresh_url(provider_name: str | None, kind: str, shortcode: str) -> str:
    """FR-34: 미디어 정보 조회로 URL 재발급."""
    last: Exception | None = None
    for provider in refresh_capable(provider_name):
        try:
            item = provider.refresh_media(shortcode)
        except NotSupported as e:
            last = e
            continue
        url = item.video_url if kind == "video" else item.thumbnail_url
        if url:
            return url
        last = ProviderError("재발급 응답에 URL 없음")
    raise last or ProviderError("URL 재발급 가능한 제공사 없음")


def _fetch(url: str, dest: Path) -> tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    size = 0
    with httpx.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=True,
                      timeout=httpx.Timeout(60, connect=15)) as r:
        if r.status_code in (403, 410):
            raise ExpiredUrl(f"HTTP {r.status_code}")
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)
                h.update(chunk)
                size += len(chunk)
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise InvalidMedia("빈 파일")
    os.replace(tmp, dest)
    return h.hexdigest(), size


def download_file(file_id: int) -> None:
    if not claim(file_id):
        return
    with session_scope() as s:
        f = s.get(MediaFile, file_id)
        reel = s.get(Reel, f.media_pk)
        shortcode = reel.shortcode
        f.attempts += 1
        snap = {c: getattr(f, c) for c in ("kind", "source_url", "url_fetched_at", "provider", "attempts",
                                           "refresh_count", "media_pk")}
    kind, url = snap["kind"], snap["source_url"]
    key = storage_key(snap["media_pk"], kind)
    dest = storage_path(key)
    try:
        stale = (not url or snap["url_fetched_at"] is None
                 or utcnow() - snap["url_fetched_at"] > timedelta(hours=settings.url_ttl_hours))
        for _ in range(MAX_REFRESH + 1):
            if stale:
                with session_scope() as s:
                    f = s.get(MediaFile, file_id)
                    if f.refresh_count >= MAX_REFRESH:
                        raise ProviderError("URL 재발급 한도 초과")
                    f.refresh_count += 1
                url = _refresh_url(snap["provider"], kind, shortcode)
                with session_scope() as s:
                    f = s.get(MediaFile, file_id)
                    f.source_url, f.url_fetched_at = url, utcnow()
            try:
                sha, size = _fetch(url, dest)
                break
            except ExpiredUrl:
                stale = True
        else:
            raise ProviderError("URL 재발급 후에도 만료")

        meta = ffprobe(dest) if kind == "video" else {}
        now = utcnow()
        with session_scope() as s:
            f = s.get(MediaFile, file_id)
            f.status = "stored"
            f.storage_key = key
            f.sha256, f.bytes = sha, size
            f.width, f.height = meta.get("width"), meta.get("height")
            f.codec, f.duration_sec = meta.get("codec"), meta.get("duration_sec")
            f.stored_at = now
            f.delete_after = now + timedelta(days=settings.video_retention_days)  # FR-60
            f.last_error = None
            f.next_attempt_at = None
    except MediaNotFound as e:  # FR-62
        with session_scope() as s:
            f = s.get(MediaFile, file_id)
            f.status, f.last_error, f.next_attempt_at = "failed", f"원본 삭제·비공개: {e}", None
            r = s.get(Reel, f.media_pk)
            r.is_available = False
    except Exception as e:  # noqa: BLE001  재시도 없음: 실패는 곧 최종 실패
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
        if isinstance(e, InvalidMedia):
            dest.unlink(missing_ok=True)
        with session_scope() as s:
            f = s.get(MediaFile, file_id)
            f.status = "failed"
            f.last_error = f"{type(e).__name__}: {e}"[:500]
            f.next_attempt_at = None
        log.info("download %s failed (%s)", file_id, e)
