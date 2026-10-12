"""Worker process: runs scheduled jobs and jobs queued from the UI."""

import logging
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import __version__
from app.main import load_modules
from app.platform import system_jobs  # noqa: F401
from app.platform.config import get_settings
from app.platform.db import session_factory, utcnow
from app.platform.jobs import REGISTRY, apscheduler_crontab, enqueue, execute, missed_runs, next_queued, run_now
from app.platform.models import WorkerStatus

log = logging.getLogger("worker")
HEARTBEAT_SECONDS = 30


def heartbeat(started_at: datetime) -> None:
    with session_factory()() as db:
        row = db.get(WorkerStatus, 1) or WorkerStatus(id=1)
        row.version, row.started_at, row.heartbeat_at = __version__, started_at, utcnow()
        db.add(row)
        db.commit()


def queue_missed(tz: str) -> None:
    with session_factory()() as db:
        for name in missed_runs(db, utcnow(), ZoneInfo(tz)):
            enqueue(db, name, "catchup")
            log.info("queued missed run of %s", name)
        db.commit()


def main() -> None:
    load_modules()
    tz = get_settings().timezone
    sched = BackgroundScheduler(
        timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    )
    for jd in REGISTRY.values():
        if jd.cron:
            trigger = CronTrigger.from_crontab(apscheduler_crontab(jd.cron), timezone=tz)
            sched.add_job(run_now, trigger, args=[jd.name], id=jd.name)
            log.info("scheduled %s (%s)", jd.name, jd.cron)
    started_at = utcnow()
    heartbeat(started_at)
    # in the scheduler's threads, so a long job in the loop below does not stop the heartbeat
    sched.add_job(heartbeat, "interval", seconds=HEARTBEAT_SECONDS, args=[started_at], id="heartbeat")
    sched.start()
    run_now("rules.load", trigger="startup")
    queue_missed(tz)

    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop:
        rid = next_queued()
        if rid is not None:
            run = execute(rid)
            log.info("ran %s -> %s", run.job, run.status)
            continue
        time.sleep(2)
    sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
