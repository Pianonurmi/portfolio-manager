"""Portfolio analytics: FIFO cost basis, holdings, TWR/XIRR, income, kids (PORT-01..04, KIDS-01)."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.portfolio import lots
from app.modules.portfolio.performance import twr, xirr
from app.platform.db import today

FIX = Path("/srv/fixtures/nordnet")


def _import(c: TestClient) -> dict[str, int]:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    aot = c.post(
        "/api/accounts", json={"name": "Nordnet AOT", "kind": "aot", "external_ref": "12345678", "owners": own}
    ).json()
    ost = c.post(
        "/api/accounts", json={"name": "Nordnet OST", "kind": "ost", "external_ref": "87654321", "owners": own}
    ).json()
    b = c.post("/api/imports", files={"file": ("t.csv", (FIX / "transactions-fi.csv").read_bytes(), "text/csv")}).json()
    c.post(f"/api/imports/{b['id']}/create-instruments")
    c.post(f"/api/imports/{b['id']}/map-type", json={"raw_type": "OMISTUSMUUTOS", "kind": "ignore"})
    assert c.post(f"/api/imports/{b['id']}/commit").json()["committed"] == 21
    return {"aot": aot["id"], "ost": ost["id"], "entity": me["entity_id"]}


def test_fifo_cost_basis_and_realized_gain(auth_client: TestClient, db: Session) -> None:
    acc = _import(auth_client)
    iwda = auth_client.get("/api/instruments", params={"q": "ishares"}).json()[0]["id"]
    book = lots.build(db, [acc["aot"]])
    # 40 @ 62,10 + 9 fee = 2493; sold 10 FIFO (623,25 cost); bought 25 @ 88,04 + 9 = 2210
    assert book.cost_basis(acc["aot"], iwda) == Decimal("4079.75")
    d = next(x for x in book.disposals if x.instrument_id == iwda)
    assert d.quantity == 10 and d.cost == Decimal("623.25")
    assert d.proceeds == Decimal("706.00")  # 10 × 71,50 − 9
    assert d.gain == Decimal("82.75")
    assert not book.warnings


def test_holdings_values_with_prices(auth_client: TestClient) -> None:
    c = auth_client
    _import(c)
    insts = {i["isin"]: i for i in c.get("/api/instruments").json() if i["isin"]}
    on = "2026-10-09"
    for isin, px in {
        "IE00B4L5Y983": "98.42",
        "FI4000552500": "41.18",
        "FI0009013403": "52.96",
        "FI4000415252": "186.40",
        "FI0009000681": "4.62",
    }.items():
        c.post("/api/prices", json={"instrument_id": insts[isin]["id"], "date": on, "close": px})
    c.post(
        "/api/prices",
        json={"instrument_id": insts["US67066G1040"]["id"], "date": on, "close": "182.40", "currency": "USD"},
    )
    from app.modules.pricing.models import FxRate
    from app.platform.db import session_factory

    with session_factory()() as s:
        s.add(FxRate(quote="USD", date=date(2026, 10, 9), rate=Decimal("1.0900")))
        s.commit()
    h = c.get("/api/portfolio/holdings", params={"on": on}).json()
    by = {r["isin"]: r for a in h["accounts"] for r in a["holdings"] if r["isin"]}
    assert by["IE00B4L5Y983"]["value"]["value"] == "5413.10"  # 55 × 98,42
    assert Decimal(by["US67066G1040"]["value"]["value"]) == Decimal("3346.79")  # 20 × 182,40 / 1,09
    assert by["IE00B4L5Y983"]["cost"]["value"] == "4079.75"
    assert by["FI4000415252"]["value"]["stale"] is None or "price" in by["FI4000415252"]["value"]["stale"]
    total = sum(Decimal(r["value"]["value"]) for a in h["accounts"] for r in a["holdings"])
    assert Decimal(h["total"]["value"]) == total
    # provenance chain for a holding
    tree = c.get(f"/api/explain/{by['US67066G1040']['value']['ref']}").json()
    assert tree["formula"] == "quantity × price × FX"
    assert [n["label"] for n in tree["children"]] == ["Quantity", "Price", "FX USD → EUR"]


def test_twr_and_xirr_math() -> None:
    D = Decimal
    # deposit 1000 at start, doubles: +100 % regardless of a later deposit
    assert abs(twr([D(1000), D(2000), D(3000)], [D(0), D(0), D(1000)]) - 1.0) < 1e-9  # type: ignore[operator]
    days = [date(2025, 1, 1), date(2026, 1, 1)]
    assert abs(xirr(days, [D(1000), D(1100)], [D(0), D(0)]) - 0.10) < 1e-6  # type: ignore[operator]


def test_performance_and_income_endpoints(auth_client: TestClient) -> None:
    c = auth_client
    _import(c)
    p = c.get("/api/portfolio/performance", params={"period": "Max"}).json()
    assert set(p["periods"]) >= {"1Y", "Max"}
    assert p["series"] and "contributions" in p["series"][0]
    inc = c.get("/api/portfolio/income", params={"period": "Max"}).json()
    # dividends: 85,00 + 90,00 (Sampo) + 1,02 (NVDA); withholding: 21,68 + 22,95 + 0,15
    assert Decimal(inc["dividends"]["value"]) == Decimal("176.02")
    assert Decimal(inc["withholding"]["value"]) == Decimal("-44.78")
    assert Decimal(inc["interest"]["value"]) == Decimal("1.23")
    tree = c.get(f"/api/explain/{p['periods']['Max']['twr']['ref']}").json() if p["periods"]["Max"]["twr"] else None
    assert tree is None or tree["label"].startswith("Time-weighted")


def test_kids_scope_only_sees_child_accounts(auth_client: TestClient) -> None:
    c = auth_client
    kid = c.post("/api/persons", json={"name": "Child A", "role": "child", "birth_date": "2016-05-01"}).json()
    a = c.post(
        "/api/accounts",
        json={
            "name": "Nordnet AOT · Child A",
            "kind": "aot",
            "owners": [{"entity_id": kid["entity_id"], "share": "1"}],
        },
    ).json()
    start = today() - timedelta(days=400)
    c.post(
        "/api/ledger/transactions",
        json={"account_id": a["id"], "date": start.isoformat(), "kind": "deposit", "amount": "1000"},
    )
    k = c.get("/api/portfolio/kids").json()
    assert k[0]["name"] == "Child A" and k[0]["value"]["value"] == "1000.00"
    assert k[0]["contributions"]["value"] == "1000.00"
    h = c.get("/api/portfolio/holdings", params={"scope": f"entity:{kid['entity_id']}"}).json()
    assert [x["name"] for x in h["accounts"]] == ["Nordnet AOT · Child A"]


def test_split_keeps_cost(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()
    i = c.post("/api/instruments", json={"type": "stock", "name": "NVIDIA", "currency": "USD"}).json()
    c.post(
        "/api/ledger/transactions",
        json={"account_id": a["id"], "date": "2024-01-02", "kind": "deposit", "amount": "5000"},
    )
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": a["id"],
            "date": "2024-01-03",
            "kind": "buy",
            "amount": "-4009",
            "instrument_id": i["id"],
            "quantity": "10",
            "fees": "9",
        },
    )
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": a["id"],
            "date": "2024-06-10",
            "kind": "split",
            "amount": "0",
            "instrument_id": i["id"],
            "quantity": "90",
        },
    )
    book = lots.build(db, [a["id"]])
    lot = book.lots(a["id"], i["id"])[0]
    assert lot.quantity == 100 and lot.cost == Decimal("4009") and lot.acquired == date(2024, 1, 3)


def test_allocation_and_tax_aware_rebalance(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    ost = c.post("/api/accounts", json={"name": "OST", "kind": "ost", "owners": own}).json()
    aot = c.post("/api/accounts", json={"name": "AOT", "kind": "aot", "owners": own}).json()
    eq = c.post(
        "/api/instruments", json={"type": "etf", "name": "World ETF", "currency": "EUR", "asset_class": "equity"}
    ).json()
    for a in (ost, aot):
        c.post(
            "/api/ledger/transactions",
            json={"account_id": a["id"], "date": "2024-01-02", "kind": "deposit", "amount": "5000"},
        )
        c.post(
            "/api/ledger/transactions",
            json={
                "account_id": a["id"],
                "date": "2024-01-03",
                "kind": "buy",
                "amount": "-4500",
                "instrument_id": eq["id"],
                "quantity": "45",
            },
        )
    c.post("/api/prices", json={"instrument_id": eq["id"], "date": "2024-01-03", "close": "100"})
    al = c.get("/api/portfolio/allocation").json()
    assert {i["key"]: i["value"] for i in al["items"]} == {"Equity": "9000.00", "Cash": "1000.00"}
    c.put("/api/portfolio/targets", json={"targets": {"Equity": "0.5", "Cash": "0.5"}, "band": "0.05"})
    r = c.get("/api/portfolio/rebalance", params={"new_money": "1000"}).json()
    buys = [t for t in r["trades"] if t["action"] == "buy"]
    sells = [t for t in r["trades"] if t["action"] == "sell"]
    assert buys[0]["class"] == "Cash" and buys[0]["amount"] == "1000.00"
    assert sells[0]["account"] == "OST" and sells[0]["taxable_gain_estimate"] == "0.00"  # OST first: no tax


def test_income_forecast_uses_yahoo_then_own_dividends(auth_client: TestClient) -> None:
    from app.modules.ledger.service import TxnSpec, record
    from app.modules.pricing.models import Distribution
    from app.platform.db import session_factory

    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()["id"]
    c.post(
        "/api/ledger/transactions", json={"account_id": acct, "date": "2024-01-02", "kind": "deposit", "amount": "5000"}
    )
    ids = {}
    for name in ("Yahoo payer", "Ledger payer", "Non payer"):
        i = c.post("/api/instruments", json={"type": "stock", "name": name, "currency": "EUR"}).json()["id"]
        ids[name] = i
        c.post(
            "/api/ledger/transactions",
            json={
                "account_id": acct,
                "date": "2024-01-03",
                "kind": "buy",
                "amount": "-200",
                "instrument_id": i,
                "quantity": "10",
            },
        )
        c.post("/api/prices", json={"instrument_id": i, "date": today().isoformat(), "close": "20"})
    recent = today() - timedelta(days=60)
    with session_factory()() as s:
        for d, a in ((recent, "0.5"), (recent - timedelta(days=180), "0.5"), (recent - timedelta(days=500), "9")):
            s.add(
                Distribution(
                    instrument_id=ids["Yahoo payer"], ex_date=d, amount=Decimal(a), currency="EUR", source="yahoo"
                )
            )
        record(
            s,
            TxnSpec(
                account_id=acct,
                date=recent,
                kind="dividend",
                currency="EUR",
                amount=Decimal("5"),
                instrument_id=ids["Ledger payer"],
            ),
        )
        s.commit()
    inc = c.get("/api/portfolio/income").json()
    # Yahoo: 10 units × (0.5 + 0.5) = 10; ledger: 5 gross over 10 units = 0.5 each → 5; the 500-day-old 9 is outside
    assert Decimal(inc["forecast_12m"]["value"]) == Decimal("15.00")
    assert inc["forecast_sources"] == {"yahoo": 1, "ledger": 1}
    assert abs(inc["forecast_yield"] - 15 / 600) < 1e-9  # on the 600 € current value
    assert abs(inc["yield_on_cost"] - 15 / 600) < 1e-9  # cost 3 × 200
