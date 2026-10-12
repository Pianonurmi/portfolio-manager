"""Finnish tax engine on the Nordnet fixture (TAX-01..08)."""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.platform.jobs import run_now
from tests.test_portfolio import _import


def _ready(c: TestClient) -> dict[str, int]:
    run_now("rules.load")
    return _import(c)


def test_year_with_small_sale_is_exempt(auth_client: TestClient) -> None:
    acc = _ready(auth_client)
    y = auth_client.get("/api/tax/year", params={"scope": f"entity:{acc['entity']}", "year": 2023}).json()
    sale = y["sales"][0]
    # IWDA 10 × 71,50: gross 715, net 706, FIFO cost 623,25 → actual gain 82,75 (deemed 20 % would give 572)
    assert sale["gross"] == "715.00" and sale["method"] == "actual cost" and sale["gain"] == "82.75"
    assert y["small_disposals_exempt"] is True and y["taxable_gains"] == "0.00"
    # dividends 2023 in the AOT: Sampo 90,00 gross (NVDA dividend is inside the OST)
    assert y["dividends"]["gross"] == "90.00" and y["taxable_dividends"] == "76.50"
    assert y["prepaid"] == "22.95" and y["rules_version"] == "TAX-2017.2"


def test_ost_sale_is_not_taxed_and_ost_status(auth_client: TestClient) -> None:
    acc = _ready(auth_client)
    y = auth_client.get("/api/tax/year", params={"scope": f"entity:{acc['entity']}", "year": 2025}).json()
    assert y["sales"] == []  # Nokia sold inside the OST
    ost = auth_client.get("/api/tax/ost", params={"scope": f"entity:{acc['entity']}"}).json()
    assert ost[0]["deposits"] == "15000.00" and ost[0]["cap_left"] == "85000.00"


def test_household_scope_rejected(auth_client: TestClient) -> None:
    _ready(auth_client)
    assert auth_client.get("/api/tax/year").status_code == 422


def test_gifts_window_and_calculators(auth_client: TestClient) -> None:
    c = auth_client
    run_now("rules.load")
    kid = c.post("/api/persons", json={"name": "Child A", "role": "child"}).json()
    c.post(
        "/api/tax/gifts",
        json={"donor": "Alex", "recipient_entity_id": kid["entity_id"], "date": "2025-12-24", "amount": "3600"},
    )
    w = c.post(
        "/api/tax/gifts",
        json={"donor": "Alex", "recipient_entity_id": kid["entity_id"], "date": "2026-06-01", "amount": "1500"},
    ).json()
    win = w["window"][0]
    # from 2026 gifts below 7 500 € per donor within three years are tax-free
    assert win["total"] == "5100.00" and win["left"] == "2400.00" and win["tax_if_now"] == "0.00"
    w = c.post(
        "/api/tax/gifts",
        json={"donor": "Alex", "recipient_entity_id": kid["entity_id"], "date": "2026-09-01", "amount": "3000"},
    ).json()
    assert w["window"][0]["tax_if_now"] == "148.00"  # 8 100: 100 + 8 % × 600
    assert (
        c.get("/api/tax/calc/inheritance", params={"amount": "150000"}).json()["tax"] == "14500.00"
    )  # 2026 scale: 2 800 + 13 % × 90 000
    assert (
        c.get("/api/tax/calc/capital-income", params={"amount": "40000"}).json()["tax"] == "12400.00"
    )  # 9 000 + 3 400


def test_loss_carry_forward(auth_client: TestClient) -> None:
    from app.modules.portfolio.valuation import Scope
    from app.modules.tax.service import year_summary
    from app.platform.db import session_factory

    c = auth_client
    run_now("rules.load")
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()
    i = c.post("/api/instruments", json={"type": "stock", "name": "X", "currency": "EUR"}).json()
    tx = lambda **k: c.post("/api/ledger/transactions", json={"account_id": a["id"], **k})  # noqa: E731
    tx(date="2023-01-02", kind="deposit", amount="10000")
    tx(date="2023-01-03", kind="buy", amount="-5000", instrument_id=i["id"], quantity="100")
    tx(
        date="2023-06-01", kind="sell", amount="3000", instrument_id=i["id"], quantity="100"
    )  # loss 2 000, proceeds > 1 000
    tx(date="2024-01-03", kind="buy", amount="-2000", instrument_id=i["id"], quantity="100")
    tx(date="2024-06-01", kind="sell", amount="5000", instrument_id=i["id"], quantity="100")  # gain 3 000
    with session_factory()() as db:
        y23 = year_summary(db, Scope(me["entity_id"]), 2023)
        y24 = year_summary(db, Scope(me["entity_id"]), 2024)
    assert y23["loss_carry_out"] == Decimal("2000") and y23["tax"] == 0
    assert (
        y24["loss_carry_in"] == Decimal("2000")
        and y24["capital_income"] == Decimal("1000")
        and y24["tax"] == Decimal("300.00")
    )


def test_small_disposals_loss_rule() -> None:
    from datetime import date

    from app.modules.tax.service import Sale, small_disposals

    rules = {
        "capital_income": {"small_disposals_exempt_total_proceeds": 1000, "small_disposals_loss_limit_total_cost": 1000}
    }

    def sale(gross: str, cost: str) -> Sale:
        g, c = Decimal(gross), Decimal(cost)
        d = date(2026, 1, 2)
        return Sale("a", "x", d, d, Decimal(1), g, g, c, Decimal(0), "actual cost", g - c, Decimal(1))

    assert small_disposals([sale("900", "500")], rules) == (True, False)  # small gain: tax-free
    assert small_disposals([sale("900", "950")], rules) == (True, False)  # small loss: not deductible
    assert small_disposals([sale("900", "3000")], rules) == (True, True)  # cost over 1 000 €: loss deductible
    assert small_disposals([sale("1200", "1500")], rules) == (False, True)


