from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.modules.portfolio import advisor as ad
from app.platform.db import today
from app.platform.jobs import run_now

from .test_risk import _prices, _rets, _setup

DAY = date(2026, 10, 12)


def test_metrics_match_the_lognormal_formulas() -> None:
    x = ad.metrics(0.07, 0.16, 10)
    m, s = ad.log_params(0.07, 0.16)
    assert x["var95"] == pytest.approx(pytest.approx(2.718281828 ** (m - 1.6448536 * s) - 1, rel=1e-6))
    assert x["cvar95"] < x["var95"] < 0  # the average of the bad tail is worse than its edge
    assert ad.metrics(0.07, 0.16, 20)["p_loss"] < x["p_loss"] < ad.metrics(0.07, 0.16, 1)["p_loss"]
    assert ad.metrics(0.02, 0.0, 1)["p_loss"] == 0.0


def test_tighter_limits_and_shorter_horizons_take_risk_off() -> None:
    def eq(**kw: float) -> float:
        r = ad.advise(ad.Inputs(**kw), DAY)  # type: ignore[arg-type]
        return float(r["points"][r["recommended"]]["mix"]["equity"])

    assert eq(horizon_years=25, max_loss=0.30) == pytest.approx(1.0)
    assert eq(horizon_years=25, max_loss=0.10) < eq(horizon_years=25, max_loss=0.20) <= 1.0
    assert eq(horizon_years=2, max_loss=0.30) < eq(horizon_years=10, max_loss=0.30)
    r = ad.advise(ad.Inputs(horizon_years=10, max_loss=0.15), DAY)
    rec = r["points"][r["recommended"]]
    assert rec["ok"] and rec["var95"] >= -0.15 and rec["p_loss"] <= 0.10
    assert all(not p["ok"] or p["return"] <= rec["return"] + 1e-12 for p in r["points"])  # the best inside the limits


def test_glide_path_never_adds_risk_and_ends_safe() -> None:
    r = ad.advise(ad.Inputs(horizon_years=12, max_loss=0.25), DAY)
    eqs = [g["mix"]["equity"] for g in r["glide"]]
    assert eqs == sorted(eqs, reverse=True) and eqs[0] > eqs[-1]
    assert r["glide"][0]["date"] == "2026-10-01" and r["glide"][-1]["date"] == "2038-10-01"
    assert r["glide_targets"][0]["mix"].keys() <= {"Equity", "Fixed income", "Cash"}


def test_impossible_limits_fall_back_to_the_lowest_risk() -> None:
    r = ad.advise(ad.Inputs(horizon_years=1, max_loss=0.001, max_shortfall=0.001), DAY)
    assert not r["found"] and r["recommended"] == 0 and "No mix meets" in r["notes"][0]
    with pytest.raises(ad.AdvisorError):
        ad.advise(ad.Inputs(horizon_years=0.1), DAY)
    with pytest.raises(ad.AdvisorError):
        ad.advise(ad.Inputs(bounds={"equity": (0.6, 0.4)}), DAY)


def test_projection_counts_contributions() -> None:
    a = ad.Inputs(start_value=10_000, monthly=500, mean={"equity": 0, "bond": 0, "cash": 0, "other": 0})
    a.vol = {"equity": 0.0, "bond": 0.0, "cash": 0.0, "other": 0.0}
    p = ad.project(a, {"cash": 1.0}, None, 2)
    assert p["invested"] == 22_000 and p["p50"] == pytest.approx(22_000) and p["drawdown_p50"] == 0


# ---- endpoints ---------------------------------------------------------------------------------------------------


def _portfolio(c: TestClient) -> None:
    run_now("rules.load")  # the plan prices selling instead with the tax tables
    _setup(c, {"World": (_prices(_rets([0.01, -0.005])), 80), "Bonds": (_prices(_rets([0.002])), 20)})


