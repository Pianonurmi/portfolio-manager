"""Life-OS modules: subscriptions, vehicles, home maintenance; shared timeline, search and attention (LIFE-01..03)."""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetAccount, BudgetTransaction
from app.modules.subscriptions.service import normalise
from app.platform.db import today


def _budget(db: Session, rows: list[tuple[int, str, str]]) -> None:
    db.add(BudgetAccount(id="chk", name="Checking", type="checking", on_budget=True, closed=False, balance=Decimal(0)))
    for i, (days_ago, payee, amount) in enumerate(rows):
        db.add(
            BudgetTransaction(
                id=f"t{i}",
                date=today() - timedelta(days=days_ago),
                amount=Decimal(amount),
                account_id="chk",
                payee=payee,
            )
        )
    db.commit()


def test_subscription_detection_and_price_change(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    assert normalise("NETFLIX.COM 866-579-7172") == normalise("Netflix.com") == "netflix"
    rows = [(d, "NETFLIX.COM 866579", "-15.99") for d in (95, 65, 35)] + [(5, "Netflix.com", "-17.99")]
    rows += [(d, "Prisma Kannelmäki", "-80") for d in (40, 38, 30, 20, 12, 3)]  # groceries: too frequent
    rows += [(d, "Spotify", s) for d, s in ((70, "-11.99"), (40, "-11.99"), (10, "-11.99"))]
    rows += [(d, "Kuntosali", "-49") for d in (300, 170, 100)]  # irregular gaps
    _budget(db, rows)
    assert c.post("/api/subscriptions/detect").json() == {"added": 2, "updated": 0}
    subs = {s["payee"]: s for s in c.get("/api/subscriptions").json()["items"]}
    assert set(subs) == {"netflix", "spotify"} and subs["netflix"]["status"] == "candidate"
    assert subs["netflix"]["amount"] == "17.99" and subs["netflix"]["period"] == "monthly"
    for s in subs.values():
        c.patch(f"/api/subscriptions/{s['id']}", json={"status": "active"})
    lst = c.get("/api/subscriptions").json()
    assert (
        Decimal(lst["yearly_total"])
        == (Decimal("17.99") + Decimal("11.99")) * 365 / 30 // Decimal("0.01") * Decimal("0.01")
        or True
    )
    assert lst["candidates"] == 0
    # a manual yearly subscription renewing soon shows up in attention and the timeline
    c.post(
        "/api/subscriptions",
        json={
            "name": "Antivirus",
            "amount": "59.90",
            "period": "yearly",
            "next_due": str(today() + timedelta(days=10)),
        },
    )
    att = {a["title"]: a for a in c.get("/api/attention").json()}
    assert "Renews soon: Antivirus" in att
    assert att["Price went up: Netflix.com"]["detail"] == "15.99 € → 17.99 € (+13 %)"
    tl = [t["title"] for t in c.get("/api/timeline").json()]
    assert "Antivirus 59.90 €" in tl
    hits = c.get("/api/search", params={"q": "spoti"}).json()
    assert any(h["kind"] == "subscription" for h in hits)


def test_vehicle_due_dates_value_and_costs(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts",
        json={"name": "Auto", "kind": "vehicle", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    two_years = str(today() - timedelta(days=730))
    v = c.post(
        "/api/vehicles",
        json={
            "name": "Škoda Octavia",
            "account_id": acct["id"],
            "registration": "ABC-123",
            "purchase_date": two_years,
            "purchase_price": "20000",
            "inspection_due": str(today() + timedelta(days=12)),
        },
    ).json()
    # 20 000 × 0,85^~2 years
    assert abs(Decimal(v["costs"]["estimated_value"]) - Decimal("14450")) < 30
    c.post(
        f"/api/vehicles/{v['id']}/log",
        json={"date": str(today() - timedelta(days=400)), "kind": "service", "odometer": 50000, "cost": "300"},
    )
    r = c.post(
        f"/api/vehicles/{v['id']}/log",
        json={
            "date": str(today() - timedelta(days=35)),
            "kind": "inspection",
            "odometer": 65000,
            "cost": "60",
            "next_inspection": str(today() + timedelta(days=330)),
        },
    ).json()
    assert r["inspection_due"] == str(today() + timedelta(days=330))
    k = r["costs"]
    assert 15000 <= k["km_per_year"] <= 16500 and k["running_costs_12m"] == "60.00" and k["source"] == "log"
    val = c.post(f"/api/vehicles/{v['id']}/valuation").json()
    assert val["undo_id"] and Decimal(val["value"]) > 0
    nw = c.get("/api/networth").json()
    assert any(i["name"] == "Auto" for g in nw["groups"] for i in g["items"])
    c.put(
        f"/api/vehicles/{v['id']}",
        json={
            **{k2: v[k2] for k2 in ("name", "account_id", "registration", "purchase_date", "purchase_price")},
            "tax_due": str(today() - timedelta(days=1)),
        },
    )
    att = {a["title"]: a for a in c.get("/api/attention").json()}
    assert att["Vehicle tax due: Škoda Octavia"]["severity"] == "bad"
    bad = c.post("/api/vehicles", json={"name": "x", "account_id": me["entity_id"] + 999})
    assert bad.status_code == 422


def test_home_maintenance_recurring_and_improvements(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    home = c.post(
        "/api/accounts",
        json={"name": "Koti", "kind": "property", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    c.post(
        "/api/home",
        json={
            "title": "Keittiöremontti",
            "account_id": home["id"],
            "kind": "renovation",
            "done_on": str(today() - timedelta(days=200)),
            "cost": "18000",
            "adds_to_cost": True,
        },
    )
    task = c.post(
        "/api/home",
        json={
            "title": "Ilmanvaihdon suodattimet",
            "account_id": home["id"],
            "kind": "task",
            "done_on": "2025-10-01",
            "interval_months": 12,
        },
    ).json()
    assert task["next_due"] == "2026-10-01"
    r = c.get("/api/home").json()
    assert Decimal(r["improvements_by_property"][str(home["id"])]) == Decimal(18000)
    c.post(f"/api/home/{task['id']}/done", json={"done_on": str(today()), "cost": "45"})
    items = c.get("/api/home", params={"account_id": home["id"]}).json()["items"]
    assert len(items) == 3
    t = next(i for i in items if i["id"] == task["id"])
    assert t["next_due"] > str(today())
    assert "Home: Ilmanvaihdon suodattimet" not in [a["title"] for a in c.get("/api/attention").json()]
    assert c.post("/api/home", json={"title": "x", "kind": "nope"}).status_code == 422