def _aot(c: TestClient) -> tuple[int, int]:
    run_now("rules.load")
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()
    return me["entity_id"], a["id"]


def _book(account_id: int, *specs: dict[str, object]) -> None:
    from datetime import date

    from app.modules.ledger.service import TxnSpec, record
    from app.platform.db import session_factory

    with session_factory()() as db:
        for s in specs:
            d = date.fromisoformat(str(s.pop("date")))
            record(db, TxnSpec(account_id=account_id, date=d, currency="EUR", **s))  # type: ignore[arg-type]
        db.commit()


def _summary(entity: int, year: int) -> dict[str, object]:
    from app.modules.portfolio.valuation import Scope
    from app.modules.tax.service import year_summary
    from app.platform.db import session_factory

    with session_factory()() as db:
        return year_summary(db, Scope(entity), year)


def test_loss_is_first_deducted_from_the_years_dividends(auth_client: TestClient) -> None:
    c = auth_client
    entity, acc = _aot(c)
    x = c.post("/api/instruments", json={"type": "stock", "name": "X", "currency": "EUR"}).json()["id"]
    sampo = c.post(
        "/api/instruments", json={"type": "stock", "name": "Sampo", "currency": "EUR", "isin": "FI4000552500"}
    ).json()["id"]
    D = Decimal
    _book(
        acc,
        {"date": "2023-01-02", "kind": "deposit", "amount": D(10000)},
        {"date": "2023-01-03", "kind": "buy", "amount": D(-5000), "instrument_id": x, "quantity": D(100)},
        {"date": "2023-06-01", "kind": "sell", "amount": D(3000), "instrument_id": x, "quantity": D(100)},
        {"date": "2023-05-01", "kind": "dividend", "amount": D(1000), "instrument_id": sampo},
        {"date": "2024-01-03", "kind": "buy", "amount": D(-2000), "instrument_id": x, "quantity": D(100)},
        {"date": "2024-06-01", "kind": "sell", "amount": D(5000), "instrument_id": x, "quantity": D(100)},
    )
    # 2023: loss 2 000 less taxable dividends 850 → 1 150 carried forward, not 2 000
    assert _summary(entity, 2023)["loss_carry_out"] == D("1150.00")
    y24 = _summary(entity, 2024)
    assert y24["loss_carry_in"] == D("1150.00") and y24["capital_income"] == D("1850.00")


def test_losses_older_than_the_window_take_their_share_first(auth_client: TestClient) -> None:
    c = auth_client
    entity, acc = _aot(c)
    x = c.post("/api/instruments", json={"type": "stock", "name": "X", "currency": "EUR"}).json()["id"]
    D = Decimal

    def trade(year: int, cost: int, proceeds: int) -> list[dict[str, object]]:
        return [
            {"date": f"{year}-01-03", "kind": "buy", "amount": D(-cost), "instrument_id": x, "quantity": D(100)},
            {"date": f"{year}-06-01", "kind": "sell", "amount": D(proceeds), "instrument_id": x, "quantity": D(100)},
        ]

    _book(
        acc,
        {"date": "2017-01-02", "kind": "deposit", "amount": D(10000)},
        *trade(2017, 3000, 2000),  # loss 1 000, usable until 2022
        *trade(2019, 3000, 2000),  # loss 1 000, usable until 2024
        *trade(2020, 2000, 3500),  # gain 1 500: uses the 2017 loss first, then 500 of 2019's
        *trade(2024, 1000, 2000),  # gain 1 000
    )
    y24 = _summary(entity, 2024)
    assert y24["loss_carry_in"] == D("500") and y24["capital_income"] == D("500")


def test_foreign_credit_is_capped_at_the_treaty_rate(auth_client: TestClient) -> None:
    c = auth_client
    entity, acc = _aot(c)
    inst = lambda name, isin: c.post(  # noqa: E731
        "/api/instruments", json={"type": "stock", "name": name, "currency": "EUR", "isin": isin}
    ).json()["id"]
    us, ch = inst("Apple", "US0378331005"), inst("Nestle", "CH0038863350")
    D = Decimal
    _book(
        acc,
        {"date": "2024-05-02", "kind": "dividend", "amount": D(850), "tax": D(150), "instrument_id": us},
        {"date": "2024-05-03", "kind": "dividend", "amount": D(650), "tax": D(350), "instrument_id": ch},
    )
    y = _summary(entity, 2024)
    assert y["tax"] == D("510.00")  # 30 % of 85 % × 2 000
    rows = {r["country"]: r for r in y["foreign_tax_by_country"]}  # type: ignore[attr-defined]
    assert rows["US"]["credit"] == D("150.00") and rows["US"]["reclaimable"] == D("0.00")
    # Switzerland withholds 35 %; Finland credits the 10 % treaty rate and the rest is reclaimed from Switzerland
    assert rows["CH"]["credit"] == D("100.00") and rows["CH"]["reclaimable"] == D("250.00")
    assert y["foreign_tax_credit"] == D("250.00") and y["estimate_due"] == D("260.00")
