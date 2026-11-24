"""부록 A DDL 을 SQLAlchemy 로 옮긴 것. SQLite/PostgreSQL 모두 동작하도록 JSONB→JSON, TEXT[]→JSON 사용.

PRD 대비 변경:
- 제외: 주기 실행(FR-03), 모바일 일치도(FR-50~52, mobile_baselines) — 수동 실행만 지원
- 추가 collection_runs.cost_usd, deadline_at, provider_switched_from, note
- media_files.provider, next_attempt_at, refresh_count
- alerts, retention_extensions : 알림(US-06), 보관 연장 승인(FR-60)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

PK = BigInteger().with_variant(Integer, "sqlite")


class Keyword(Base):
    __tablename__ = "keywords"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    locale: Mapped[str] = mapped_column(Text, default="ko_KR", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # queued|searching|downloading|done|partial|failed
    found_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloaded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    search_exhausted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    screened: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, default="manual", nullable=False)  # 항상 manual (자동 수집 없음)
    provider_switched_from: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class RawPage(Base):
    __tablename__ = "raw_pages"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    cursor_in: Mapped[dict | None] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class Author(Base):
    __tablename__ = "authors"
    ig_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text)
    is_verified: Mapped[bool | None] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Reel(Base):
    __tablename__ = "reels"
    media_pk: Mapped[str] = mapped_column(Text, primary_key=True)
    shortcode: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("authors.ig_user_id"), index=True)
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list | None] = mapped_column(JSON)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    play_count: Mapped[int | None] = mapped_column(BigInteger)
    like_count: Mapped[int | None] = mapped_column(BigInteger)
    comment_count: Mapped[int | None] = mapped_column(BigInteger)
    share_count: Mapped[int | None] = mapped_column(BigInteger)
    music_title: Mapped[str | None] = mapped_column(Text)
    music_artist: Mapped[str | None] = mapped_column(Text)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RunResult(Base):
    __tablename__ = "run_results"
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id", ondelete="CASCADE"), primary_key=True)
    media_pk: Mapped[str] = mapped_column(ForeignKey("reels.media_pk", ondelete="CASCADE"), primary_key=True, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # reels_serp | hashtag_top | hashtag_recent
    metrics_snapshot: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("run_id", "rank"),)


class MediaFile(Base):
    __tablename__ = "media_files"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    media_pk: Mapped[str] = mapped_column(ForeignKey("reels.media_pk", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # video | thumbnail
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # pending|downloading|stored|failed|expired_url|deleted
    provider: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    url_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refresh_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    __table_args__ = (UniqueConstraint("media_pk", "kind"),)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_runs.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    billed_units: Mapped[float | None] = mapped_column(Float)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    called_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False, default="warning")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RetentionExtension(Base):
    __tablename__ = "retention_extensions"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    media_pk: Mapped[str] = mapped_column(ForeignKey("reels.media_pk", ondelete="CASCADE"), nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)  # pending|approved|rejected
    decided_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
