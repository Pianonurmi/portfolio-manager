from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.platform.jobs import apscheduler_crontab, run_now
from app.platform.rules import rules_for


def test_rules_job_loads_tax_tables(db: Session) -> None:
    run = run_now("rules.load")
    assert run.status == "ok", run.log
    rules = rules_for(db, "tax", 2026)
    assert rules["capital_income"]["brackets"][1]["from"] == 30000
    assert rules["_version"] == "TAX-2026.3"
    # second run is a no-op
    assert run_now("rules.load").summary == "rule tables up to date"
    # later years fall back to the latest table
    assert rules_for(db, "tax", 2030)["year"] == 2026


def test_jobs_api_and_queue(auth_client: TestClient) -> None:
    jobs = {j["name"]: j for j in auth_client.get("/api/jobs").json()}
    assert "rules.load" in jobs
    r = auth_client.post("/api/jobs/rules.load/run")
    assert r.status_code == 202
    run = auth_client.get(f"/api/jobs/runs/{r.json()['run_id']}").json()
    assert run["status"] == "queued"
    assert auth_client.post("/api/jobs/nope/run").status_code == 404


def test_failed_job_is_recorded() -> None:
    from app.platform.jobs import JobContext, job

    @job("test.boom", "Boom")
    def boom(ctx: JobContext) -> None:
        raise RuntimeError("kaboom")

    run = run_now("test.boom")
    assert run.status == "failed" and "kaboom" in (run.summary or "")


def test_saved_views_and_dashboards(auth_client: TestClient) -> None:
    c = auth_client
    v = c.post("/api/views", json={"screen": "portfolio", "name": "By account", "state": {"group": "account"}}).json()
    assert c.get("/api/views", params={"screen": "portfolio"}).json()[0]["id"] == v["id"]
    d = c.put("/api/dashboards/0", json={"name": "Home", "layout": {"widgets": []}, "is_home": True}).json()
    d2 = c.put(f"/api/dashboards/{d['id']}", json={"name": "Home", "layout": {"widgets": [1]}, "is_home": True}).json()
    assert d2["version"] == 2


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_explain_unknown_kind(auth_client: TestClient) -> None:
    assert auth_client.get("/api/explain/nope:1").status_code == 404


def test_crontab_weekdays_follow_cron_numbering() -> None:
    # APScheduler counts weekdays from Monday; cron from Sunday
    assert apscheduler_crontab("45 18 * * 1-5") == "45 18 * * mon,tue,wed,thu,fri"
    assert apscheduler_crontab("0 5 * * 0") == "0 5 * * sun"
    assert apscheduler_crontab("0 5 * * 7") == "0 5 * * sun"
    assert apscheduler_crontab("0 4 * * 6") == "0 4 * * sat"
    assert apscheduler_crontab("0 9 * * 5-7") == "0 9 * * sun,fri,sat"
    assert apscheduler_crontab("0 9 * * */2") == "0 9 * * sun,tue,thu,sat"
    assert apscheduler_crontab("*/30 * * * *") == "*/30 * * * *"


def test_every_job_fires_on_the_cron_weekday() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    tz = ZoneInfo("Europe/Helsinki")
    sunday = datetime(2026, 10, 11, 12, 0, tzinfo=tz)
    monday = CronTrigger.from_crontab(apscheduler_crontab("0 7 * * 1"), timezone=tz)
    assert monday.get_next_fire_time(None, sunday).strftime("%a") == "Mon"
    weekdays = CronTrigger.from_crontab(apscheduler_crontab("45 18 * * 1-5"), timezone=tz)
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=tz)
    assert weekdays.get_next_fire_time(None, saturday).strftime("%a %d") == "Mon 12"


def test_missed_runs_are_found(db: Session) -> None:
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.platform.jobs import JobContext, job, missed_runs
    from app.platform.models import JobRun

    @job("test.weekly", "Weekly", cron="0 7 * * 1")
    @job("test.often", "Often", cron="*/30 * * * *")
    def noop(ctx: JobContext) -> None:
        pass

    tz = ZoneInfo("Europe/Helsinki")
    monday = datetime(2026, 10, 12, 8, 0, tzinfo=tz)
    missed = missed_runs(db, monday, tz)
    assert "test.weekly" in missed and "test.often" not in missed  # the half-hourly job just waits for its slot
    db.add(JobRun(job="test.weekly", status="ok", trigger="schedule", created_at=monday - timedelta(minutes=55)))
    db.flush()
    assert "test.weekly" not in missed_runs(db, monday, tz)
    assert "test.weekly" not in missed_runs(db, datetime(2026, 10, 9, 8, 0, tzinfo=UTC), tz)  # not due in 3 days


def test_worker_status_and_attention(auth_client: TestClient) -> None:
    from datetime import timedelta

    from app import __version__
    from app.platform.db import session_factory, utcnow
    from app.platform.models import WorkerStatus

    assert auth_client.get("/api/worker").json()["version"] is None
    with session_factory()() as db:
        db.add(WorkerStatus(id=1, version=__version__, started_at=utcnow(), heartbeat_at=utcnow()))
        db.commit()
    w = auth_client.get("/api/worker").json()
    assert w["alive"] and w["current"]
    assert not [a for a in auth_client.get("/api/attention").json() if a.get("chip") == "worker"]
    with session_factory()() as db:
        row = db.get(WorkerStatus, 1)
        assert row is not None
        row.version = "0.0.1"
        db.commit()
    titles = [a["title"] for a in auth_client.get("/api/attention").json()]
    assert f"Worker runs 0.0.1, the app {__version__}" in titles
    with session_factory()() as db:
        row = db.get(WorkerStatus, 1)
        assert row is not None
        row.heartbeat_at = utcnow() - timedelta(minutes=5)
        db.commit()
    assert "Worker is not running" in [a["title"] for a in auth_client.get("/api/attention").json()]
