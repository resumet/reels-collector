from __future__ import annotations

import base64
import logging
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .api import router
from .config import BASE_DIR, settings
from .db import init_db
from .services import workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    workers.recover_on_startup()
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)
    workers.shutdown()


app = FastAPI(title="Reels Collector", lifespan=lifespan)


# NFR-07: APP_USER/APP_PASSWORD 설정 시 HTTP Basic 인증
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if settings.app_user and settings.app_password and not request.url.path.startswith("/mock-cdn/"):
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, settings.app_user) and secrets.compare_digest(p, settings.app_password)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="reels"'})
    return await call_next(request)


app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------ Mock CDN (PRIMARY_PROVIDER=mock 전용)
MOCK_DIR = BASE_DIR / "data" / "mock_cdn"
_mock_lock = threading.Lock()
_fail_once: set[str] = set()
COLORS = ["0x4f46e5", "0x059669", "0xdc2626", "0xd97706"]


def _mock_asset(variant: int, ext: str) -> Path:
    from .providers.mock import VARIANT_DURATION
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    mp4 = MOCK_DIR / f"v{variant}.mp4"
    jpg = MOCK_DIR / f"v{variant}.jpg"
    with _mock_lock:
        ff = shutil.which("ffmpeg")
        if not mp4.exists():
            if not ff:
                raise RuntimeError("ffmpeg 가 필요합니다")
            dur = VARIANT_DURATION[variant]
            subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i",
                            f"testsrc2=size=360x640:rate=24:duration={dur}", "-f", "lavfi", "-i",
                            f"sine=frequency={440 + 110 * variant}:duration={dur}",
                            "-vf", f"drawbox=x=0:y=0:w=iw:h=60:color={COLORS[variant]}@0.8:t=fill",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                            "-movflags", "+faststart", str(mp4)], check=True)
        if not jpg.exists():
            subprocess.run([ff, "-y", "-v", "error", "-ss", "1", "-i", str(mp4), "-frames:v", "1", str(jpg)],
                           check=True)
    return mp4 if ext == "mp4" else jpg


@app.get("/mock-cdn/{name}", include_in_schema=False)
def mock_cdn(name: str, exp: int = 0):
    if settings.primary_provider != "mock" and not settings.fallback_provider.startswith("mock"):
        return JSONResponse({"detail": "mock disabled"}, status_code=404)
    stem, _, ext = name.partition(".")
    if not stem.isdigit() or ext not in ("mp4", "jpg"):
        return JSONResponse({"detail": "bad name"}, status_code=404)
    if exp and exp < time.time():
        return JSONResponse({"detail": "URL signature expired"}, status_code=403)  # FR-34 시험
    if ext == "mp4" and int(stem) % 50 == 0 and stem not in _fail_once:  # 실패 처리 시험: 첫 요청 500 (재시도 없음 → 실패로 남음)
        _fail_once.add(stem)
        return JSONResponse({"detail": "temporary error"}, status_code=500)
    return FileResponse(_mock_asset(int(stem) % 4, ext), media_type="video/mp4" if ext == "mp4" else "image/jpeg")
