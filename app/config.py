from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name) or default)


def _i(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    primary_provider: str = _s("PRIMARY_PROVIDER", "mock")
    fallback_provider: str = _s("FALLBACK_PROVIDER", "")

    hikerapi_key: str = _s("HIKERAPI_KEY")
    hikerapi_base_url: str = _s("HIKERAPI_BASE_URL", "https://api.hikerapi.com").rstrip("/")
    hikerapi_search_endpoint: str = _s("HIKERAPI_SEARCH_ENDPOINT", "/gql/topsearch")
    hikerapi_cost_per_call: float = _f("HIKERAPI_COST_PER_CALL", 0.001)

    apify_token: str = _s("APIFY_TOKEN")
    apify_actor: str = _s("APIFY_ACTOR", "memo23~instagram-reels-search-scraper")
    apify_cost_per_item: float = _f("APIFY_COST_PER_ITEM", 0.0015)

    database_url: str = _s("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'reels.db').as_posix()}")
    storage_dir: Path = Path(_s("STORAGE_DIR", str(BASE_DIR / "storage")))

    search_workers: int = _i("SEARCH_WORKERS", 2)
    download_workers: int = _i("DOWNLOAD_WORKERS", 8)
    max_pages: int = _i("MAX_PAGES", 20)
    page_delay_min: float = _f("PAGE_DELAY_MIN", 1.0)
    page_delay_max: float = _f("PAGE_DELAY_MAX", 2.0)
    run_timeout_min: int = _i("RUN_TIMEOUT_MIN", 15)
    video_retention_days: int = _i("VIDEO_RETENTION_DAYS", 90)
    raw_retention_days: int = _i("RAW_RETENTION_DAYS", 30)
    metadata_retention_days: int = _i("METADATA_RETENTION_DAYS", 365)
    url_ttl_hours: float = 3.0
    done_download_ratio: float = 0.95

    alert_webhook_url: str = _s("ALERT_WEBHOOK_URL")
    baseline_cost_per_keyword: float = _f("BASELINE_COST_PER_KEYWORD", 0.15)

    app_user: str = _s("APP_USER")
    app_password: str = _s("APP_PASSWORD")
    public_base_url: str = _s("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


settings = Settings()
if not settings.storage_dir.is_absolute():
    object.__setattr__(settings, "storage_dir", (BASE_DIR / settings.storage_dir).resolve())
settings.storage_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(exist_ok=True)
