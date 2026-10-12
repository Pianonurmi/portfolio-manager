"""Goals and habits (LIFE-04): progress maths, status, the real balance sheet, streaks and registrations."""

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.goals.models import Habit
from app.modules.goals.service import habit_stats, habit_warnings, progress, stamp_reached
from app.platform.db import today

D = Decimal


def test_progress_on_track_behind_reached() -> None:
    start, end = date(2026, 1, 1), date(2027, 1, 1)  # 365 days, 12 000 € target
    on = date(2026, 7, 2)  # day 182: the straight line is at ~5 975 €
    ok = progress(start, D(0), D(12000), end, D(6000), on)
    assert ok.status == "on_track" and ok.pct == D("50.0")
    assert ok.expected_now is not None and abs(ok.expected_now - D("5975")) < 10
    assert ok.required_monthly is not None and abs(ok.required_monthly - D("1000")) < 10
    assert ok.actual_monthly is not None and abs(ok.actual_monthly - D("1003")) < 10
    assert ok.projected_date is not None and abs((ok.projected_date - end).days) < 10
    behind = progress(start, D(0), D(12000), end, D(3000), on)
    assert behind.status == "behind" and behind.pct == D("25.0")
    assert behind.projected_date is not None and behind.projected_date > end  # at this pace it lands late
    assert progress(start, D(0), D(12000), end, D(12500), on).status == "reached"
    # past the date and short of the target
    assert progress(start, D(0), D(12000), end, D(11000), date(2027, 2, 1)).status == "behind"
    # no pace yet (started today) and no target date
    p = progress(on, D(0), D(100), None, D(0), on)
    assert p.status == "on_track" and p.actual_monthly is None and p.projected_date is None


def test_progress_debt_payoff_runs_downwards() -> None:
    p = progress(date(2026, 1, 1), D(10000), D(0), date(2027, 1, 1), D(7000), date(2026, 7, 2))
    assert p.pct == D("30.0") and p.status == "behind"
    assert p.required_monthly is not None and p.required_monthly < 0  # pay down
    ok = progress(date(2026, 1, 1), D(10000), D(0), date(2027, 1, 1), D(4000), date(2026, 7, 2))
    assert ok.status == "on_track" and ok.projected_date is not None and ok.projected_date < date(2027, 1, 1)
    assert progress(date(2026, 1, 1), D(10000), D(0), None, D(0), date(2026, 7, 2)).status == "reached"


