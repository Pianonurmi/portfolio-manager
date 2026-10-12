from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.modules.portfolio import optimize as opt

from .test_risk import _prices, _rets, _setup


def cov_of(vol: list[float], corr: list[list[float]]) -> Any:
    v = np.array(vol)
    return np.outer(v, v) * np.array(corr)


def test_two_asset_min_variance_matches_the_formula() -> None:
    s1, s2, rho = 0.2, 0.1, 0.3
    cov = cov_of([s1, s2], [[1, rho], [rho, 1]])
    w = opt.solve("min_variance", np.array([0.08, 0.04]), cov, np.zeros(2), np.ones(2))
    w1 = (s2**2 - rho * s1 * s2) / (s1**2 + s2**2 - 2 * rho * s1 * s2)
    assert w == pytest.approx([w1, 1 - w1], abs=1e-6)


def test_max_sharpe_is_the_tangency_portfolio() -> None:
    mu = np.array([0.09, 0.06, 0.04])
    cov = cov_of([0.2, 0.12, 0.05], [[1, 0.3, 0.1], [0.3, 1, 0.2], [0.1, 0.2, 1]])
    rf = 0.01
    z = np.linalg.solve(cov, mu - rf)
    tangent = z / z.sum()
    assert (tangent > 0).all()  # the case is chosen so that the long-only optimum is the closed form
    w = opt.solve("max_sharpe", mu, cov, np.zeros(3), np.ones(3), rf)
    assert w == pytest.approx(tangent, abs=1e-4)


def test_caps_and_fixed_weights_are_respected() -> None:
    mu = np.array([0.10, 0.05, 0.03, 0.02])
    cov = cov_of([0.2, 0.1, 0.05, 0.02], np.eye(4) * 0.8 + 0.2)
    lo, hi = np.zeros(4), np.full(4, 0.4)
    lo[3] = hi[3] = 0.1  # a fixed block
    for objective, target in [
        ("max_sharpe", None),
        ("min_variance", None),
        ("risk_parity", None),
        ("target_return", 0.05),
    ]:
        w = opt.solve(objective, mu, cov, lo, hi, 0.01, target)
        assert w.sum() == pytest.approx(1.0) and (w <= hi + 1e-9).all() and (w >= lo - 1e-9).all()
        assert w[3] == pytest.approx(0.1)
    w = opt.solve("target_vol", mu, cov, lo, hi, 0.01, 0.08)
    assert w.max() <= 0.4 + 1e-9 and np.sqrt(w @ cov @ w) <= 0.08 + 1e-5


def test_risk_parity_equalises_risk_contributions() -> None:
    cov = cov_of([0.2, 0.1, 0.05], [[1, 0.5, 0.1], [0.5, 1, 0.2], [0.1, 0.2, 1]])
    w = opt.solve("risk_parity", np.zeros(3), cov, np.zeros(3), np.ones(3))
    assert opt.risk_contributions(w, cov) == pytest.approx([1 / 3] * 3, abs=1e-4)
    assert w[2] > w[1] > w[0]  # the calmer the asset, the more of it


def test_frontier_is_monotone_and_spans_the_feasible_returns() -> None:
    mu = np.array([0.08, 0.05, 0.03])
    cov = cov_of([0.18, 0.08, 0.02], [[1, 0.2, 0.0], [0.2, 1, 0.1], [0.0, 0.1, 1]])
    lo, hi = np.zeros(3), np.full(3, 0.7)
    pts = opt.frontier(mu, cov, lo, hi, 0.01)
    assert len(pts) == opt.FRONTIER_POINTS
    rets, vols = [p["return"] for p in pts], [p["volatility"] for p in pts]
    assert all(b > a for a, b in zip(rets, rets[1:], strict=False))
    assert all(b >= a - 1e-7 for a, b in zip(vols, vols[1:], strict=False))
    assert rets[0] == pytest.approx(opt.stats(opt.solve("min_variance", mu, cov, lo, hi), mu, cov, 0)[0])
    assert rets[-1] == pytest.approx(0.7 * 0.08 + 0.3 * 0.05)  # 70 % in the best asset, the rest in the next
    assert all(sum(p["weights"]) == pytest.approx(1.0) for p in pts)


