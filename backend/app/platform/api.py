"""Platform endpoints: health, jobs, explain, search, undo, outbound log, rules, dashboards, views."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Integer, func, select, text
from sqlalchemy.orm import Session

from app.platform.auth import CurrentUser, Db
from app.platform.db import utcnow
from app.platform.explain import EXPLAINERS, ExplainNode
from app.platform.jobs import REGISTRY, enqueue
from app.platform.models import Dashboard, JobRun, OutboundLog, RuleSet, SavedView, UndoEntry, WorkerStatus
from app.platform.registry import SEARCH_PROVIDERS, UNDO_HANDLERS, SearchHit, attention_provider

health = APIRouter(tags=["health"])
router = APIRouter(prefix="/api", tags=["platform"])


@health.get("/healthz")
def healthz(db: Db) -> dict[str, str]:
    db.execute(text("select 1"))
    return {"status": "ok"}


# ---- jobs -----------------------------------------------------------------------------------


def worker_state(db: Db) -> dict[str, Any]:
    """Whether the worker is alive (heartbeat within two minutes) and runs the same version as the api."""
    from app import __version__

    w = db.get(WorkerStatus, 1)
    return {
        "api_version": __version__,
        "version": w.version if w else None,
        "started_at": w.started_at if w else None,
        "heartbeat_at": w.heartbeat_at if w else None,
        "alive": w is not None and utcnow() - w.heartbeat_at < timedelta(minutes=2),
        "current": w is not None and w.version == __version__,
    }


@router.get("/worker")
def worker(_: CurrentUser, db: Db) -> dict[str, Any]:
    return worker_state(db)


@attention_provider
def _worker_attention(db: Session) -> list[dict[str, Any]]:
    w = worker_state(db)
    if w["version"] is None:  # the worker has never started (fresh install)
        return []
    if not w["alive"]:
        return [
            {
                "severity": "bad",
                "title": "Worker is not running",
                "detail": f"last heartbeat {w['heartbeat_at']:%Y-%m-%d %H:%M} UTC · scheduled jobs are not running",
                "route": "/settings/jobs",
                "chip": "worker",
            }
        ]
    if not w["current"]:
        return [
            {
                "severity": "warn",
                "title": f"Worker runs {w['version']}, the app {w['api_version']}",
                "detail": "redeploy with docker compose up -d --build",
                "route": "/settings/jobs",
                "chip": "worker",
            }
        ]
    return []


class JobOut(BaseModel):
    name: str
    title: str
    cron: str | None
    last_status: str | None
    last_started: str | None
    last_finished: str | None
    last_summary: str | None
    last_run_id: int | None


@router.get("/jobs")
def list_jobs(_: CurrentUser, db: Db) -> list[JobOut]:
    out: list[JobOut] = []
    for jd in sorted(REGISTRY.values(), key=lambda j: j.title):
        last = db.scalar(select(JobRun).where(JobRun.job == jd.name).order_by(JobRun.created_at.desc()).limit(1))
        out.append(
            JobOut(
                name=jd.name,
                title=jd.title,
                cron=jd.cron,
                last_status=last.status if last else None,
                last_started=last.started_at.isoformat() if last and last.started_at else None,
                last_finished=last.finished_at.isoformat() if last and last.finished_at else None,
                last_summary=last.summary if last else None,
                last_run_id=last.id if last else None,
            )
        )
    return out


@router.get("/jobs/runs/{run_id}")
def job_run(run_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    run = db.get(JobRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return {
        "id": run.id,
        "job": run.job,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "summary": run.summary,
        "log": run.log,
    }


@router.post("/jobs/{name}/run", status_code=202)
def queue_job(name: str, _: CurrentUser, db: Db) -> dict[str, int]:
    try:
        run = enqueue(db, name)
    except KeyError:
        raise HTTPException(404, f"Unknown job {name}") from None
    return {"run_id": run.id}


# ---- explain / search / undo ------------------------------------------------------------------


@router.get("/explain/{ref:path}")
def explain(ref: str, _: CurrentUser, db: Db) -> ExplainNode:
    kind, _sep, rest = ref.partition(":")
    fn = EXPLAINERS.get(kind)
    if fn is None:
        raise HTTPException(404, f"No explanation available for {kind!r}")
    args = rest.split(":") if rest else []
    try:
        return fn(db, *args)
    except (LookupError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/search")
def search(_: CurrentUser, db: Db, q: str = Query(min_length=1, max_length=100), limit: int = 20) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for provider in SEARCH_PROVIDERS:
        hits.extend(provider(db, q, limit))
    return hits[: limit * 3]


@router.post("/undo/{undo_id}", status_code=204)
def undo(undo_id: int, user: CurrentUser, db: Db) -> None:
    entry = db.get(UndoEntry, undo_id)
    if entry is None or entry.undone_at is not None:
        raise HTTPException(404, "Nothing to undo")
    if entry.expires_at < utcnow():
        raise HTTPException(410, "This action can no longer be undone")
    handler = UNDO_HANDLERS.get(entry.action)
    if handler is None:
        raise HTTPException(400, f"No undo handler for {entry.action}")
    handler(db, entry.inverse)
    entry.undone_at = utcnow()


def make_undo(db: Db, action: str, label: str, inverse: dict[str, Any], minutes: int = 30) -> int:
    entry = UndoEntry(action=action, label=label, inverse=inverse, expires_at=utcnow() + timedelta(minutes=minutes))
    db.add(entry)
    db.flush()
    return entry.id


# ---- outbound / rules ---------------------------------------------------------------------------


@router.get("/outbound")
def outbound(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    from app.platform.config import get_settings

    allow = get_settings().outbound_allowlist
    rows = db.execute(
        select(
            OutboundLog.host,
            func.max(OutboundLog.created_at),
            func.count(),
            func.sum(OutboundLog.blocked.cast(Integer)),
        ).group_by(OutboundLog.host)
    ).all()
    stats = {r[0]: (r[1], r[2], r[3] or 0) for r in rows}
    out = [
        {"host": h, "allowed": True, "last_call": stats.get(h, (None,))[0], "calls": stats.get(h, (None, 0))[1]}
        for h in allow
    ]
    for h, (last, n, blocked) in stats.items():
        if h not in allow:
            out.append({"host": h, "allowed": False, "last_call": last, "calls": n, "blocked": blocked})
    return out


@router.get("/rules")
def rules(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    rows = db.scalars(select(RuleSet).order_by(RuleSet.domain, RuleSet.year.desc())).all()
    return [{"domain": r.domain, "year": r.year, "version": r.version, "loaded_at": r.created_at} for r in rows]


# ---- dashboards and saved views (UX-03) ---------------------------------------------------------


class DashboardIn(BaseModel):
    name: str
    layout: dict[str, Any]
    is_home: bool = False


@router.get("/dashboards")
def dashboards(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {"id": d.id, "name": d.name, "is_home": d.is_home, "layout": d.layout, "version": d.version}
        for d in db.scalars(select(Dashboard).order_by(Dashboard.id))
    ]


@router.put("/dashboards/{dash_id}")
def save_dashboard(dash_id: int, body: DashboardIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    d = db.get(Dashboard, dash_id)
    if d is None:
        d = Dashboard(name=body.name, layout=body.layout, is_home=body.is_home)
        db.add(d)
    else:
        d.name, d.layout, d.is_home, d.version = body.name, body.layout, body.is_home, d.version + 1
    db.flush()
    return {"id": d.id, "version": d.version}


class ViewIn(BaseModel):
    screen: str
    name: str
    state: dict[str, Any]


@router.get("/views")
def views(_: CurrentUser, db: Db, screen: str) -> list[dict[str, Any]]:
    return [
        {"id": v.id, "name": v.name, "state": v.state}
        for v in db.scalars(select(SavedView).where(SavedView.screen == screen).order_by(SavedView.name))
    ]


@router.post("/views", status_code=201)
def save_view(body: ViewIn, _: CurrentUser, db: Db) -> dict[str, int]:
    v = SavedView(screen=body.screen, name=body.name, state=body.state)
    db.add(v)
    db.flush()
    return {"id": v.id}


@router.delete("/views/{view_id}", status_code=204)
def delete_view(view_id: int, _: CurrentUser, db: Db) -> None:
    v = db.get(SavedView, view_id)
    if v:
        db.delete(v)
