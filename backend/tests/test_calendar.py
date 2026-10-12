"""Calendar (LIFE-05): recurrence maths, aggregation across modules, .ics output, reminders."""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.calendar.recurrence import add_months, next_on_or_after, occurrences, rrule
from app.modules.core.models import Account
from app.platform.db import today


def _dates(start: date, rec: str, lo: date, hi: date) -> list[str]:
    return [d.isoformat() for d in occurrences(start, rec, lo, hi)]


def test_monthly_on_the_31st_returns_to_its_day() -> None:
    got = _dates(date(2026, 1, 31), "monthly", date(2026, 1, 1), date(2026, 6, 30))
    assert got == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30"]
    assert add_months(date(2026, 1, 31), 13) == date(2027, 2, 28)
    leap = _dates(date(2027, 12, 31), "monthly", date(2028, 2, 1), date(2028, 2, 29))
    assert leap == ["2028-02-29"]
    # starting the window late does not shift anything
    assert _dates(date(2026, 1, 31), "monthly", date(2026, 3, 1), date(2026, 4, 30)) == ["2026-03-31", "2026-04-30"]


def test_yearly_feb_29_and_weekly() -> None:
    got = _dates(date(2024, 2, 29), "yearly", date(2024, 1, 1), date(2029, 12, 31))
    assert got == ["2024-02-29", "2025-02-28", "2026-02-28", "2027-02-28", "2028-02-29", "2029-02-28"]
    assert _dates(date(2026, 10, 1), "weekly", date(2026, 10, 14), date(2026, 10, 29)) == [
        "2026-10-15",
        "2026-10-22",
        "2026-10-29",
    ]
    assert _dates(date(2026, 10, 1), "none", date(2026, 9, 1), date(2026, 9, 30)) == []
    assert next_on_or_after(date(2026, 1, 31), "monthly", date(2026, 2, 1)) == date(2026, 2, 28)
    assert next_on_or_after(date(2026, 1, 1), "none", date(2026, 2, 1)) is None


def test_rrule_matches_the_clamping() -> None:
    assert rrule(date(2026, 1, 15), "monthly") == "FREQ=MONTHLY"
    assert rrule(date(2026, 1, 31), "monthly") == "FREQ=MONTHLY;BYMONTHDAY=28,29,30,31;BYSETPOS=-1"
    assert rrule(date(2024, 2, 29), "yearly") == "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1"
    assert rrule(date(2026, 1, 1), "none") is None