def test_infeasible_requests_explain_themselves() -> None:
    mu = np.array([0.08, 0.04])
    cov = cov_of([0.2, 0.1], [[1, 0.0], [0.0, 1]])
    lo, hi = np.zeros(2), np.ones(2)
    with pytest.raises(opt.OptimizeError, match="above the highest reachable"):
        opt.solve("target_return", mu, cov, lo, hi, 0.0, 0.12)
    with pytest.raises(opt.OptimizeError, match="below the lowest reachable"):
        opt.solve("target_vol", mu, cov, lo, hi, 0.0, 0.01)
    with pytest.raises(opt.OptimizeError, match="cannot add up to 100"):
        opt.solve("min_variance", mu, cov, lo, np.full(2, 0.4))
    with pytest.raises(opt.OptimizeError, match="needs a target"):
        opt.solve("target_vol", mu, cov, lo, hi)
    with pytest.raises(opt.OptimizeError, match="beat the risk-free"):
        opt.solve("max_sharpe", mu, cov, lo, hi, 0.09)


def test_ledoit_wolf_shrinks_towards_constant_correlation() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.04, (60, 5)) @ np.linalg.cholesky(np.full((5, 5), 0.4) + np.eye(5) * 0.6).T
    cov, delta = opt.ledoit_wolf_cc(x)
    assert 0 < delta < 1
    assert np.linalg.eigvalsh(cov).min() > 0
    assert np.diag(cov) == pytest.approx(np.var(x, axis=0, ddof=1), rel=1e-9)  # variances are kept
    chol = np.linalg.cholesky(np.full((5, 5), 0.4) + np.eye(5) * 0.6).T
    _, d2 = opt.ledoit_wolf_cc(rng.normal(0, 0.04, (20000, 5)) @ chol)
    assert d2 < delta  # plenty of data: little shrinkage


def test_class_inputs_validate_assumptions() -> None:
    mu, cov = opt.class_inputs(None)
    assert mu.shape == (4,) and cov.shape == (4, 4) and cov[0, 0] == pytest.approx(0.16**2)
    with pytest.raises(opt.OptimizeError):
        opt.class_inputs({"corr": [[1, 2, 0, 0], [2, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]})


# ---- endpoint --------------------------------------------------------------------------------------------------


def test_optimize_endpoint_classes(auth_client: TestClient) -> None:
    c = auth_client
    r = c.get("/api/portfolio/optimize?universe=classes&objective=max_sharpe")
    assert r.status_code == 422 and "no investment holdings" in r.json()["detail"].lower()
    assert c.get("/api/portfolio/optimize?universe=nope").status_code == 422
    assert c.get("/api/portfolio/optimize?objective=target_vol").status_code == 422
    assert c.post("/api/portfolio/optimize", json={"assumptions": {"vol": {"equity": -1}}}).status_code == 422
    assert c.get("/api/portfolio/optimize/defaults").json()["classes"] == ["equity", "bond", "cash", "other"]


def _four_holdings(c: TestClient) -> None:
    _setup(
        c,
        {
            "Alpha": (_prices(_rets([0.02, -0.01, 0.03, -0.02])), 40),
            "Beta": (_prices(_rets([0.01, 0.02, -0.015, 0.03, -0.02, 0.0])), 30),
            "Calm": (_prices(_rets([0.004, 0.003, 0.005, 0.002])), 60),
            "Echo": (_prices(_rets([-0.01, 0.02, 0.01, -0.005, 0.015])), 20),
        },
    )


def test_optimize_endpoint_holdings(auth_client: TestClient) -> None:
    c = auth_client
    _four_holdings(c)
    r = c.get("/api/portfolio/optimize?universe=holdings&objective=max_sharpe&period=3Y&max_weight=0.5&rf=0.01")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["assets"]) == 4 and len(d["frontier"]) == 25
    assert sum(a["suggested_weight"] for a in d["assets"]) == pytest.approx(1.0)
    assert max(a["suggested_weight"] for a in d["assets"]) <= 0.5 + 1e-6
    assert sum(float(a["move"]) for a in d["assets"]) == pytest.approx(0.0, abs=0.05)  # buys and sells cancel
    assert d["turnover"] == pytest.approx(
        sum(abs(float(a["move"])) for a in d["assets"]) / 2 / float(d["total"]), abs=1e-4
    )
    assert d["optimum"]["sharpe"] >= d["current"]["sharpe"] - 1e-9
    assert d["window"]["months"] == 36 and any("shrunk" in n for n in d["notes"])
    mv = c.get("/api/portfolio/optimize?universe=holdings&objective=min_variance&period=3Y&max_weight=1").json()
    assert mv["optimum"]["volatility"] <= d["optimum"]["volatility"] + 1e-9
    assert mv["optimum"]["volatility"] == pytest.approx(mv["frontier"][0]["volatility"], abs=1e-6)
    # 4 holdings under a 20 % cap cannot reach 100 %
    bad = c.get("/api/portfolio/optimize?universe=holdings&max_weight=0.2")
    assert bad.status_code == 422 and "cannot reach 100" in bad.json()["detail"]
    rp = c.get("/api/portfolio/optimize?universe=holdings&objective=risk_parity&period=3Y&max_weight=1").json()
    assert sum(a["risk_share"] for a in rp["assets"]) == pytest.approx(1.0)
    assert all(a["risk_share"] == pytest.approx(0.25, abs=1e-3) for a in rp["assets"])
    assert (
        c.get("/api/portfolio/optimize?universe=holdings&objective=target_return&target=5&max_weight=1").status_code
        == 422
    )