def test_net_worth_goal_reads_the_real_sheet(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    cash = c.post("/api/accounts", json={"name": "Svea", "kind": "savings", "owners": own}).json()
    loan = c.post("/api/accounts", json={"name": "Laina", "kind": "loan", "owners": own}).json()
    t = today()
    c.post("/api/valuations", json={"account_id": cash["id"], "date": str(t), "value": "60000"})
    c.post("/api/valuations", json={"account_id": loan["id"], "date": str(t), "value": "10000", "method": "statement"})
    g = c.post(
        "/api/goals",
        json={
            "title": "Net worth 100k",
            "kind": "net_worth",
            "target_amount": "100000",
            "start_date": str(t - timedelta(days=100)),
            "target_date": str(t + timedelta(days=265)),
        },
    )
    assert g.status_code == 201, g.text
    g = g.json()
    assert g["current"] == "50000.00" and g["pct"] == "50.0" and g["status"] == "on_track"
    nw = c.get("/api/networth").json()
    assert Decimal(nw["net_worth"]["value"]) == Decimal(g["current"])
    # per-person scope reads the same sheet through that person's ownership
    mine = c.post(
        "/api/goals",
        json={"title": "Mine", "kind": "net_worth", "entity_id": me["entity_id"], "target_amount": "200000"},
    ).json()
    assert mine["current"] == "50000.00"
    # debt payoff: starts at what is owed now and falls
    d = c.post(
        "/api/goals",
        json={"title": "Loan free", "kind": "debt_payoff", "account_id": loan["id"], "target_amount": "0"},
    ).json()
    assert d["start_amount"] == "10000.0000" and d["current"] == "10000.00" and d["pct"] == "0.0"
    c.post("/api/valuations", json={"account_id": loan["id"], "date": str(t), "value": "7500", "method": "statement"})
    d2 = [x for x in c.get("/api/goals").json() if x["id"] == d["id"]][0]
    assert d2["current"] == "7500.00" and d2["pct"] == "25.0"
    bad = c.post("/api/goals", json={"title": "x", "kind": "debt_payoff", "target_amount": "0"})
    assert bad.status_code == 422
    assert c.post("/api/goals", json={"title": "x", "kind": "wish", "target_amount": "1"}).status_code == 422


def test_savings_goal_entries_timeline_attention_search(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    t = today()
    g = c.post(
        "/api/goals",
        json={
            "title": "Emergency fund",
            "kind": "savings",
            "target_amount": "6000",
            "start_date": str(t - timedelta(days=180)),
            "target_date": str(t + timedelta(days=180)),
        },
    ).json()
    assert g["manual"] and g["current"] == "0.00" and g["status"] == "behind"
    att = {a["title"]: a for a in c.get("/api/attention").json()}
    assert "Goal behind: Emergency fund" in att
    c.post(f"/api/goals/{g['id']}/entries", json={"amount": "2000", "date": str(t - timedelta(days=30))})
    r = c.post(f"/api/goals/{g['id']}/entries", json={"amount": "1500"}).json()
    assert r["current"] == "3500.00" and r["status"] == "on_track" and r["pct"] == "58.3"
    assert "Goal behind: Emergency fund" not in {a["title"] for a in c.get("/api/attention").json()}
    r = c.post(f"/api/goals/{g['id']}/entries", json={"amount": "2500"}).json()
    assert r["status"] == "reached" and r["reached_on"] == str(t)
    kinds = {x["kind"] for x in c.get("/api/timeline").json()}
    assert {"goal.created", "goal.reached"} <= kinds
    assert any(h["kind"] == "goal" for h in c.get("/api/search", params={"q": "emergency"}).json())
    # a market-driven goal cannot take manual entries
    nw = c.post("/api/goals", json={"title": "NW", "kind": "net_worth", "target_amount": "1"}).json()
    assert c.post(f"/api/goals/{nw['id']}/entries", json={"amount": "1"}).status_code == 422
    assert stamp_reached(db) >= 0


def _habit(cadence: str, per_week: int = 1) -> Habit:
    return Habit(title="h", cadence=cadence, times_per_week=per_week, active=True)


def test_daily_streaks() -> None:
    on = date(2026, 10, 14)  # a Wednesday
    days = [on - timedelta(days=n) for n in (0, 1, 2, 3, 10, 11, 12, 13, 14, 15)]
    st = habit_stats(_habit("daily"), days, on, on - timedelta(days=60))
    assert st["current_streak"] == 4 and st["best_streak"] == 6 and st["done_today"]
    assert st["rate_30d"] == D("0.333")  # 10 of 30 days
    # today not done yet: yesterday's streak is still alive
    st = habit_stats(_habit("daily"), [on - timedelta(days=n) for n in (1, 2, 3)], on, on - timedelta(days=60))
    assert st["current_streak"] == 3 and not st["done_today"]
    assert habit_warnings(_habit("daily"), st, on) == "at_risk"
    # missed yesterday and today: broken
    st = habit_stats(_habit("daily"), [on - timedelta(days=n) for n in (2, 3, 4)], on, on - timedelta(days=60))
    assert st["current_streak"] == 0 and st["best_streak"] == 3
    assert habit_warnings(_habit("daily"), st, on) == "broken"
    # a new habit is rated from its first day
    assert habit_stats(_habit("daily"), [on], on, on)["rate_30d"] == D("1.000")


def test_weekly_streaks() -> None:
    h = _habit("weekly", 3)
    mon = date(2026, 10, 12)  # Monday
    weeks = [mon - timedelta(days=7 * k) for k in (1, 2, 3, 5, 6)]  # weeks -1,-2,-3 and -5,-6 are done
    days = [w + timedelta(days=i) for w in weeks for i in (0, 2, 4)]
    on = mon + timedelta(days=2)  # Wednesday of a week with no check-ins yet
    st = habit_stats(h, days, on, mon - timedelta(days=60))
    assert st["current_streak"] == 3 and st["best_streak"] == 3
    # a week with only 2 of 3 does not count
    st = habit_stats(h, days + [mon + timedelta(days=0), mon + timedelta(days=1)], on, mon - timedelta(days=60))
    assert st["current_streak"] == 3 and st["done_this_week"] == 2
    st = habit_stats(h, days + [mon, mon + timedelta(days=1), mon + timedelta(days=2)], on, mon - timedelta(days=60))
    assert st["current_streak"] == 4
    # not reachable any more this week: 3 needed, none done, only Sunday left
    assert (
        habit_warnings(
            h, habit_stats(h, days, mon + timedelta(days=6), mon - timedelta(days=60)), mon + timedelta(days=6)
        )
        == "broken"
    )
    # rate: 3/week expected over 30 days = 12.857, done 9 in window → 0.7
    assert habit_stats(h, days, on, mon - timedelta(days=60))["rate_30d"] <= D("1")


def test_habit_api_checkins_and_attention(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    h = c.post("/api/habits", json={"title": "Stretch", "person_id": me["id"]}).json()
    assert h["current_streak"] == 0 and h["warning"] is None
    t = today()
    for n in range(3):
        r = c.post(f"/api/habits/{h['id']}/checkins", json={"date": str(t - timedelta(days=n))}).json()
    assert r["current_streak"] == 3 and r["done_today"] and r["best_streak"] == 3
    again = c.post(f"/api/habits/{h['id']}/checkins", json={}).json()  # idempotent
    assert again["current_streak"] == 3
    assert c.post(f"/api/habits/{h['id']}/checkins", json={"date": str(t + timedelta(days=1))}).status_code == 422
    r = c.delete(f"/api/habits/{h['id']}/checkins/{t}").json()
    assert not r["done_today"] and r["current_streak"] == 2
    assert r["warning"] is None  # a run of 2 is not yet worth a nag
    # a streak of 3+ that stopped two days ago is broken
    h2 = c.post("/api/habits", json={"title": "Run"}).json()
    for n in (2, 3, 4):
        c.post(f"/api/habits/{h2['id']}/checkins", json={"date": str(t - timedelta(days=n))})
    att = {a["title"] for a in c.get("/api/attention").json()}
    assert "Streak broken: Run" in att
    assert any(x["kind"] == "habit" for x in c.get("/api/search", params={"q": "stretch"}).json())
    assert c.post("/api/habits", json={"title": "x", "cadence": "hourly"}).status_code == 422
    assert [x["title"] for x in c.get("/api/habits").json()] == ["Run", "Stretch"]
