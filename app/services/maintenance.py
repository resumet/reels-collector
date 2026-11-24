"""보관·삭제 (FR-60~62), 운영 지표·알림 (§14, US-05/06), 대시보드 집계 (FR-42; 일치도 추세 제외)."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

import httpx
from sqlalchemy import delete, func

from ..config import settings
from ..db import session_scope, utcnow
from ..models import Alert, ApiUsage, CollectionRun, MediaFile, RawPage, Reel, RunResult

log = logging.getLogger("maintenance")


# ------------------------------------------------------------------ 보관·삭제
def run_retention() -> dict:
    from .downloader import storage_path
    now = utcnow()
    stats = {"files_deleted": 0, "raw_pages_deleted": 0, "reels_deleted": 0}
    with session_scope() as s:
        files = s.query(MediaFile).filter(MediaFile.status == "stored", MediaFile.delete_after <= now).all()
        for f in files:
            if f.storage_key:
                storage_path(f.storage_key).unlink(missing_ok=True)
            f.status = "deleted"
            stats["files_deleted"] += 1
        stats["raw_pages_deleted"] = s.execute(
            delete(RawPage).where(RawPage.fetched_at < now - timedelta(days=settings.raw_retention_days))
        ).rowcount
        cutoff = now - timedelta(days=settings.metadata_retention_days)
        old = [pk for (pk,) in s.query(Reel.media_pk).filter(Reel.last_seen_at < cutoff)]
        if old:
            for f in s.query(MediaFile).filter(MediaFile.media_pk.in_(old)):
                if f.storage_key:
                    storage_path(f.storage_key).unlink(missing_ok=True)
            s.execute(delete(MediaFile).where(MediaFile.media_pk.in_(old)))
            s.execute(delete(RunResult).where(RunResult.media_pk.in_(old)))
            s.execute(delete(Reel).where(Reel.media_pk.in_(old)))
            stats["reels_deleted"] = len(old)
    log.info("retention: %s", stats)
    return stats


# ------------------------------------------------------------------ 집계
def dashboard(days: int = 7) -> dict:
    now = utcnow()
    since = now - timedelta(days=days)
    with session_scope() as s:
        runs = s.query(CollectionRun).filter(CollectionRun.created_at >= since).all()
        finished = [r for r in runs if r.status in ("done", "partial", "failed")]
        by_status = defaultdict(int)
        for r in runs:
            by_status[r.status] += 1
        found = sum(r.found_count for r in finished)
        downloaded = sum(r.downloaded_count for r in finished)

        calls = s.query(ApiUsage).filter(ApiUsage.called_at >= since).all()
        err_calls = [c for c in calls if c.http_status is None or c.http_status >= 400]
        cost_by_day: dict[str, float] = defaultdict(float)
        calls_by_provider: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "cost": 0.0})
        for c in calls:
            cost_by_day[c.called_at.date().isoformat()] += c.cost_usd or 0
            p = calls_by_provider[c.provider]
            p["calls"] += 1
            p["cost"] += c.cost_usd or 0
            if c.http_status is None or c.http_status >= 400:
                p["errors"] += 1
        days_list = [(since + timedelta(days=i + 1)).date().isoformat() for i in range(days)]

        run_errors = defaultdict(int)
        for r in finished:
            if r.error:
                run_errors[r.error.split(":")[0][:80]] += 1
        file_errors = defaultdict(int)
        for (err,) in s.query(MediaFile.last_error).filter(MediaFile.status == "failed",
                                                          MediaFile.last_error.is_not(None)):
            file_errors[err.split(":")[0][:80]] += 1

        total_cost = sum(c.cost_usd or 0 for c in calls)
        return {
            "days": days,
            "runs_total": len(runs),
            "runs_by_status": dict(by_status),
            "success_rate": (by_status["done"] / len(finished)) if finished else None,
            "save_rate": (downloaded / found) if found else None,
            "provider_error_rate": (len(err_calls) / len(calls)) if calls else None,
            "gone_410": sum(1 for c in calls if c.http_status == 410),
            "total_cost": total_cost,
            "avg_cost_per_run": (sum(r.cost_usd for r in finished) / len(finished)) if finished else None,
            "cost_by_day": [{"day": d, "cost": round(cost_by_day.get(d, 0.0), 4)} for d in days_list],
            "providers": dict(calls_by_provider),
            "run_errors": sorted(run_errors.items(), key=lambda x: -x[1])[:10],
            "file_errors": sorted(file_errors.items(), key=lambda x: -x[1])[:10],
            "storage_bytes": s.query(func.coalesce(func.sum(MediaFile.bytes), 0))
                              .filter(MediaFile.status == "stored").scalar(),
        }


# ------------------------------------------------------------------ 알림 (§14)
def _raise_alert(s, code: str, message: str, level: str = "warning") -> Alert | None:
    key = f"{code}:{utcnow().date().isoformat()}"
    if s.query(Alert.id).filter_by(dedup_key=key).first():
        return None
    a = Alert(code=code, level=level, message=message, dedup_key=key)
    s.add(a)
    return a


def evaluate_alerts() -> list[str]:
    d7 = dashboard(7)
    d1 = dashboard(1)
    new: list[Alert] = []
    with session_scope() as s:
        finished7 = sum(d7["runs_by_status"].get(k, 0) for k in ("done", "partial", "failed"))
        if finished7 >= 5 and d7["success_rate"] is not None and d7["success_rate"] < 0.80:
            new.append(_raise_alert(s, "run_success_low", f"최근 7일 실행 성공률 {d7['success_rate']:.0%} (<80%)"))
        if d7["save_rate"] is not None and d7["save_rate"] < 0.90:
            new.append(_raise_alert(s, "save_rate_low", f"최근 7일 영상 저장률 {d7['save_rate']:.0%} (<90%)"))
        if d1["provider_error_rate"] is not None and d1["provider_error_rate"] >= 0.15 and \
                sum(p["calls"] for p in d1["providers"].values()) >= 10:
            new.append(_raise_alert(s, "provider_errors",
                                    f"최근 24시간 제공사 오류율 {d1['provider_error_rate']:.0%} (≥15%)", "critical"))
        if d1["gone_410"]:
            new.append(_raise_alert(s, "provider_410", f"최근 24시간 HTTP 410 {d1['gone_410']}건 — 엔드포인트 폐기 확인 필요",
                                    "critical"))
        if d7["avg_cost_per_run"] and d7["avg_cost_per_run"] > 2 * settings.baseline_cost_per_keyword:
            new.append(_raise_alert(s, "cost_high", f"키워드당 평균 비용 ${d7['avg_cost_per_run']:.3f} "
                                                    f"(기준 ${settings.baseline_cost_per_keyword} 의 2배 초과)"))
        msgs = [a.message for a in new if a is not None]
    if msgs and settings.alert_webhook_url:
        try:
            httpx.post(settings.alert_webhook_url, json={"text": "[릴스 수집] " + "\n".join(msgs)}, timeout=10)
        except httpx.HTTPError as e:
            log.warning("webhook failed: %s", e)
    return msgs
