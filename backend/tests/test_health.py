"""Health (LIFE-06): metrics, averages, targets, medications and the privacy boundaries."""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.ai.api import facts
from app.modules.alerts import jobs as alert_jobs
from app.modules.health.models import HealthTarget
from app.modules.health.service import evaluate, series_key
from app.platform.db import today


def _t(direction: str, low: str | None = None, high: str | None = None, high2: str | None = None) -> HealthTarget:
    f = lambda x: None if x is None else Decimal(x)  # noqa: E731
    return HealthTarget(direction=direction, low=f(low), high=f(high), high2=f(high2))


def test_target_evaluation() -> None:
    assert evaluate(None, Decimal(80)) == "no_target" and evaluate(_t("below", high="80"), None) == "no_data"
    assert evaluate(_t("below", high="80"), Decimal(80)) == "ok"
    assert evaluate(_t("below", high="80"), Decimal("80.1")) == "above"
    assert evaluate(_t("above", low="8000"), Decimal(7000)) == "below"
    assert evaluate(_t("above", low="8000"), Decimal(8000)) == "ok"
    r = _t("range", "7", "9")
    assert [evaluate(r, Decimal(x)) for x in ("6.5", "7", "9", "9.5")] == ["below", "ok", "ok", "above"]
    bp = _t("below", high="130", high2="85")
    assert evaluate(bp, Decimal(128), Decimal(82)) == "ok"
    assert evaluate(bp, Decimal(128), Decimal(90)) == "above" and evaluate(bp, Decimal(135), Decimal(80)) == "above"
    assert series_key("custom", " Glucose ") == "custom:glucose" and series_key("weight", None) == "weight"


def test_overview_averages_targets_and_validation(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    t = today()
    for days_ago, kg in ((100, "90"), (60, "86"), (20, "82"), (10, "80"), (0, "78")):
        r = c.post(
            "/api/health/metrics",
            json={"person_id": me["id"], "kind": "weight", "value": kg, "date": str(t - timedelta(days=days_ago))},
        )
        assert r.status_code == 201
    c.post(
        "/api/health/metrics",
        json={"person_id": me["id"], "kind": "blood_pressure", "value": "128", "value2": "84", "note": "morning"},
    )
    c.post(
        "/api/health/metrics",
        json={"person_id": me["id"], "kind": "custom", "name": "Glucose", "unit": "mmol/l", "value": "5.4"},
    )
    c.put("/api/health/targets", json={"person_id": me["id"], "kind": "weight", "direction": "below", "high": "80"})
    c.put(
        "/api/health/targets", json={"person_id": me["id"], "kind": "weight", "direction": "below", "high": "75"}
    )  # replaces
    c.put(
        "/api/health/targets",
        json={"person_id": me["id"], "kind": "blood_pressure", "direction": "below", "high": "130", "high2": "80"},
    )
    c.put(
        "/api/health/targets",
        json={"person_id": me["id"], "kind": "sleep", "direction": "range", "low": "7", "high": "9"},
    )
    o = c.get("/api/health/overview", params={"person_id": me["id"]}).json()
    s = {x["series"]: x for x in o["series"]}
    w = s["weight"]
    assert w["latest"]["value"] == "78.0000" and w["count"] == 5 and w["unit"] == "kg"
    assert w["avg_30"]["value"] == "80.0"  # (82 + 80 + 78) / 3
    assert w["avg_90"]["value"] == "81.5"  # (86 + 82 + 80 + 78) / 4; the 100-day-old reading is outside
    assert w["target"]["high"] == "75.0000" and w["status"] == "above"
    assert len(w["points"]) == 5
    bp = s["blood_pressure"]
    assert bp["latest"]["value2"] == "84.0000" and bp["status"] == "above" and bp["avg_30"]["value2"] == "84.0"
    assert s["custom:glucose"]["label"] == "Glucose" and s["custom:glucose"]["unit"] == "mmol/l"
    assert s["custom:glucose"]["status"] == "no_target"
    assert s["sleep"]["status"] == "no_data" and s["sleep"]["latest"] is None  # target without readings still listed
    for bad in (
        {"kind": "blood_pressure", "value": "120"},
        {"kind": "custom", "value": "1"},
        {"kind": "mood", "value": "1"},
        {"kind": "weight", "value": "-1"},
        {"kind": "weight", "value": "1", "person_id": 999},
    ):
        assert c.post("/api/health/metrics", json={"person_id": me["id"], **bad}).status_code == 422
    assert (
        c.put("/api/health/targets", json={"person_id": me["id"], "kind": "weight", "direction": "below"}).status_code
        == 422
    )
    assert (
        c.put(
            "/api/health/targets",
            json={"person_id": me["id"], "kind": "sleep", "direction": "range", "low": "9", "high": "7"},
        ).status_code
        == 422
    )
    rows = c.get("/api/health/metrics", params={"person_id": me["id"], "series": "weight"}).json()
    assert rows[0]["date"] == str(t) and len(rows) == 5
    assert c.delete(f"/api/health/metrics/{rows[0]['id']}").status_code == 204
    assert (
        c.get("/api/health/overview", params={"person_id": me["id"]}).json()["series"][0]["latest"]["value"]
        == "80.0000"
    )
    assert c.get("/api/health/overview", params={"person_id": 999}).status_code == 422


def test_medications_feed_calendar_and_attention_privately(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    t = today()
    m = c.post(
        "/api/health/medications",
        json={
            "person_id": me["id"],
            "name": "Statin",
            "dose": "20 mg",
            "schedule": "evening",
            "refill_date": str(t + timedelta(days=3)),
        },
    ).json()
    c.post(
        "/api/health/medications", json={"person_id": me["id"], "name": "Old", "active": False, "refill_date": str(t)}
    )
    c.post(
        "/api/health/medications",
        json={"person_id": me["id"], "name": "Later", "refill_date": str(t + timedelta(days=40))},
    )
    cal = c.get("/api/calendar", params={"from": str(t), "to": str(t + timedelta(days=30))}).json()
    refill = [r for r in cal if r["source"] == "health"]
    assert (
        [r["title"] for r in refill] == ["Refill: Statin"]
        and refill[0]["category"] == "health"
        and refill[0]["private"]
    )
    att = {a["title"]: a for a in c.get("/api/attention").json()}
    assert att["Refill: Statin (Alex)"]["private"] is True and "Refill: Later (Alex)" not in att
    assert c.put(f"/api/health/medications/{m['id']}", json={**m, "refill_date": str(t - timedelta(days=1))}).json()[
        "refill_date"
    ] == str(t - timedelta(days=1))
    assert c.get("/api/attention").json()
    meds = c.get("/api/health/medications", params={"person_id": me["id"]}).json()
    assert [x["name"] for x in meds] == ["Statin", "Later", "Old"]
    # privacy: not in the AI facts, not in the digest or the push notifications
    assert "Statin" not in str(facts(db)) and "health" not in str(facts(db)).lower()
    sent: list[tuple[str, str]] = []
    orig = alert_jobs.send
    alert_jobs.send = lambda _db, _key, title, body, **_kw: sent.append((title, body)) or 1  # type: ignore[assignment]
    try:
        from app.platform.jobs import JobContext

        ctx = JobContext(db=db)
        alert_jobs.check(ctx)
        alert_jobs.digest(ctx)
    finally:
        alert_jobs.send = orig
    assert not any("Statin" in a or "Statin" in b for a, b in sent)
    assert any(a == "Weekly summary" for a, _ in sent)
