from datetime import date
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.modules.planning import montecarlo as mc
from app.modules.portfolio import leverage as lv

RULES = yaml.safe_load(Path("/srv/rules/nordnet_credit-2026.yaml").read_text())
TERMS = lv.Terms.from_rules(RULES, date(2026, 10, 12))


def stocks(n: int, pawn: float = 0.70, value: float = 1000.0) -> list[lv.Pledge]:
    return [lv.Pledge(str(i), f"S{i}", value, "stock", pawn) for i in range(n)]


def test_terms_pick_the_rate_in_force_on_the_day() -> None:
    assert TERMS.rates == {"base": 0.0485, "hopea": 0.0449, "kulta": 0.0394}
    later = lv.Terms.from_rules(RULES, date(2026, 10, 15))
    assert later.rates["kulta"] == 0.0444 and later.rates["hopea"] == 0.0465


def test_diversification_bonus_matches_nordnets_example() -> None:
    """Nordnet: shares with a 70 % rate can reach 85 % when no share is over 10 % of the portfolio (11 shares)."""
    c = lv.collateral(stocks(11), TERMS)
    assert c["largest_share"] == pytest.approx(1 / 11) and c["effective_rate"] == pytest.approx(0.85)
    assert c["collateral_value"] == pytest.approx(11_000 * 0.85)
    # one share at 60 % of the portfolio: no bonus at all
    concentrated = stocks(1, value=6000) + stocks(4)
    assert lv.collateral(concentrated, TERMS)["effective_rate"] == pytest.approx(0.70)


def test_funds_count_as_three_holdings_and_zero_rate_is_left_out() -> None:
    fund = lv.Pledge("f", "Index fund", 3000, "fund", 0.80)
    junk = lv.Pledge("j", "Unlisted", 5000, "stock", 0.0, listed=False)
    c = lv.collateral([fund, *stocks(7, pawn=0.80)], TERMS)
    assert c["largest_share"] == pytest.approx((3000 / 3) / 10_000)  # 10 %: the 80 % row's best column
    assert c["effective_rate"] == pytest.approx(0.85)
    with_junk = lv.collateral([fund, *stocks(7, pawn=0.80), junk], TERMS)
    assert with_junk["collateral_value"] == pytest.approx(c["collateral_value"])
    assert with_junk["pledged_value"] == 10_000 and with_junk["market_value"] == 15_000


def test_tiers_follow_the_three_conditions() -> None:
    ps = stocks(11)
    cv = lv.collateral(ps, TERMS)["collateral_value"]
    assert lv.tier(ps, 0.2 * cv, cv, TERMS)["tier"] == "kulta"
    assert lv.tier(ps, 0.5 * cv, cv, TERMS)["tier"] == "hopea"
    assert lv.tier(ps, 0.7 * cv, cv, TERMS)["tier"] == "base"
    lumpy = stocks(1, value=3000) + stocks(9)  # the largest share is 25 %: too much for Kulta, fine for Hopea
    cv2 = lv.collateral(lumpy, TERMS)["collateral_value"]
    t = lv.tier(lumpy, 0.1 * cv2, cv2, TERMS)
    assert t["tier"] == "hopea" and not t["checks"]["kulta"][0]["ok"]


def test_margin_call_distance() -> None:
    ps = stocks(11)
    cv = lv.collateral(ps, TERMS)["collateral_value"]
    d = lv.margin_call_drop(ps, 0.5 * cv, TERMS)
    assert d["all"] == pytest.approx(0.5)
    assert d["largest"] is None  # one share of eleven cannot fall far enough on its own
    big = stocks(1, value=8000) + stocks(4)
    cvb = lv.collateral(big, TERMS)["collateral_value"]
    db = lv.margin_call_drop(big, 0.7 * cvb, TERMS)
    assert db["largest"] is not None and 0 < db["largest"] < 1
    assert lv.margin_call_drop(ps, 0, TERMS) == {"all": None, "largest": None}


