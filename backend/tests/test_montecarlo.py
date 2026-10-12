"""Contribution/withdrawal Monte Carlo: closed forms in a zero-volatility world, determinism, rules, endpoints."""

import time
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.modules.planning import montecarlo as mc
from app.modules.portfolio import risk
from app.platform.db import today

ZERO_VOL = {"mean": mc.DEFAULT_MEAN, "vol": dict.fromkeys(mc.CLASSES, 0.0), "corr": np.eye(4).tolist()}


def params(**kw: Any) -> mc.Params:
    """Equity only, 5 % a year with no volatility, no inflation, no fees, 200 paths."""
    d: dict[str, Any] = {
        "start_value": 100_000,
        "start_age": 40,
        "horizon_age": 90,
        "mix": {"equity": 1.0},
        "inflation": 0.0,
        "assumptions": {**ZERO_VOL, "mean": {**mc.DEFAULT_MEAN, "equity": 0.05}},
        "paths": 200,
    }
    d.update(kw)
    return mc.Params.from_dict(d)


def test_zero_vol_matches_annuity_future_value() -> None:
    p = params(contributions={"monthly": 500, "yearly_increase": 0.0}, withdrawal={"start_age": 60})
    res = mc.run(p)
    g = 1.05 ** (1 / 12) - 1
    n = 20 * 12
    fv = 100_000 * (1 + g) ** n + 500 * (((1 + g) ** n - 1) / g) * (1 + g)  # paid at month start
    assert abs(res["nominal"]["p50"][20] / fv - 1) < 0.005
    assert res["nominal"]["p10"][20] == res["nominal"]["p90"][20]  # no randomness at all


def test_determinism_and_seed() -> None:
    kw: dict[str, Any] = {"assumptions": {}, "contributions": {"monthly": 300}}
    a, b = mc.run(params(seed=5, **kw)), mc.run(params(seed=5, **kw))
    c = mc.run(params(seed=6, **kw))
    assert a["nominal"] == b["nominal"] and a["success"] == b["success"]
    assert a["nominal"] != c["nominal"]


def test_fixed_real_fails_when_too_large_and_succeeds_when_small() -> None:
    # 100 000 at 5 %: growth is about 417 a month, so 600 drains it and 300 never does
    big = mc.run(params(withdrawal={"start_age": 40, "strategy": "fixed_real", "monthly": 600}))
    small = mc.run(params(withdrawal={"start_age": 40, "strategy": "fixed_real", "monthly": 300}))
    assert big["success"] == 0.0 and big["median_depletion_age"] is not None
    assert small["success"] == 1.0 and small["median_depletion_age"] is None
    assert small["ending_real"]["p50"] > 100_000


def test_guardrails_rules_on_a_hand_made_path() -> None:
    f = mc.guardrails_step
    r0 = 0.05
    value = np.array([1_000_000.0, 900_000.0, 1_000_000.0, 1_000_000.0, 1_000_000.0, 500_000.0])
    w_prev = np.array([50_000.0, 50_000.0, 70_000.0, 30_000.0, 59_000.0, 50_000.0])
    losing = np.array([False, True, False, False, True, True])
    out = f(w_prev, value, r0, losing, 0.02)
    assert out[0] == pytest.approx(51_000)  # in band: inflation raise
    assert out[1] == pytest.approx(50_000)  # losing year and rate 5.6 % > 5 %: no inflation raise, still in band
    assert out[2] == pytest.approx(70_000 * 1.02 * 0.9)  # rate 7.1 % > 6 %: cut 10 %
    assert out[3] == pytest.approx(30_000 * 1.02 * 1.1)  # rate 3.06 % < 4 %: raise 10 %
    assert out[4] == pytest.approx(59_000)  # losing year above the initial rate: no raise; 5.9 % is inside the band
    assert out[5] == pytest.approx(50_000 * 0.9)  # rate 10 % > 6 %: cut, no inflation raise in a losing year


def test_vpw_depletes_at_the_horizon() -> None:
    p = params(inflation=0.02, withdrawal={"start_age": 40, "strategy": "vpw"})
    res = mc.run(p)
    assert res["success"] == 1.0
    assert res["ending_real"]["p50"] < 5
    assert res["first_year"]["median"] > 0


def test_safe_rate_matches_the_analytic_rate_when_nothing_is_random() -> None:
    p = params(inflation=0.02, withdrawal={"start_age": 40, "strategy": "four_percent"}, success_target=1.0)
    res = mc.run(p)
    g = 1.05 ** (1 / 12) - 1
    q = 1.02 ** (1 / 12) / (1 + g)
    n = 50 * 12
    rate = 12 / (sum(q**k for k in range(n)))  # per euro at the start: v0 = W * sum q^k, rate = 12 W / v0
    assert abs(res["safe_withdrawal_rate"] - rate) < 0.0005
    assert res["perpetual_withdrawal_rate"] is not None
    assert res["perpetual_withdrawal_rate"] < res["safe_withdrawal_rate"]  # keeping the capital costs more


