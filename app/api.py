from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, utcnow
from .models import Alert, Author, CollectionRun, Keyword, MediaFile, RawPage, Reel, RetentionExtension, RunResult
from .providers import configured_providers, get_provider
from .services import maintenance
from .services.collector import run_progress
from .services.downloader import storage_path
from .services.workers import enqueue_run, has_active_run

router = APIRouter(prefix="/api")


def iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def reel_url(code: str) -> str:
    return f"https://www.instagram.com/reel/{code}/"


# ------------------------------------------------------------------ 키워드 (FR-01)
class KeywordIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    target_count: int = Field(100, ge=1, le=1000)
    locale: str = "ko_KR"


class KeywordPatch(BaseModel):
    target_count: int | None = Field(None, ge=1, le=1000)
    locale: str | None = None
    is_active: bool | None = None


def kw_out(s: Session, k: Keyword) -> dict:
    last = s.query(CollectionRun).filter_by(keyword_id=k.id).order_by(CollectionRun.id.desc()).first()
    return {"id": k.id, "keyword": k.keyword, "target_count": k.target_count, "locale": k.locale,
            "is_active": k.is_active, "created_at": iso(k.created_at),
            "last_run": {"id": last.id, "status": last.status, "found_count": last.found_count,
                         "downloaded_count": last.downloaded_count, "created_at": iso(last.created_at)} if last else None}


@router.get("/keywords")
def list_keywords(db: Session = Depends(get_db)):
    return [kw_out(db, k) for k in db.query(Keyword).order_by(Keyword.is_active.desc(), Keyword.id.desc())]


@router.post("/keywords", status_code=201)
def create_keyword(body: KeywordIn, db: Session = Depends(get_db)):
    kw_text = body.keyword.strip()
    if db.query(Keyword).filter_by(keyword=kw_text).first():
        raise HTTPException(409, "이미 등록된 키워드입니다")
    k = Keyword(keyword=kw_text, target_count=body.target_count, locale=body.locale)
    db.add(k)
    db.commit()
    return kw_out(db, k)


@router.patch("/keywords/{kid}")
def update_keyword(kid: int, body: KeywordPatch, db: Session = Depends(get_db)):
    k = db.get(Keyword, kid) or _404()
    for f, v in body.model_dump(exclude_unset=True).items():
        setattr(k, f, v)
    db.commit()
    return kw_out(db, k)


def _404():
    raise HTTPException(404, "없음")


# ------------------------------------------------------------------ 실행 (FR-02, FR-04)
def start_run(s: Session, keyword_id: int) -> CollectionRun:
    k = s.get(Keyword, keyword_id) or _404()
    if not k.is_active:
        raise HTTPException(409, "비활성 키워드입니다")
    if has_active_run(s, keyword_id):
        raise HTTPException(409, "이 키워드는 이미 검색 중입니다")
    run = CollectionRun(keyword_id=keyword_id, provider=settings.primary_provider, status="queued", trigger="manual")
    s.add(run)
    s.commit()
    enqueue_run(run.id)
    return run


def run_out(s: Session, r: CollectionRun, detail: bool = False) -> dict:
    k = s.get(Keyword, r.keyword_id)
    out = {"id": r.id, "keyword_id": r.keyword_id, "keyword": k.keyword if k else None,
           "target_count": k.target_count if k else None, "provider": r.provider,
           "provider_switched_from": r.provider_switched_from, "status": r.status,
           "found_count": r.found_count, "downloaded_count": r.downloaded_count,
           "search_exhausted": r.search_exhausted, "screened": r.screened, "cost_usd": round(r.cost_usd, 4),
           "created_at": iso(r.created_at), "started_at": iso(r.started_at), "finished_at": iso(r.finished_at),
           "error": r.error, "note": r.note}
    if detail:
        out["progress"] = run_progress(s, r.id)
        out["pages"] = s.query(RawPage).filter_by(run_id=r.id).count()
    return out


@router.post("/keywords/{kid}/runs", status_code=201)
def create_run(kid: int, db: Session = Depends(get_db)):
    return run_out(db, start_run(db, kid))