def test_report_and_what_if() -> None:
    ps = stocks(11)
    r = lv.report(ps, 2000, TERMS, deductible=0.30)
    assert r["equity"] == 9000 and r["leverage"] == pytest.approx(11_000 / 9000)
    assert r["tier"]["tier"] == "hopea"  # 2 000 is 21 % of the 9 350 collateral value: over Kulta's 20 %
    assert r["interest"]["gross"] == pytest.approx(2000 * TERMS.rates["hopea"])
    assert r["interest"]["after_tax"] == pytest.approx(r["interest"]["gross"] * 0.7)
    assert [s["margin_call"] for s in r["shocks"]] == [False] * 5
    more = lv.what_if(ps, 2000, 5000, TERMS)
    assert sum(p.value for p in more) == pytest.approx(16_000)
    with pytest.raises(lv.LeverageError):
        lv.what_if(ps, 0, 100, TERMS, into="nope")


# ---- Monte Carlo ------------------------------------------------------------------------------------------------


def _lev(**kw: object) -> mc.Leverage:
    return mc.Leverage(**{"deductible": 0.0, "gain_tax": 0.0, **kw})  # type: ignore[arg-type]


def test_no_loan_changes_nothing() -> None:
    ret = np.random.default_rng(1).normal(0.006, 0.04, (120, 50))
    out, info = mc.levered_returns(ret, _lev(ratio=1.0))
    assert out == pytest.approx(ret) and info["margin_call"] == 0 and info["interest_per_year"] == 0


def test_constant_leverage_doubles_returns_without_interest() -> None:
    ret = np.random.default_rng(2).normal(0.005, 0.03, (36, 20))
    out, _ = mc.levered_returns(ret, _lev(ratio=2.0, rate=0.0, policy="monthly"))
    assert out == pytest.approx(2 * ret, abs=1e-12)


def test_interest_after_tax_is_charged_on_the_loan() -> None:
    ret = np.zeros((12, 3))
    out, info = mc.levered_returns(ret, mc.Leverage(ratio=1.5, rate=0.04, deductible=0.30, gain_tax=0.0))
    assert out[0] == pytest.approx(-0.5 * 0.04 * 0.7 / 12)
    assert info["interest_per_year"] == pytest.approx(0.5 * 0.04 * 0.7, rel=0.02)


def test_tier_rate_follows_the_loan_to_collateral() -> None:
    lev = _lev()
    r = mc.lever_rate(lev, np.array([0.1, 0.5, 0.9]))
    assert list(r) == [0.0444, 0.0465, 0.0485]
    assert list(mc.lever_rate(_lev(diversified=False), np.array([0.1]))) == [0.0485]


def test_crash_triggers_a_margin_call_and_taxes_the_gain() -> None:
    ret = np.zeros((3, 2))
    ret[0] = [-0.30, 0.0]
    lev = mc.Leverage(
        ratio=2.0, rate=0.0, collateral_rate=0.80, restore=0.80, gain_tax=0.30, gain_share=0.5, policy="never"
    )
    out, info = mc.levered_returns(ret, lev)
    # assets 2 → 1.4 against a loan of 1: the collateral value 0.8 · 1.4 = 1.12 still covers it
    assert info["margin_call"] == 0 and out[0, 0] == pytest.approx(-0.60)
    ret[0] = [-0.40, 0.0]
    out, info = mc.levered_returns(ret, lev)
    assert info["margin_call"] == pytest.approx(0.5) and info["forced_tax_per_year"] == 0  # sold at a loss: no tax
    assert out[0, 0] == pytest.approx(-0.80)  # selling to repay does not change own money; it only stops the slide


def test_ruin_and_too_much_leverage() -> None:
    ret = np.zeros((2, 1))
    ret[0] = -0.6
    _, info = mc.levered_returns(ret, _lev(ratio=2.0, rate=0.0, policy="never"))
    assert info["ruin"] == 1.0
    with pytest.raises(ValueError, match="above the collateral value"):
        mc.levered_returns(ret, _lev(ratio=6.0, collateral_rate=0.80))


