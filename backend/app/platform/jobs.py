"""Job registry and runner. Runs are rows in `job_run`, so the UI can show them (NFR-17, ADR-08).

Modules register jobs with @job(...). The worker process schedules them by cron and also
executes runs queued from the UI (status "queued").
"""

import contextlib
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Literal

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.db import session_factory, utcnow
from app.platform.models import JobRun

Status = Literal["ok", "partial", "failed"]


@dataclass
class JobContext:
    db: Session
    lines: list[str] = field(default_factory=list)
    summary: str | None = None
    status: Status = "ok"

    def log(self, msg: str) -> None:
        self.lines.append(f"{utcnow():%H:%M:%S} {msg}")

    def warn(self, msg: str) -> None:
        self.status = "partial"
        self.log("WARN " + msg)


_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def apscheduler_crontab(expr: str) -> str:
    """Rewrite a crontab line so APScheduler reads its weekdays as cron does.

    APScheduler numbers weekdays from Monday (0 = mon), cron from Sunday (0 or 7 = sun), so
    CronTrigger.from_crontab("0 7 * * 1") fires on Tuesdays. Numeric weekdays become names.
    """
    fields = expr.split()
    if len(fields) != 5 or fields[4] == "*":
        return expr
    days: set[int] = set()
    for part in fields[4].split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            lo, hi = 0, 6
        elif "-" in rng:
            a, b = rng.split("-")
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(rng)
            if step:
                hi = 6
        days.update(d % 7 for d in range(lo, hi + 1, int(step or 1)))
    fields[4] = ",".join(_DAY_NAMES[d] for d in sorted(days))
    return " ".join(fields)


@dataclass(frozen=True)
class JobDef:
    name: str
    title: str
    func: Callable[[JobContext], None]
    cron: str | None  # crontab "m h dom mon dow" in Europe/Helsinki


REGISTRY: dict[str, JobDef] = {}
# Called with (job name, run summary) after a run fails; modules register (e.g. alerts).
ON_FAILURE: list[Callable[[str, str], None]] = []


def job(
    name: str, title: str, cron: str | None = None
) -> Callable[[Callable[[JobContext], None]], Callable[[JobContext], None]]:
    def deco(fn: Callable[[JobContext], None]) -> Callable[[JobContext], None]:
        REGISTRY[name] = JobDef(name=name, title=title, func=fn, cron=cron)
        return fn

    return deco


def execute(run_id: int) -> JobRun:
    """Execute a queued run in its own transaction; the run row is committed separately."""
    sf = session_factory()
    with sf() as meta:
        run = meta.get(JobRun, run_id)
        assert run is not None
        jd = REGISTRY.get(run.job)
        run.status = "running"
        run.started_at = utcnow()
        meta.commit()
        if jd is None:
            run.status, run.finished_at, run.summary = "failed", utcnow(), f"unknown job {run.job}"
            meta.commit()
            return run
        with sf() as db:
            ctx = JobContext(db=db)
            try:
                jd.func(ctx)
                db.commit()
                run.status = ctx.status
            except Exception as exc:
                db.rollback()
                ctx.lines.append(traceback.format_exc())
                ctx.summary = ctx.summary or f"{type(exc).__name__}: {exc}"
                run.status = "failed"
        run.finished_at = utcnow()
        run.summary = ctx.summary
        run.log = "\n".join(ctx.lines)[-50_000:]
        meta.commit()
        meta.refresh(run)
        if run.status == "failed":
            for hook in ON_FAILURE:
                with contextlib.suppress(Exception):  # alerting must never break the runner
                    hook(run.job, run.summary or "")
        return run


def enqueue(db: Session, name: str, trigger: str = "manual") -> JobRun:
    if name not in REGISTRY:
        raise KeyError(name)
    run = JobRun(job=name, status="queued", trigger=trigger)
    db.add(run)
    db.flush()
    return run


def run_now(name: str, trigger: str = "schedule") -> JobRun:
    with session_factory()() as db:
        run = enqueue(db, name, trigger)
        db.commit()
        rid = run.id
    return execute(rid)


def next_queued() -> int | None:
    with session_factory()() as db:
        rid = db.scalar(select(JobRun.id).where(JobRun.status == "queued").order_by(JobRun.created_at).limit(1))
        return rid


def missed_runs(db: Session, now: datetime, tz: tzinfo, lookback: timedelta = timedelta(days=3)) -> list[str]:
    """Scheduled jobs whose latest due time in the lookback passed without a run, e.g. while the worker restarted.

    The scheduler keeps its plan in memory, so a run due during a restart is lost. Jobs due again within the hour
    are left to their next slot.
    """
    out = []
    for jd in REGISTRY.values():
        if not jd.cron:
            continue
        trigger = CronTrigger.from_crontab(apscheduler_crontab(jd.cron), timezone=tz)
        due, nxt = None, trigger.get_next_fire_time(None, now - lookback)
        while nxt is not None and nxt <= now:
            due, nxt = nxt, trigger.get_next_fire_time(nxt, nxt + timedelta(seconds=1))
        if due is None or (nxt is not None and nxt - due <= timedelta(hours=1)):
            continue
        if db.scalar(select(JobRun.id).where(JobRun.job == jd.name, JobRun.created_at >= due).limit(1)) is None:
            out.append(jd.name)
    return out