@router.get("/runs")
def list_runs(keyword_id: int | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    q = db.query(CollectionRun)
    if keyword_id:
        q = q.filter_by(keyword_id=keyword_id)
    return [run_out(db, r) for r in q.order_by(CollectionRun.id.desc()).limit(limit)]


@router.get("/runs/{rid}")
def get_run(rid: int, db: Session = Depends(get_db)):
    return run_out(db, db.get(CollectionRun, rid) or _404(), detail=True)


def _results(db: Session, rid: int):
    rows = (db.query(RunResult, Reel, Author)
            .join(Reel, Reel.media_pk == RunResult.media_pk)
            .outerjoin(Author, Author.ig_user_id == Reel.author_id)
            .filter(RunResult.run_id == rid).order_by(RunResult.rank).all())
    files = {(f.media_pk, f.kind): f for f in db.query(MediaFile).filter(
        MediaFile.media_pk.in_([r.media_pk for r, _, _ in rows]))}
    for rr, reel, au in rows:
        v, t = files.get((reel.media_pk, "video")), files.get((reel.media_pk, "thumbnail"))
        snap = rr.metrics_snapshot or {}
        yield {
            "rank": rr.rank, "page_no": rr.page_no, "source": rr.source, "media_pk": reel.media_pk,
            "shortcode": reel.shortcode, "url": reel_url(reel.shortcode),
            "username": au.username if au else None, "full_name": au.full_name if au else None,
            "is_verified": au.is_verified if au else None,
            "caption": reel.caption, "hashtags": reel.hashtags or [], "taken_at": iso(reel.taken_at),
            "duration_sec": reel.duration_sec,
            "play_count": snap.get("play_count"), "like_count": snap.get("like_count"),
            "comment_count": snap.get("comment_count"), "share_count": snap.get("share_count"),
            "latest": {"play_count": reel.play_count, "like_count": reel.like_count,
                       "comment_count": reel.comment_count, "share_count": reel.share_count},
            "music_title": reel.music_title, "music_artist": reel.music_artist,
            "is_available": reel.is_available,
            "video_status": v.status if v else None, "video_error": v.last_error if v else None,
            "video_sha256": v.sha256 if v else None, "video_bytes": v.bytes if v else None,
            "video_duration": v.duration_sec if v else None,
            "video_resolution": f"{v.width}x{v.height}" if v and v.width else None,
            "video_codec": v.codec if v else None, "delete_after": iso(v.delete_after) if v else None,
            "thumb_status": t.status if t else None,
        }


@router.get("/runs/{rid}/results")
def run_results(rid: int, db: Session = Depends(get_db)):
    db.get(CollectionRun, rid) or _404()
    return list(_results(db, rid))


CSV_COLS = ["rank", "source", "page_no", "shortcode", "url", "username", "full_name", "is_verified", "caption",
            "hashtags", "taken_at", "duration_sec", "play_count", "like_count", "comment_count", "share_count",
            "music_title", "music_artist", "is_available", "video_stored", "video_sha256", "video_resolution",
            "video_codec", "delete_after"]


@router.get("/runs/{rid}/export.csv")
def export_csv(rid: int, db: Session = Depends(get_db)):  # FR-40
    run = db.get(CollectionRun, rid) or _404()
    kw = db.get(Keyword, run.keyword_id)
    buf = io.StringIO()
    buf.write("﻿")  # Excel 한글
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for row in _results(db, rid):
        row = dict(row, hashtags=" ".join("#" + h for h in row["hashtags"]),
                   video_stored="Y" if row["video_status"] == "stored" else "N")
        w.writerow(row)
    name = f"run_{rid}_{kw.keyword if kw else ''}.csv"
    from urllib.parse import quote
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/runs/{rid}/raw-pages")
def raw_pages(rid: int, db: Session = Depends(get_db)):
    return [{"id": p.id, "page_no": p.page_no, "endpoint": p.endpoint, "cursor_in": p.cursor_in,
             "fetched_at": iso(p.fetched_at)} for p in db.query(RawPage).filter_by(run_id=rid).order_by(RawPage.page_no)]


@router.get("/raw-pages/{pid}")
def raw_page(pid: int, db: Session = Depends(get_db)):
    p = db.get(RawPage, pid) or _404()
    return p.payload


# ------------------------------------------------------------------ 영상 재생 (FR-41)
@router.get("/media/{media_pk}/{kind}")
def media_file(media_pk: str, kind: str, db: Session = Depends(get_db)):
    if kind not in ("video", "thumbnail"):
        _404()
    f = db.query(MediaFile).filter_by(media_pk=media_pk, kind=kind, status="stored").first() or _404()
    path = storage_path(f.storage_key)
    if not path.exists():
        _404()
    return FileResponse(path, media_type="video/mp4" if kind == "video" else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ 보관 연장 승인 (FR-60)
class ExtensionIn(BaseModel):
    days: int = Field(90, ge=1, le=365)
    reason: str = Field(min_length=2)
    requested_by: str | None = None


class DecisionIn(BaseModel):
    decided_by: str = Field(min_length=1)


@router.post("/reels/{media_pk}/extensions", status_code=201)
def request_extension(media_pk: str, body: ExtensionIn, db: Session = Depends(get_db)):
    db.get(Reel, media_pk) or _404()
    e = RetentionExtension(media_pk=media_pk, days=body.days, reason=body.reason, requested_by=body.requested_by)
    db.add(e)
    db.commit()
    return {"id": e.id}


@router.get("/extensions")
def list_extensions(db: Session = Depends(get_db)):
    out = []
    for e, code, f in (db.query(RetentionExtension, Reel.shortcode, MediaFile)
                       .join(Reel, Reel.media_pk == RetentionExtension.media_pk)
                       .outerjoin(MediaFile, (MediaFile.media_pk == RetentionExtension.media_pk)
                                  & (MediaFile.kind == "video"))
                       .order_by(RetentionExtension.id.desc()).limit(200)):
        out.append({"id": e.id, "media_pk": e.media_pk, "shortcode": code, "url": reel_url(code), "days": e.days,
                    "reason": e.reason, "requested_by": e.requested_by, "status": e.status,
                    "decided_by": e.decided_by, "created_at": iso(e.created_at), "decided_at": iso(e.decided_at),
                    "delete_after": iso(f.delete_after) if f else None})
    return out


@router.post("/extensions/{eid}/{decision}")
def decide_extension(eid: int, decision: str, body: DecisionIn, db: Session = Depends(get_db)):
    if decision not in ("approve", "reject"):
        _404()
    e = db.get(RetentionExtension, eid) or _404()
    if e.status != "pending":
        raise HTTPException(409, "이미 처리됨")
    e.status = "approved" if decision == "approve" else "rejected"
    e.decided_by, e.decided_at = body.decided_by, utcnow()
    if e.status == "approved":
        for f in db.query(MediaFile).filter_by(media_pk=e.media_pk, status="stored"):
            f.delete_after = (f.delete_after or utcnow()) + timedelta(days=e.days)
    db.commit()
    return {"status": e.status}


# ------------------------------------------------------------------ 대시보드·알림·운영 (FR-42, US-05/06)
@router.get("/dashboard")
def get_dashboard(days: int = Query(7, ge=1, le=90)):
    return maintenance.dashboard(days)


@router.get("/alerts")
def list_alerts(db: Session = Depends(get_db)):
    return [{"id": a.id, "code": a.code, "level": a.level, "message": a.message, "created_at": iso(a.created_at),
             "acknowledged": a.acknowledged} for a in db.query(Alert).order_by(Alert.id.desc()).limit(100)]


@router.post("/alerts/{aid}/ack")
def ack_alert(aid: int, db: Session = Depends(get_db)):
    a = db.get(Alert, aid) or _404()
    a.acknowledged = True
    db.commit()
    return {"ok": True}


@router.post("/admin/alerts/evaluate")
def eval_alerts():
    return {"new": maintenance.evaluate_alerts()}


@router.post("/admin/retention/run")
def retention_now():
    return maintenance.run_retention()


@router.get("/providers")
def providers():
    out = []
    for name in configured_providers():
        try:
            h = get_provider(name).health()
        except Exception as e:  # noqa: BLE001
            h = {"ok": False, "detail": str(e)}
        out.append({"name": name, "role": "primary" if name == settings.primary_provider else "fallback", **h})
    return out