def _event(c: TestClient, **kw: object) -> dict[str, object]:
    body: dict[str, object] = {"title": "Event", "start_date": str(today())}
    body.update(kw)
    r = c.post("/api/calendar/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def test_events_crud_validation_and_agenda(auth_client: TestClient) -> None:
    c = auth_client
    t = today()
    e = _event(c, title="Rent", start_date=str(date(t.year, 1, 31)), recurrence="monthly", category="finance")
    _event(
        c,
        title="Dentist",
        start_date=str(t + timedelta(days=3)),
        all_day=False,
        start_time="09:30:00",
        category="health",
    )
    rows = c.get("/api/calendar", params={"from": str(t), "to": str(t + timedelta(days=40))}).json()
    assert any(r["title"] == "Rent" and r["recurring"] for r in rows) and rows == sorted(
        rows, key=lambda r: (r["date"], r["time"] or "", r["title"])
    )
    dent = [r for r in rows if r["title"] == "Dentist"][0]
    assert dent["time"] == "09:30" and dent["private"] is True and dent["source"] == "calendar"
    only = c.get(
        "/api/calendar", params={"from": str(t), "to": str(t + timedelta(days=40)), "category": "health"}
    ).json()
    assert [r["title"] for r in only] == ["Dentist"]
    assert c.put(f"/api/calendar/events/{e['id']}", json={**e, "title": "Rent due"}).json()["title"] == "Rent due"
    for bad in (
        {"recurrence": "daily"},
        {"category": "x"},
        {"end_date": str(t - timedelta(days=1))},
        {"all_day": False},
        {"person_id": 999},
    ):
        assert c.post("/api/calendar/events", json={"title": "x", "start_date": str(t), **bad}).status_code == 422
    assert c.delete(f"/api/calendar/events/{e['id']}").status_code == 204
    assert c.get("/api/calendar/events").json()[0]["title"] == "Dentist"
    assert c.get("/api/calendar", params={"from": "2026-01-01", "to": "2030-01-01"}).status_code == 422


def test_multi_day_event_overlaps_the_window(auth_client: TestClient) -> None:
    c = auth_client
    _event(c, title="Trip", start_date="2026-12-28", end_date="2027-01-03")
    rows = c.get("/api/calendar", params={"from": "2027-01-01", "to": "2027-01-31"}).json()
    assert [(r["title"], r["date"], r["end_date"]) for r in rows] == [("Trip", "2026-12-28", "2027-01-03")]


def test_aggregate_includes_other_modules(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    t = today()
    c.post(
        "/api/policies",
        json={"kind": "home", "insurer": "If", "renewal_date": str(t + timedelta(days=5))},
    )
    c.post(
        "/api/subscriptions",
        json={"name": "Antivirus", "amount": "59.90", "period": "monthly", "next_due": str(t + timedelta(days=2))},
    )
    c.post("/api/vehicles", json={"name": "Octavia", "inspection_due": str(t + timedelta(days=9))})
    c.post("/api/home", json={"title": "Chimney sweep", "kind": "task", "next_due": str(t + timedelta(days=11))})
    c.post(
        "/api/goals",
        json={"title": "Fund", "kind": "savings", "target_amount": "100", "target_date": str(t + timedelta(days=20))},
    )
    loan = c.post("/api/accounts", json={"name": "Mortgage", "kind": "mortgage"}).json()
    acct = db.get(Account, loan["id"])
    assert acct is not None
    acct.meta = {"next_reset": str(t + timedelta(days=15)), "end_date": str(t + timedelta(days=9000))}
    db.commit()
    rows = c.get("/api/calendar", params={"from": str(t), "to": str(t + timedelta(days=60))}).json()
    by = {r["title"]: r for r in rows}
    pol = by["If home insurance renews"]
    assert (
        pol["source"] == "insurance"
        and pol["category"] == "finance"
        and pol["route"]
        and pol["date"] == str(t + timedelta(days=5))
    )
    assert by["Octavia: Katsastus"]["source"] == "vehicles"
    assert by["Home: Chimney sweep"]["category"] == "home"
    assert by["Goal target: Fund"]["source"] == "goals"
    assert by["Rate reset: Mortgage"]["source"] == "loans" and "Final payment: Mortgage" not in by  # outside the window
    subs = [r for r in rows if r["title"].startswith("Antivirus")]
    assert len(subs) == 2 and subs[0]["source"] == "subscriptions"  # now and one month on
    assert {r["source"] for r in rows} >= {"insurance", "vehicles", "home", "goals", "subscriptions"}
    f = c.get("/api/calendar", params={"from": str(t), "to": str(t + timedelta(days=60)), "source": "vehicles"}).json()
    assert [r["title"] for r in f] == ["Octavia: Katsastus"]


def test_ics_export(auth_client: TestClient) -> None:
    c = auth_client
    _event(
        c,
        title="Rent, due; pay",
        start_date="2026-01-31",
        recurrence="monthly",
        notes="Line 1\nLine 2",
        reminder_days_before=3,
    )
    _event(c, title="Meeting", start_date="2026-10-20", all_day=False, start_time="09:30:00", end_time="10:15:00")
    _event(c, title="Trip", start_date="2026-12-28", end_date="2027-01-03")
    _event(c, title="Birthday", start_date="2024-02-29", recurrence="yearly", category="family")
    c.post("/api/policies", json={"kind": "home", "insurer": "If", "renewal_date": str(today() + timedelta(days=5))})
    r = c.get("/api/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "\n" not in body.replace("\r\n", "")  # CRLF only (an escaped newline is the two characters \n)
    assert body.count("BEGIN:VEVENT") == body.count("END:VEVENT") == 4  # own events only by default
    assert "UID:event-1@" in body and body.count("DTSTAMP:") == 4
    assert "SUMMARY:Rent\\, due\\; pay" in body and "DESCRIPTION:Line 1\\nLine 2" in body
    assert "RRULE:FREQ=MONTHLY;BYMONTHDAY=28,29,30,31;BYSETPOS=-1" in body
    assert "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1" in body
    assert body.count("RRULE:") == 2
    assert "DTSTART:20261020T093000" in body and "DTEND:20261020T101500" in body
    assert "DTSTART;VALUE=DATE:20261228" in body and "DTEND;VALUE=DATE:20270104" in body  # end is exclusive
    assert "TRIGGER:-P3D" in body
    assert "If home insurance" not in body
    both = c.get("/api/calendar.ics", params={"include": "all"}).text
    assert "SUMMARY:If home insurance renews" in both and both.count("BEGIN:VEVENT") == 5
    ranged = c.get("/api/calendar.ics", params={"from": "2026-10-01", "to": "2026-10-31"}).text
    assert "SUMMARY:Meeting" in ranged and "SUMMARY:Trip" not in ranged and "SUMMARY:Rent" in ranged


def test_ics_folds_long_lines() -> None:
    from app.modules.calendar.ics import _fold

    out = _fold("SUMMARY:" + "ä" * 100)
    assert all(len(x.encode()) <= 75 for x in out.split("\r\n"))
    assert out.replace("\r\n ", "") == "SUMMARY:" + "ä" * 100


def test_reminders_attention_timeline_search(auth_client: TestClient) -> None:
    c = auth_client
    t = today()
    _event(c, title="Tax return deadline", start_date=str(t + timedelta(days=4)), reminder_days_before=7)
    _event(c, title="Far away", start_date=str(t + timedelta(days=40)), reminder_days_before=7)
    _event(c, title="No reminder", start_date=str(t + timedelta(days=2)))
    _event(c, title="Check-up", start_date=str(t + timedelta(days=1)), category="health", reminder_days_before=2)
    att = {a["title"]: a for a in c.get("/api/attention").json()}
    assert "Coming up: Tax return deadline" in att and att["Coming up: Tax return deadline"]["detail"].startswith(
        "in 4 days"
    )
    assert "Coming up: Far away" not in att and "Coming up: No reminder" not in att
    assert att["Coming up: Check-up"]["private"] is True and not att["Coming up: Tax return deadline"]["private"]
    tl = [x["title"] for x in c.get("/api/timeline").json()]
    assert "Tax return deadline" in tl and "Check-up" not in tl  # health appointments stay off the shared timeline
    hits = c.get("/api/search", params={"q": "tax return"}).json()
    assert any(h["kind"] == "event" for h in hits)
    assert not any(h["title"] == "Check-up" for h in c.get("/api/search", params={"q": "check"}).json())