def test_run_reports_leverage_and_the_sweep() -> None:
    p = mc.Params.from_dict(
        {
            "start_value": 100_000,
            "start_age": 40,
            "horizon_age": 70,
            "paths": 300,
            "contributions": {"monthly": 500},
            "withdrawal": {"start_age": 60},
            "leverage": {"ratio": 1.5},
            "leverage_sweep": [1.0, 1.5, 2.0],
        }
    )
    r = mc.run(p)
    assert r["leverage"]["ratio"] == 1.5 and 0 <= r["leverage"]["margin_call"] <= 1
    sw = {x["ratio"]: x for x in r["leverage_sweep"]}
    assert sw[1.0]["margin_call"] == 0 and sw[1.0]["interest_per_year"] == 0
    # more leverage: a worse bad case and more margin calls; with shares at 7 % ± 16 % and credit at about 4.9 %
    # (3.4 % after tax), 2× does not even raise the median: volatility drag eats the extra return
    assert sw[2.0]["at_withdrawal_real_p10"] < sw[1.5]["at_withdrawal_real_p10"] < sw[1.0]["at_withdrawal_real_p10"]
    # at 1.5× a call needs a 58 % fall between yearly re-levers; at 2× 37.5 % is enough
    assert sw[2.0]["margin_call"] > sw[1.5]["margin_call"] >= 0


# ---- endpoints and the new-money plan ------------------------------------------------------------------------------


def test_leverage_endpoint_with_collateral_rates(auth_client: TestClient) -> None:
    from app.modules.core.models import Account, Instrument
    from app.platform.db import session_factory
    from app.platform.jobs import run_now

    from .test_risk import _prices, _rets, _setup

    run_now("rules.load")
    c = auth_client
    ids, _ = _setup(c, {f"ETF{i}": (_prices(_rets([0.01, -0.005])), 10) for i in range(11)})
    with session_factory()() as db:
        acc = db.scalar(select(Account).where(Account.kind == "aot"))
        assert acc is not None
        for iid in ids.values():
            inst = db.get(Instrument, iid)
            assert inst is not None
            inst.meta = {**inst.meta, "nordnet": {"pawn": 0.70, "symbol": "X", "kind": "etf", "as_of": "2026-10-12"}}
        db.commit()
        aid = acc.id
    assert c.get("/api/portfolio/leverage").status_code == 404  # no account at Nordnet in this test
    d = c.get(f"/api/portfolio/leverage?account_id={aid}").json()
    assert d["now"]["loan"] == 0 and d["now"]["effective_rate"] == pytest.approx(0.85)  # 11 equal holdings
    assert d["collateral_as_of"] == "2026-10-12"
    w = c.get(f"/api/portfolio/leverage?account_id={aid}&extra=5000").json()
    assert (
        w["after"]["loan"] == 5000 and w["after"]["leverage"] > 1 and w["after"]["tier"]["tier"] in ("kulta", "hopea")
    )
    assert w["deductible"] == pytest.approx(0.30)
    first = str(ids["ETF0"])
    assert c.put(f"/api/portfolio/leverage/collateral/{first}", json={"rate": 0.5}).status_code == 200
    assert first in c.get(f"/api/portfolio/leverage?account_id={aid}").json()["manual"]
    assert c.put(f"/api/portfolio/leverage/collateral/{first}", json={"rate": 0.95}).status_code == 422

    mcr = c.post(
        "/api/plan/montecarlo",
        json={"start_value": 100_000, "start_age": 40, "horizon_age": 70, "paths": 200,
              "leverage": {"ratio": 1.3}, "leverage_sweep": [1.0, 1.5]},
    )  # fmt: skip
    assert mcr.status_code == 200, mcr.text
    out = mcr.json()
    assert out["leverage"]["ratio"] == 1.3 and [x["ratio"] for x in out["leverage_sweep"]] == [1.0, 1.5]
    too_much = c.post(
        "/api/plan/montecarlo",
        json={"start_value": 1000, "paths": 100, "leverage": {"ratio": 9, "collateral_rate": 0.5}},
    )
    assert too_much.status_code == 422 and "collateral value" in too_much.json()["detail"]