def test_compare_runs_on_the_same_paths() -> None:
    w = {"start_age": 60, "strategy": "four_percent", "rate": 0.04}
    p = params(assumptions={}, contributions={"monthly": 500}, withdrawal=w)
    a = mc.run(p)
    row = {r["strategy"]: r for r in a["compare"]}
    assert len(a["compare"]) == 5 and row["four_percent"]["selected"]
    # the headline run and the comparison of the same configuration agree exactly
    assert row["four_percent"]["success"] == a["success"]
    assert row["four_percent"]["median_ending_real"] == a["ending_real"]["p50"]
    # a different selected strategy sees the same default-rate four_percent row on the same paths
    b = mc.run(params(assumptions={}, contributions={"monthly": 500}, withdrawal={**w, "strategy": "vpw"}))
    brow = {r["strategy"]: r for r in b["compare"]}
    assert brow["four_percent"] == {**row["four_percent"], "selected": False}


def test_pension_floor_and_tax() -> None:
    base = {"start_age": 40, "strategy": "fixed_real", "monthly": 300}
    plain = mc.run(params(withdrawal=base))
    taxed = mc.run(params(withdrawal={**base, "tax": 0.5}))
    assert taxed["first_year"]["median"] == plain["first_year"]["median"]  # net amount is what is asked
    assert taxed["ending_real"]["p50"] < plain["ending_real"]["p50"]
    pens = mc.run(params(withdrawal={**base, "pension_monthly": 300, "pension_age": 40}))
    assert pens["first_year"]["median"] == 0
    floor = mc.run(params(withdrawal={"start_age": 40, "strategy": "percent_of_portfolio", "rate": 0.01, "floor": 500}))
    assert floor["first_year"]["median"] == 6000


def history(n: int) -> mc.History:
    dates = risk.month_ends(date(2026, 9, 30), n)[1:]
    rets = np.array([0.01 + 0.001 * (i % 7) for i in range(n)])
    return mc.History(dates, rets)


def test_historical_samples_only_the_given_series() -> None:
    h = history(48)
    p = params(return_model="historical", start_age=40, horizon_age=50, withdrawal={"start_age": 45})
    rng = np.random.default_rng(1)
    r = mc.bootstrap_returns(h, 60, 50, rng)
    assert set(np.round(np.unique(r), 12)) <= set(np.round(h.returns, 12))
    res = mc.run(p, h)
    cagr = float(np.prod(1 + h.returns)) ** (12 / 48) - 1
    assert res["history"] == {"months": 48, "from": h.dates[0], "to": h.dates[-1], "cagr": pytest.approx(cagr)}


def test_historical_needs_36_months() -> None:
    p = params(return_model="historical")
    with pytest.raises(ValueError, match="36 months"):
        mc.run(p, history(35))
    with pytest.raises(ValueError, match="36 months"):
        mc.run(p, None)
    mc.run(p, history(36))


def test_bad_correlation_matrix() -> None:
    bad = [[1, 2, 0, 0], [2, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    with pytest.raises(ValueError, match="correlation"):
        mc.run(params(assumptions={"corr": bad, "vol": mc.DEFAULT_VOL}))


def test_assumption_moments() -> None:
    p = params(paths=4000, horizon_age=41, assumptions={"mean": mc.DEFAULT_MEAN, "vol": mc.DEFAULT_VOL})
    r = mc.assumption_returns(p, 12, np.random.default_rng(3))
    yearly = np.prod(1 + r, axis=0)
    assert abs(yearly.mean() - 1.07) < 0.01 and abs(yearly.std() - 0.16) < 0.01


# ---- endpoints ----------------------------------------------------------------------------------


def _small_portfolio(c: TestClient) -> None:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True, "birth_date": "1986-05-01"}).json()
    acct = c.post(
        "/api/accounts",
        json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()["id"]
    inst = c.post("/api/instruments", json={"currency": "EUR", "type": "etf", "name": "World"}).json()["id"]
    d = risk.month_ends(risk.last_month_end(today()), 40)
    c.post(
        "/api/ledger/transactions",
        json={"account_id": acct, "date": d[0].isoformat(), "kind": "deposit", "amount": "10000"},
    )
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct,
            "date": d[0].isoformat(),
            "kind": "buy",
            "amount": "-10000",
            "instrument_id": inst,
            "quantity": "100",
        },
    )
    for i, day in enumerate(d):
        c.post(
            "/api/prices",
            json={
                "instrument_id": inst,
                "date": day.isoformat(),
                "close": f"{100 * 1.01**i * (1 + 0.02 * (i % 3)):.6f}",
            },
        )


