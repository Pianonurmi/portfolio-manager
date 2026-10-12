"""Tax-aware withdrawal order and the new-money comparison (PLAN-09)."""

import pytest
from fastapi.testclient import TestClient

from app.modules.planning.withdrawals import Buckets, Plan, TaxRules, _withdraw, compare, new_money, simulate
from app.platform.db import today
from app.platform.jobs import run_now
from tests.test_portfolio import _import

T = TaxRules()


def test_gross_up_per_bucket() -> None:
    # OST with half of the value as deposits: 15 % of each euro is lost to tax → 8 500 net needs 10 000 gross
    b = Buckets(wrapper=100_000, wrapper_deposits=50_000)
    gross, tax = _withdraw(b, 8_500, "wrappers_first", 0, T)
    assert gross == pytest.approx(10_000, abs=1) and tax == pytest.approx(1_500, abs=1)
    assert b.wrapper == pytest.approx(90_000, abs=1) and b.wrapper_deposits == pytest.approx(45_000, abs=1)
    # taxable with zero cost, held < 10 years: deemed cost 20 % → 24 % tax per euro sold
    b = Buckets(taxable=100_000, taxable_cost=0)
    gross, tax = _withdraw(b, 7_600, "taxable_first", 0, T)
    assert gross == pytest.approx(10_000, abs=1) and tax == pytest.approx(2_400, abs=1)
    # after 10 years the deemed cost is 40 %: 18 % tax per euro
    b = Buckets(taxable=100_000, taxable_cost=0, taxable_age=10)
    gross, _ = _withdraw(b, 8_200, "taxable_first", 0, T)
    assert gross == pytest.approx(10_000, abs=1)
    # cost above the price: the loss offsets the OST gain in the same year
    b = Buckets(taxable=10_000, taxable_cost=20_000, wrapper=100_000, wrapper_deposits=0)
    gross, tax = _withdraw(b, 30_000, "proportional", 0, T)
    assert gross - tax == pytest.approx(30_000, abs=1)


def test_cash_only_depletes_when_expected() -> None:
    plan = Plan(
        age_now=60,
        retire_age=60,
        horizon_age=70,
        real_return=0.0,
        monthly_spending=1000,
        pension_monthly=0,
        pension_age=99,
    )
    r = simulate(plan, Buckets(cash=60_000), "taxable_first", T)
    assert r["total_tax"] == 0 and r["depleted_age"] == 66  # 12 000 a year from 61 on: 61…65 paid, 66 short


def test_compare_runs_every_strategy() -> None:
    plan = Plan(
        age_now=55,
        retire_age=58,
        horizon_age=90,
        real_return=0.04,
        monthly_spending=3000,
        pension_monthly=1500,
        pension_age=65,
        monthly_savings=1000,
    )
    start = Buckets(
        cash=10_000, taxable=300_000, taxable_cost=150_000, taxable_age=6, wrapper=100_000, wrapper_deposits=60_000
    )
    r = compare(plan, start, T)
    assert {x["strategy"] for x in r["runs"]} == {"taxable_first", "wrappers_first", "proportional", "gains_budget"}
    assert r["best"] in {x["strategy"] for x in r["runs"]}
    taxes = {x["strategy"]: x["total_tax"] for x in r["runs"]}
    assert len(set(taxes.values())) > 1  # the order matters


def test_new_money_lots_and_ost_cap() -> None:
    # one year, no growth: nothing to tax; the OST takes 1 000 and the rest overflows to the book-entry account
    opts = {o["destination"]: o for o in new_money(100, 1, 0.0, T, ost_room=1_000)}
    assert opts["Osakesäästötili"]["value"] == 1200 and "200 €" in opts["Osakesäästötili"]["note"]
    # no OST room: everything overflows, so the OST option equals the accumulating ETF
    opts = {o["destination"]: o for o in new_money(500, 20, 0.05, T, ost_room=0, loan_rate=0.04)}
    assert opts["Osakesäästötili"]["value"] == opts["Book-entry account, accumulating ETF"]["value"]
    # a 4 % loan is 1,96 % after inflation: below a 5 % real market return even after tax
    assert opts["Extra mortgage repayment"]["value"] < opts["Book-entry account, accumulating ETF"]["value"]
    # selling everything in one year reaches the 34 % bracket; yearly dividend tax (25,5 %) can come out ahead
    opts = {o["destination"]: o for o in new_money(500, 20, 0.05, T, ost_room=100_000, dividend_yield=0.04)}
    assert (
        abs(
            opts["Book-entry account, dividend stocks"]["value"] / opts["Book-entry account, accumulating ETF"]["value"]
            - 1
        )
        < 0.02
    )


def test_withdrawals_api_uses_real_buckets(auth_client: TestClient) -> None:
    run_now("rules.load")
    acc = _import(auth_client)
    c = auth_client
    insts = {i["isin"]: i for i in c.get("/api/instruments").json() if i["isin"]}
    on = today().isoformat()
    for isin, px in {
        "IE00B4L5Y983": "98.42",
        "FI4000552500": "41.18",
        "FI0009013403": "52.96",
        "FI4000415252": "186.40",
        "FI0009000681": "4.62",
    }.items():
        c.post("/api/prices", json={"instrument_id": insts[isin]["id"], "date": on, "close": px})
    params = auth_client.get("/api/plan/base").json()["params"]
    r = auth_client.post("/api/plan/withdrawals", json={"params": params}).json()
    assert r["buckets"]["taxable"] > 0 and r["buckets"]["wrapper"] > 0
    assert r["buckets"]["wrapper_deposits"] == 15000.0  # OST deposits from the fixture
    nm = auth_client.post(
        "/api/plan/new-money", json={"monthly": 500, "years": 15, "scope": f"entity:{acc['entity']}"}
    ).json()
    assert nm["ost_room"] == 85000.0 and len(nm["options"]) == 2
