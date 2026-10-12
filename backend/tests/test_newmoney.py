from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.modules.core.models import Instrument
from app.modules.portfolio import newmoney as nm
from app.platform.db import session_factory
from app.platform.jobs import run_now

from .test_risk import _prices, _rets, _setup

START = date(2026, 1, 1)
TARGETS = {"Equity": 0.7, "Fixed income": 0.2, "Cash": 0.1}


def _book(**kw: object) -> list[nm.Position]:
    world = nm.Position("w", "World", 90_000, {"Equity": 1.0}, {"North America": 0.7, "Eurozone": 0.3}, 0.002)
    bond = nm.Position("b", "Bond", 5_000, {"Fixed income": 1.0}, {}, 0.001)
    mixed = nm.Position("m", "80/20", 0, {"Equity": 0.8, "Fixed income": 0.2}, {"North America": 0.8}, 0.0025)
    cash = nm.Position("cash", "Cash", 5_000, {"Cash": 1.0}, {}, 0.0)
    out = [world, bond, mixed, cash]
    for p in out:
        p.buyable = True
        p.held = p.value > 0
    return out


def test_allocate_spends_exactly_and_never_sells() -> None:
    s = np.array([[1.0, 0, 0], [0, 1.0, 0], [0.8, 0.2, 0], [0, 0, 1.0]])
    x = nm.allocate(np.array([90_000.0, 5_000, 5_000]), s, np.array([0.7, 0.2, 0.1]), 1_000, np.full(4, 0.25))
    assert x.sum() == pytest.approx(1_000) and (x >= 0).all()
    assert x[1] == pytest.approx(1_000, abs=1)  # all of it into the most underweight class


def test_allocate_reaches_the_target_mix_once_the_gap_is_closed() -> None:
    """On target already: the money is bought in the target proportions, so the mix stays put."""
    s = np.eye(3)
    x = nm.allocate(np.array([70.0, 20, 10]), s, np.array([0.7, 0.2, 0.1]), 100, np.full(3, 1 / 3))
    assert x == pytest.approx([70, 20, 10], abs=0.05)


def test_mixed_fund_counts_by_what_it_holds() -> None:
    """A 60/40 target with only an 80/20 fund and a bond fund: 2/3 of the money in the mixed fund, 1/3 bonds."""
    s = np.array([[0.8, 0.2], [0.0, 1.0]])
    x = nm.allocate(np.zeros(2), s, np.array([0.6, 0.4]), 900, np.full(2, 0.5))
    assert x == pytest.approx([675, 225], abs=0.5)  # 0.8·675 = 540 = 60 % of 900


def test_preference_splits_between_equivalent_funds() -> None:
    s = np.array([[1.0, 0], [1.0, 0], [0, 1.0]])
    held = nm.allocate(np.array([70.0, 30]), s, np.array([0.7, 0.3]), 100, np.array([0.6, 0.2, 0.2]))
    assert held[0] + held[1] == pytest.approx(70, abs=0.1)
    assert held[0] / held[1] == pytest.approx(3, rel=0.02)  # in proportion to the preference


def test_min_trade_folds_small_buys_into_the_rest() -> None:
    s = np.eye(3)
    x = nm.allocate(np.array([70.0, 19, 10]), s, np.array([0.7, 0.2, 0.1]), 100, np.full(3, 1 / 3), min_trade=30)
    assert x.sum() == pytest.approx(100) and ((x == 0) | (x >= 30)).all()
    one = nm.allocate(np.array([70.0, 20, 10]), s, np.array([0.7, 0.2, 0.1]), 20, np.full(3, 1 / 3), min_trade=50)
    assert sorted(one) == pytest.approx([0, 0, 20])  # less than one trade: everything into a single fund


def test_region_targets_steer_money_between_equity_funds() -> None:
    s = np.array([[1.0], [1.0]])
    reg_split = np.array([[0.7, 0.3], [0.0, 1.0]])  # a world fund and a Europe fund
    regions = (np.array([80.0, 20.0]), reg_split, np.array([0.6, 0.4]), np.array([1.0, 1.0]))
    x = nm.allocate(np.array([100.0]), s, np.array([1.0]), 50, np.array([0.5, 0.5]), regions=regions)
    # after: NA 80 + 0.7a, EU 20 + 0.3a + b, a + b = 50; NA = 60 % of 150 = 90 → a ≈ 14.3
    assert x[0] == pytest.approx(100 / 7, abs=0.5) and x.sum() == pytest.approx(50)