def test_optimize_endpoint_classes_with_custom_assumptions(auth_client: TestClient) -> None:
    c = auth_client
    _four_holdings(c)
    d = c.get("/api/portfolio/optimize?universe=classes&objective=min_variance").json()
    assert [a["key"] for a in d["assets"]] == ["equity", "bond", "cash", "other"]
    assert d["assets"][0]["current_weight"] == pytest.approx(1.0)  # funds without a profile count as equity
    assert d["assumptions"]["mean"]["equity"] == 0.07 and d["assets"][2]["suggested_weight"] > 0.5
    body = {
        "objective": "max_sharpe",
        "assumptions": {"mean": {"bond": 0.10}},
        "bounds": {"equity": [0.2, 0.6]},
        "rf": 0.01,
    }
    p = c.post("/api/portfolio/optimize", json=body).json()
    eq, bd = p["assets"][0], p["assets"][1]
    assert 0.2 - 1e-6 <= eq["suggested_weight"] <= 0.6 + 1e-6 and bd["expected_return"] == 0.10
    assert float(eq["move"]) < 0  # the fully equity portfolio sells equity


def test_classes_max_sharpe_treats_cash_as_the_risk_free_asset(auth_client: TestClient) -> None:
    """Against a rate below cash's return, cash alone would look best and max-Sharpe would be nearly all cash."""
    _four_holdings(auth_client)
    d = auth_client.get("/api/portfolio/optimize?universe=classes&objective=max_sharpe").json()
    assert abs(d["rf"] - d["assumptions"]["mean"]["cash"]) < 1e-12
    cash = next(a for a in d["assets"] if a.get("key", a.get("name", "")).lower() == "cash")
    assert cash["suggested_weight"] < 0.05, cash
    explicit = auth_client.get("/api/portfolio/optimize?universe=classes&objective=max_sharpe&rf=0.0").json()
    assert explicit["rf"] == 0.0


def test_holdings_optimiser_can_add_candidate_funds(auth_client: TestClient) -> None:
    """A screener candidate with full history joins at 0 %; one with too little history is named and left out."""
    from app.modules.core.models import Instrument
    from app.modules.portfolio import risk
    from app.platform.db import session_factory, today

    c = auth_client
    _four_holdings(c)
    months = risk.month_ends(risk.last_month_end(today()), 36)
    made = {}
    for name, prices in {
        "Steady": _prices(_rets([0.012, 0.011, 0.013, 0.012])),
        "Fresh": [None] * 31 + _prices(_rets([0.01], 5)),
    }.items():
        iid = c.post("/api/instruments", json={"currency": "EUR", "type": "etf", "name": name}).json()["id"]
        made[name] = iid
        for d, p in zip(months, prices, strict=True):
            if p is not None:
                c.post("/api/prices", json={"instrument_id": iid, "date": d.isoformat(), "close": f"{p:.8f}"})
    with session_factory()() as db:
        for iid in made.values():
            inst = db.get(Instrument, iid)
            assert inst is not None
            inst.meta = {**inst.meta, "candidate": True}
        db.commit()
    url = "/api/portfolio/optimize?universe=holdings&objective=max_sharpe&period=3Y&max_weight=1&rf=0.01"
    plain = c.get(url).json()
    assert all("candidate" not in a["name"] for a in plain["assets"])
    d = c.get(url + "&candidates=true").json()
    steady = next(a for a in d["assets"] if a["key"] == str(made["Steady"]))
    assert (
        steady["current_weight"] == 0 and steady["suggested_weight"] > 0.5
    )  # smooth, high return: the optimum loves it
    assert all(a["key"] != str(made["Fresh"]) for a in d["assets"])
    assert any("Fresh" in n for n in d["notes"])
    assert sum(a["suggested_weight"] for a in d["assets"]) == pytest.approx(1.0)