def test_defaults_endpoint(auth_client: TestClient) -> None:
    _small_portfolio(auth_client)
    d = auth_client.get("/api/plan/montecarlo/defaults").json()
    assert d["start_value"] > 0 and abs(sum(d["mix"].values()) - 1) < 1e-3
    assert set(d["mix"]) == set(mc.CLASSES) and d["history"]["months"] >= 36
    assert d["assumptions"]["corr"][0][1] == 0.2 and d["start_age"] > 30


def test_run_endpoint(auth_client: TestClient) -> None:
    _small_portfolio(auth_client)
    body = {"start_value": 100_000, "start_age": 40, "contributions": {"monthly": 500}, "paths": 500}
    t0 = time.perf_counter()
    r = auth_client.post("/api/plan/montecarlo", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in (
        "ages",
        "real",
        "nominal",
        "success",
        "withdrawal_start_age",
        "first_year",
        "worst_year_p10",
        "compare",
        "safe_withdrawal_rate",
        "perpetual_withdrawal_rate",
        "ending_real",
        "inputs",
        "seed",
        "paths",
        "ms",
    ):
        assert k in d, k
    assert set(d["real"]) == {"p10", "p25", "p50", "p75", "p90"} and len(d["real"]["p50"]) == len(d["ages"])
    h = auth_client.post("/api/plan/montecarlo", json={**body, "return_model": "historical"})
    assert h.status_code == 200 and h.json()["history"]["months"] >= 36
    assert time.perf_counter() - t0 < 10
    for bad in (
        {"paths": 50},
        {"withdrawal": {"strategy": "nope"}},
        {"horizon_age": 30},
        {"start_age": 10, "horizon_age": 100},
        {"assumptions": {"corr": [[1, 0], [0, 1]]}},
        {"mix": {"gold": 1}},
    ):
        assert auth_client.post("/api/plan/montecarlo", json=bad).status_code == 422, bad


def test_historical_endpoint_without_history_is_422(auth_client: TestClient) -> None:
    r = auth_client.post("/api/plan/montecarlo", json={"return_model": "historical"})
    assert r.status_code == 422 and "36 months" in r.json()["detail"]


def test_speed_2000_paths_600_months() -> None:
    p = params(
        start_age=30,
        horizon_age=80,
        paths=2000,
        assumptions={},
        contributions={"monthly": 500},
        withdrawal={"start_age": 60, "strategy": "guardrails"},
    )
    t0 = time.perf_counter()
    mc.run(p)
    assert time.perf_counter() - t0 < 5


def test_short_history_warns_and_reports_its_growth() -> None:
    """Resampling a few strong years over a lifetime must say so: the answer is only as good as the sample."""
    h = mc.History(dates=[date(2020, 1, 31) + timedelta(days=31 * i) for i in range(48)], returns=np.full(48, 0.01))
    r = mc.run(
        mc.Params.from_dict(
            {"start_value": 1000, "start_age": 40, "horizon_age": 90, "return_model": "historical", "paths": 100}
        ),
        h,
    )
    assert abs(r["history"]["cagr"] - (1.01**12 - 1)) < 1e-9
    assert "Only 4 years" in r["history"]["warning"]


def test_vpw_plans_with_compounding_growth_not_the_arithmetic_mean() -> None:
    p = mc.Params.from_dict(
        {
            "start_value": 1000,
            "start_age": 40,
            "horizon_age": 90,
            "mix": {"equity": 1.0},
            "fees": 0.0,
            "inflation": 0.0,
            "paths": 100,
        }
    )
    c = mc.build_ctx(p, np.zeros((600, 100)))
    assert abs(c.r_real - (0.07 - 0.16**2 / 2)) < 1e-9


def test_glide_path_moves_the_mix_by_age() -> None:
    """All shares at 40 gliding to all cash at 50: from 50 on every path earns cash's fixed return exactly."""
    p = mc.Params.from_dict(
        {
            "start_age": 40,
            "horizon_age": 55,
            "paths": 200,
            "glide": [{"age": 40, "mix": {"equity": 1}}, {"age": 50, "mix": {"cash": 1}}],
            "assumptions": {
                "vol": {"equity": 0.16, "bond": 0.06, "cash": 0.0, "other": 0.2},
                "mean": {**mc.DEFAULT_MEAN, "cash": 0.02},
            },
        }
    )
    w = mc.mix_path(p, 15 * 12)
    assert w[0] == pytest.approx([1, 0, 0, 0]) and w[60] == pytest.approx([0.5, 0, 0.5, 0], abs=0.01)
    assert w[120:] == pytest.approx(np.tile([0, 0, 1, 0], (60, 1)))
    ret = mc.assumption_returns(p, 15 * 12, np.random.default_rng(1))
    assert ret[120:] == pytest.approx(np.full((60, 200), 1.02 ** (1 / 12) - 1))
    assert ret[:12].std() > 0.03  # all shares early on
    hist = mc.Params.from_dict({"start_age": 40, "horizon_age": 95, "return_model": "historical", "glide": p.glide})
    with pytest.raises(ValueError, match="glide path needs the assumptions"):
        mc.run(hist)