def test_plan_reports_time_to_band_and_scenarios() -> None:
    p = nm.Params(targets=TARGETS, monthly=1_000, months=60)
    r = nm.plan(_book(), p, START)
    assert r["drift_now"] == pytest.approx(0.2)
    assert r["first_month"][0]["key"] == "b"
    assert 12 < r["months_to_band"] < r["months_to_target"] < 60
    assert r["spent"] == pytest.approx(60_000)
    sc = {s["key"]: s for s in r["scenarios"]}
    # after a share crash the portfolio is already closer to target, after a rally further away
    assert (
        sc["equity_crash"]["months_to_band"] < sc["expected"]["months_to_band"] < sc["equity_rally"]["months_to_band"]
    )
    assert sc["flat"]["end_value"] == pytest.approx(160_000)
    need = r["needed_monthly"]
    assert need is not None and need > 1_000
    faster = nm.plan(_book(), nm.Params(targets=TARGETS, monthly=need, months=12), START)
    assert faster["months_to_band"] is not None and faster["months_to_band"] <= 11
    slower = nm.plan(_book(), nm.Params(targets=TARGETS, monthly=need - 50, months=12), START)
    assert slower["months_to_band"] is None


def test_unreachable_target_is_explained_and_variants_compare() -> None:
    p = nm.Params(targets=TARGETS, monthly=1_000, months=36)
    r = nm.plan(_book(), p, START, variants=[("80/20 only", {"m"}), ("Separate funds", {"w", "b", "cash"})])
    v = {x["name"]: x for x in r["variants"]}
    assert v["80/20 only"]["months_to_band"] is None and v["80/20 only"]["best_gap"] == pytest.approx(0.1, abs=1e-3)
    assert v["Separate funds"]["months_to_band"] is not None and v["Separate funds"]["best_gap"] < 1e-3
    only_mixed = [nm.Position(**{**pos.__dict__, "buyable": pos.key == "m"}) for pos in _book()]
    r2 = nm.plan(only_mixed, p, START)
    assert any("cannot make the target mix" in n for n in r2["notes"]) and r2["needed_monthly"] is None


def test_plan_validates_inputs() -> None:
    with pytest.raises(nm.PlanError):
        nm.plan(_book(), nm.Params(targets=TARGETS, monthly=-1), START)
    with pytest.raises(nm.PlanError):
        nm.plan(_book(), nm.Params(targets={"Equity": 0}), START)
    with pytest.raises(nm.PlanError):
        nm.plan([nm.Position("w", "World", 1, {"Equity": 1.0})], nm.Params(targets=TARGETS), START)


# ---- endpoint --------------------------------------------------------------------------------------------------