def test_new_money_repays_credit_first_when_asked() -> None:
    from app.modules.portfolio import newmoney as nm

    book = [nm.Position("w", "World", 50_000, {"Equity": 1.0}, buyable=True)]
    base = {"targets": {"Equity": 1.0}, "monthly": 1000, "months": 12, "loan": 5000, "loan_rate": 0.034, "min_trade": 0}
    keep = nm.plan(book, nm.Params(**base), date(2026, 11, 1))  # type: ignore[arg-type]
    pay = nm.plan(book, nm.Params(**base, repay_first=True), date(2026, 11, 1))  # type: ignore[arg-type]
    assert keep["loan"]["end"] == pytest.approx(5000 * (1 + 0.034 / 12) ** 12, rel=1e-9)
    assert pay["loan"]["end"] == 0 and pay["first_month"] == []  # the first months all go to the loan
    assert pay["loan"]["interest_after_tax"] < keep["loan"]["interest_after_tax"]
    assert any("credit costs" in n for n in pay["notes"])


def test_loan_is_repaid_at_the_stop_and_the_gain_taxed() -> None:
    ret = np.full((24, 2), 0.01)
    lev = mc.Leverage(ratio=1.5, rate=0.0, deductible=0.0, gain_tax=0.30, gain_share=0.0, policy="never")
    out, info = mc.levered_returns(ret, lev, stop=12)
    assert out[12:] == pytest.approx(ret[12:])  # unlevered after the stop
    assert info["levered_years"] == 1.0 and info["forced_tax_per_year"] > 0  # selling the borrowed half realised gains
    plain, _ = mc.levered_returns(ret, mc.Leverage(**{**lev.__dict__, "gain_tax": 0.0}), stop=12)
    assert out[11, 0] < plain[11, 0]  # the tax comes out of own money in the repayment month


def test_home_equity_and_harvest_endpoints(auth_client: TestClient) -> None:
    from app.modules.portfolio import financing
    from app.platform.jobs import run_now

    run_now("rules.load")
    c = auth_client
    r = c.post(
        "/api/portfolio/financing/home-equity", json={"amount": 50_000, "home_value": 300_000, "mortgage_now": 150_000}
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mortgage"]["room"] == pytest.approx(75_000) and d["mortgage"]["fits"]
    assert d["mortgage"]["rate_after_tax"] == pytest.approx(d["mortgage"]["rate"] * 0.7)
    assert d["nordnet"]["rate"] == 0.0485  # no Nordnet account in the test: the base rate
    assert financing.annuity(120_000, 0.0, 10) == pytest.approx(1000)
    assert financing.annuity(100_000, 0.06, 1) == pytest.approx(8606.64, abs=0.01)
    assert c.get("/api/tax/harvest-plan").status_code == 422  # taxes are per person
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    hr = c.get(f"/api/tax/harvest-plan?scope=e{me['entity_id']}")
    assert hr.status_code == 200, hr.text
    h = hr.json()
    assert h["candidates"] == [] and any("wash-sale" in n for n in h["notes"])


def test_mortgage_leverage_has_no_margin_calls() -> None:
    ret = np.zeros((3, 1))
    ret[0] = -0.5
    lev = mc.Leverage(ratio=2.0, rate=0.0, policy="never", margin_calls=False, collateral_rate=0.5)
    out, info = mc.levered_returns(ret, lev)
    assert info["margin_call"] == 0 and info["ruin"] == 1.0  # nothing is sold, yet a halving wipes out 2× equity