def test_advisor_endpoint_and_saving_a_glide_path(auth_client: TestClient) -> None:
    c = auth_client
    _portfolio(c)
    r = c.post("/api/portfolio/advisor", json={"horizon_years": 8, "max_loss": 0.15, "monthly": 300})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["current"]["mix"]["equity"] == pytest.approx(1.0)  # funds without a profile count as shares
    assert d["projection"]["recommended"]["invested"] == pytest.approx(d["inputs"]["start_value"] + 300 * 96)
    assert c.get("/api/portfolio/advisor").json()["saved"]["horizon_years"] == 8
    assert c.post("/api/portfolio/advisor", json={"bounds": {"gold": [0, 1]}}).status_code == 422

    body = {"targets": d["recommended_targets"], "band": "0.05", "glide": d["glide_targets"], "source": "advisor"}
    t = c.put("/api/portfolio/targets", json=body)
    assert t.status_code == 200, t.text
    saved = t.json()
    assert saved["source"] == "advisor" and len(saved["glide"]) == len(d["glide_targets"])
    first = d["glide_targets"][0]["mix"]
    assert {k: float(v) for k, v in saved["targets"].items()} == pytest.approx(first, abs=0.02)
    bad = {**body, "glide": body["glide"][:1]}
    assert c.put("/api/portfolio/targets", json=bad).status_code == 422

    # the new-money plan follows the path unless told otherwise
    plan = c.post("/api/portfolio/new-money", json={"monthly": 500, "months": 24, "min_trade": 0}).json()
    assert any("glide path" in n for n in plan["notes"])
    fixed = c.post("/api/portfolio/new-money", json={"monthly": 500, "months": 24, "follow_glide": False}).json()
    assert not any("glide path" in n for n in fixed["notes"])

    # and so can the Monte Carlo simulator
    age = 40
    mc = c.post(
        "/api/plan/montecarlo",
        json={"start_value": 100_000, "start_age": age, "horizon_age": 80, "paths": 200, "use_saved_glide": True},
    )
    assert mc.status_code == 200, mc.text
    glide = mc.json()["inputs"]["glide"]
    assert glide[0]["age"] == pytest.approx(
        age + (date.fromisoformat(d["glide_targets"][0]["date"]) - today()).days / 365.25
    )


def test_monte_carlo_without_a_saved_glide_says_so(auth_client: TestClient) -> None:
    r = auth_client.post("/api/plan/montecarlo", json={"start_value": 1000, "use_saved_glide": True, "paths": 100})
    assert r.status_code == 422 and "glide path" in r.json()["detail"]


def test_new_money_endpoint_applies_nordnet_fees(auth_client: TestClient) -> None:
    c = auth_client
    _portfolio(c)
    d = c.get("/api/portfolio/new-money").json()
    assert d["fees"]["etf"] == [2.5, 0.0]
    small = c.post(
        "/api/portfolio/new-money", json={"monthly": 100, "months": 6, "targets": {"Equity": 1.0}, "min_trade": 0}
    ).json()
    assert small["months_waited"] > 0 and small["fees"] > 0  # ETFs only: 100 € waits for a 250 € buy
    free = c.post(
        "/api/portfolio/new-money",
        json={"monthly": 100, "months": 6, "targets": {"Equity": 1.0}, "min_trade": 0, "fees": {"etf": [0, 0]}},
    ).json()
    assert free["fees"] == 0 and free["months_waited"] == 0


def test_margin_call_probability_matches_simulation() -> None:
    mu, sigma, k, c = 0.07, 0.16, 2.0, 0.80
    p = ad.margin_call_probability(mu, sigma, k, c)
    m, s = ad.log_params(mu, sigma)
    rng = np.random.default_rng(3)
    steps = 2520  # daily-ish monitoring over a year
    paths = rng.normal(m / steps, s / np.sqrt(steps), (4000, steps)).cumsum(axis=1)
    barrier = np.log((k - 1) / (c * k))
    sim = float((paths.min(axis=1) <= barrier).mean())
    assert p == pytest.approx(sim, abs=0.01) and 0 < p < 0.2
    assert ad.margin_call_probability(mu, sigma, 1.0, c) == 0.0


def test_advisor_can_borrow_when_allowed() -> None:
    loose = {"horizon_years": 30, "max_loss": 0.6, "max_shortfall": 0.4, "max_margin_call": 0.5}
    plain = ad.advise(ad.Inputs(**loose), DAY)  # type: ignore[arg-type]
    lev = ad.advise(ad.Inputs(**loose, leverage=True, borrow_rate=0.03, deductible=0.3), DAY)  # type: ignore[arg-type]
    assert all(p["leverage"] == 1.0 for p in plain["points"])
    rec = lev["points"][lev["recommended"]]
    assert rec["leverage"] > 1.0 and rec["return"] > plain["points"][plain["recommended"]]["return"]
    assert lev["projection"]["recommended"]["leverage"] == rec["leverage"]
    assert all(g["mix"]["equity"] <= 1.0 for g in lev["glide"])  # the glide path never borrows
    # with a thin collateral value the barrier is near, so the same limit allows less borrowing
    thin = {**loose, "max_margin_call": 0.01}
    near = ad.advise(ad.Inputs(**thin, leverage=True, borrow_rate=0.03, collateral_rate=0.55), DAY)  # type: ignore[arg-type]
    far = ad.advise(ad.Inputs(**thin, leverage=True, borrow_rate=0.03, collateral_rate=0.85), DAY)  # type: ignore[arg-type]
    assert near["points"][near["recommended"]]["leverage"] < far["points"][far["recommended"]]["leverage"]
    assert near["points"][near["recommended"]]["p_margin_call"] <= 0.01