def test_new_money_endpoint(auth_client: TestClient) -> None:
    c = auth_client
    run_now("rules.load")
    ids, _ = _setup(
        c,
        {
            "World": (_prices(_rets([0.01, -0.005])), 80),
            "Bonds": (_prices(_rets([0.002, 0.001])), 10),
        },
    )
    c.put(f"/api/instruments/{ids['Bonds']}/profile", json={"data": {"asset_classes": {"bond": 1.0}}})
    # a candidate fund that is not owned yet: 80 % shares, 20 % bonds
    mixed = c.post("/api/instruments", json={"currency": "EUR", "type": "etf", "name": "Mixed 80"}).json()["id"]
    c.put(f"/api/instruments/{mixed}/profile", json={"data": {"asset_classes": {"equity": 0.8, "bond": 0.2}}})
    with session_factory()() as db:
        inst = db.get(Instrument, mixed)
        assert inst is not None
        inst.meta = {**inst.meta, "candidate": True}
        db.commit()

    d = c.get("/api/portfolio/new-money").json()
    funds = {f["key"]: f for f in d["funds"]}
    assert funds[str(mixed)]["held"] is False and funds[str(mixed)]["buyable"] is True
    assert funds[str(mixed)]["split"] == {"Equity": 0.8, "Fixed income": 0.2}

    body = {"monthly": 500, "targets": {"Equity": 0.6, "Fixed income": 0.4}, "months": 24, "min_trade": 0}
    r = c.post("/api/portfolio/new-money", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["first_month"][0]["key"] == str(ids["Bonds"])
    assert sum(b["amount"] for b in p["first_month"]) == pytest.approx(500)
    assert p["sell_alternative"]["sold"] > 0  # selling would get there today, at a tax cost
    assert c.get("/api/portfolio/new-money").json()["saved"]["monthly"] == 500

    only = c.post("/api/portfolio/new-money", json={**body, "funds": [str(mixed)]}).json()
    assert all(b["key"] == str(mixed) for b in only["first_month"])
    assert only["best_gap"] == pytest.approx(0.2, abs=1e-3)
    assert c.post("/api/portfolio/new-money", json={**body, "funds": ["x"]}).status_code == 422
    assert c.post("/api/portfolio/new-money", json={**body, "funds": []}).status_code == 422
    assert c.post("/api/portfolio/new-money", json={**body, "prefer": "random"}).status_code == 422


# ---- fees and glide paths ----------------------------------------------------------------------------------------


def _etf_and_fund() -> list[nm.Position]:
    etf = nm.Position("e", "World ETF", 50_000, {"Equity": 1.0}, type="etf", buyable=True)
    fund = nm.Position("f", "World index fund", 0, {"Equity": 1.0}, type="fund", buyable=True, held=False)
    bond = nm.Position("b", "Bond ETF", 0, {"Fixed income": 1.0}, type="etf", buyable=True, held=False)
    return [etf, fund, bond]


def test_small_etf_buys_wait_until_the_fee_is_worth_it() -> None:
    """2.5 € per ETF buy and at most 1 % in fees: an ETF is bought only from 250 €, so 100 € a month waits."""
    book = [p for p in _etf_and_fund() if p.key != "f"]
    p = nm.Params(targets={"Equity": 1.0}, monthly=100, months=12, min_trade=0, fees={"etf": (2.5, 0.0)})
    r = nm.plan(book, p, START)
    buys = [m["buys"] for m in r["schedule"]]
    assert [bool(b) for b in buys] == [False, False, True] * 4  # 100, 200, then 300 € in one go
    assert buys[2]["e"] == pytest.approx(300 + 300 * 0.0, rel=0.01)
    assert r["fees"] == pytest.approx(4 * 2.5) and r["months_waited"] == 8
    assert r["fee_share"] == pytest.approx(10 / 1200)
    assert r["first_month"] == [] and r["first_month_waiting"] == pytest.approx(100)


def test_fee_free_fund_takes_the_small_amounts() -> None:
    p = nm.Params(targets={"Equity": 1.0}, monthly=100, months=6, min_trade=0, fees={"etf": (2.5, 0.0)})
    r = nm.plan(_etf_and_fund(), p, START)
    assert all(set(m["buys"]) == {"f"} for m in r["schedule"])  # the ETF would cost 2.5 % per buy
    assert r["fees"] == 0 and r["months_waited"] == 0
    big = nm.plan(
        _etf_and_fund(),
        nm.Params(targets={"Equity": 1.0}, monthly=2_000, months=1, min_trade=0, fees={"etf": (2.5, 0.0)}),
        START,
    )
    assert big["first_month"][0]["key"] == "e" and big["first_month"][0]["fee"] == pytest.approx(2.5)


def test_fees_reduce_what_is_invested() -> None:
    book = [p for p in _etf_and_fund() if p.key == "e"]
    r = nm.plan(
        book,
        nm.Params(targets={"Equity": 1.0}, monthly=1_000, months=1, min_trade=0, fees={"etf": (2.5, 0.001)}),
        START,
    )
    # 1 000 € leaves the account; 2.5 € fixed and 0.1 % of the rest go to fees
    assert r["fees"] == pytest.approx(2.5 + (1_000 - 2.5) * 0.001 / 1.001, rel=1e-6)
    assert r["schedule"][0]["value"] == pytest.approx(50_000 + 1_000 - r["fees"], rel=1e-9)


def test_target_on_joins_glide_points() -> None:
    g = [
        (date(2030, 1, 1), {"Equity": 0.9, "Fixed income": 0.1}),
        (date(2040, 1, 1), {"Equity": 0.5, "Fixed income": 0.5}),
    ]
    assert nm.target_on(g, date(2026, 1, 1)) == {"Equity": 0.9, "Fixed income": 0.1}
    mid = nm.target_on(g, date(2035, 1, 1))
    assert mid["Equity"] == pytest.approx(0.7, abs=0.001) and mid["Fixed income"] == pytest.approx(0.3, abs=0.001)
    assert nm.target_on(g, date(2050, 1, 1))["Equity"] == 0.5


def test_plan_follows_the_glide_path() -> None:
    glide = [(START, {"Equity": 0.9, "Fixed income": 0.1}), (date(2028, 1, 1), {"Equity": 0.6, "Fixed income": 0.4})]
    p = nm.Params(targets={"Equity": 0.9, "Fixed income": 0.1}, monthly=3_000, months=36, glide=glide, min_trade=0)
    r = nm.plan(_book(), p, START)
    first, last = r["schedule"][0], r["schedule"][-1]
    assert first["targets"]["Equity"] == pytest.approx(0.9 / 1.0, abs=0.01)
    assert last["targets"]["Equity"] == pytest.approx(0.6, abs=0.01)
    assert last["weights"]["Fixed income"] > 0.3  # bought bonds to follow the path down
    assert any("glide path" in n for n in r["notes"])


def test_one_agreement_fee_covers_up_to_four_etfs_a_month() -> None:
    etfs = [nm.Position(f"e{i}", f"ETF {i}", 10_000, {"Equity": 1.0}, type="etf", buyable=True) for i in range(5)]
    p = nm.Params(
        targets={"Equity": 1.0}, monthly=2_000, months=1, min_trade=0, prefer="equal", fees={"etf": (2.5, 0.0)}
    )
    r = nm.plan(etfs[:4], p, START)
    assert len(r["first_month"]) == 4 and r["fees"] == pytest.approx(2.5)  # one agreement for all four
    five = nm.plan(etfs, p, START)
    assert len(five["first_month"]) == 5 and five["fees"] == pytest.approx(5.0)  # a second agreement
    assert sum(b["fee"] for b in five["first_month"]) == pytest.approx(5.0)
