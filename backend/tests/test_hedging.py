import math

import pytest
from fastapi.testclient import TestClient

from app.modules.hedging import engine as he
from app.modules.hedging import options as op
from app.modules.hedging import products


def test_black_scholes_reference_values_and_parity() -> None:
    # Hull's textbook case: S = K = 100, T = 1, r = 5 %, σ = 20 %: call 10.4506, put 5.5735
    assert op.call(1.0, 1, 0.05, 0.0, 0.2) * 100 == pytest.approx(10.4506, abs=1e-3)
    assert op.put(1.0, 1, 0.05, 0.0, 0.2) * 100 == pytest.approx(5.5735, abs=1e-3)
    k, t, r, q, v = 0.9, 0.5, 0.03, 0.02, 0.25
    parity = op.call(k, t, r, q, v) - op.put(k, t, r, q, v)
    assert parity == pytest.approx(math.exp(-q * t) - k * math.exp(-r * t), abs=1e-12)
    assert op.put(0.9, 0, 0.03, 0, 0.2) == 0.0


def test_collar_cap_pays_for_the_put() -> None:
    cap = op.collar_cap(0.9, 1.0, 0.02, 0.015, 0.19, 0.145)
    assert cap > 1.0
    assert op.call(cap, 1.0, 0.02, 0.015, 0.145) == pytest.approx(op.put(0.9, 1.0, 0.02, 0.015, 0.19), abs=1e-9)


def _mkt(**kw: float) -> he.Market:
    return he.Market(**{"value": 100_000.0, "beta": 1.0, "resid_vol": 0.0, "gain_share": 0.4, **kw})  # type: ignore[arg-type]


def test_put_cuts_the_bad_tail_and_costs_on_average() -> None:
    out = he.simulate(
        _mkt(),
        he.Run(months=12, paths=3000, strategies=[he.Strategy("none"), he.Strategy("put", tenor=12, strike=0.9)]),
    )
    none, put = out["strategies"]
    assert put["p5"] > none["p5"] and put["mean"] < none["mean"] and put["cost"] < 0
    assert put["upfront"] == pytest.approx(100_000 * put["premium"], rel=1e-9)
    # a 30 % fall in one period: the put pays 20 % of the value back, less its premium and fee, after tax
    i = he.FALLS.index(-0.30)
    s = he.Strategy("put", tenor=12, strike=0.9)
    fees = float(he.option_cost(s, 100_000, put["premium"])) / 100_000  # 0.8 % (min 15 €) + 0.75 % of the premium
    assert fees == pytest.approx(max(15, 0.008 * 100_000 * put["premium"]) / 100_000 + 0.0075 * put["premium"])
    pnl = 0.20 - put["premium"] - fees
    assert put["scenario"][i] == pytest.approx(-0.30 + pnl * (1 - 0.30), abs=1e-9)
    assert put["available"] is False  # Nordnet has no standardised options outside the Nordic exchanges
    assert none["scenario"][i] == pytest.approx(-0.30)


def test_short_mini_offsets_the_index_and_pays_financing() -> None:
    s = he.Strategy("short", ratio=1.0, mini_cost=0.02, tenor=12)
    i = he.FALLS.index(-0.20)
    v = he._scenario(_mkt(), s, -0.20, None, 0.0)
    # the shares lose 20 %; the short gains 20 % less a year's financing, taxed at 30 %
    assert v == pytest.approx(-0.20 + (0.20 - 0.02) * 0.7)
    assert he.FALLS[i] == -0.20


def test_bear_certificate_costs_its_financing_in_a_flat_market() -> None:
    mkt = _mkt(index_mean=0.0, index_vol=0.30, portfolio_mean=0.0)
    out = he.simulate(
        mkt,
        he.Run(
            months=12, paths=4000, strategies=[he.Strategy("none"), he.Strategy("bear", ratio=1.0, bear_factor=3.0)]
        ),
    )
    none, bear = out["strategies"]
    # on average a daily-reset certificate costs about its financing: a third of the value in a ×3 certificate at
    # 2.5 % a year is 0.83 %, 0.58 % after the tax credit on the loss
    assert -0.012 < bear["cost"] < -0.003
    assert he._bear_cost(3.0) == pytest.approx(2 * 0.01 + 0.005) and he._bear_cost(10.0) == pytest.approx(
        9 * 0.03 + 0.005
    )


def test_selling_pays_tax_now_and_osakesaastotili_first() -> None:
    s = he.Strategy("sell", ratio=0.5)
    out = he.simulate(_mkt(tax_free_share=0.2), he.Run(months=1, paths=200, strategies=[s]))
    sell = next(r for r in out["strategies"] if r["kind"] == "sell")
    # sells 50 000: 20 000 inside the OST tax-free, 30 000 taxable with 40 % gain: 30 000 × 0.4 × 0.3 = 3 600
    assert sell["tax_now"] == pytest.approx(3_600)


def test_validation() -> None:
    with pytest.raises(he.HedgeError):
        he.simulate(_mkt(), he.Run(months=0))
    with pytest.raises(he.HedgeError):
        he.simulate(_mkt(), he.Run(strategies=[he.Strategy("straddle")]))
    with pytest.raises(he.HedgeError):
        he.simulate(_mkt(value=0), he.Run())


def test_product_parsing_keeps_index_shorts_only() -> None:
    info = {
        "isin": "DE000JE88EQ1",
        "name": "MINI S SP500 NORDNET FI55",
        "is_tradable": True,
        "currency": "EUR",
        "issuer_name": "J.P. Morgan SE",
        "instrument_id": 1,
    }
    row = {
        "instrument_info": info,
        "etp_info": {"direction": "Short", "underlying_name": "S&P 500 Index"},
        "ko_info": {"financial_level": 7200.0, "barrier_price": 7050.0},
    }
    p = products.parse(row, "minifuturelist")
    assert p and p["index_key"] == "SPX" and p["kind"] == "mini" and p["financing_level"] == 7200.0
    bear = {**row, "instrument_info": {**row["instrument_info"], "name": "BEAR SP500 X2 NORDNET FI31", "isin": "X1"}}
    assert products.parse(bear, "bullbearlist")["leverage"] == 2.0  # type: ignore[index]
    long = {**row, "etp_info": {"direction": "Long", "underlying_name": "S&P 500 Index"}}
    assert products.parse(long, "minifuturelist") is None
    other = {**row, "etp_info": {"direction": "Short", "underlying_name": "Tesla"}}
    assert products.parse(other, "minifuturelist") is None


def test_endpoints_without_history(auth_client: TestClient) -> None:
    from .test_risk import _prices, _rets, _setup

    _setup(auth_client, {"World": (_prices(_rets([0.01, -0.005])), 80)})
    o = auth_client.get("/api/hedging").json()
    assert o["fits"] == [] and o["best"] is None and o["tax"]["value"] > 0  # no index levels loaded in tests
    r = auth_client.post("/api/hedging/simulate", json={"months": 6})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["index"] is None and len(d["strategies"]) == len(he.default_strategies())
    assert {s["kind"] for s in d["strategies"]} >= {"none", "put", "collar", "short", "bear", "sell"}
    assert auth_client.post("/api/hedging/simulate", json={"index": "SPX"}).status_code == 422
    bad = auth_client.post("/api/hedging/simulate", json={"strategies": [{"kind": "put", "ratio": 5}]})
    assert bad.status_code == 422


def test_monte_carlo_hedge_overlay() -> None:
    import numpy as np

    from app.modules.planning import montecarlo as mc

    ret = np.zeros((12, 2))
    ret[5] = [-0.30, 0.10]  # one path crashes, the other rallies
    h = mc.Hedge(kind="put", ratio=1.0, tenor=12, strike=0.90, fee=0.0, tax=0.0)
    out, info = mc.hedged_returns(ret, h, vol=0.16, rate=0.02)
    g = np.prod(1 + out, axis=0)
    prem = info["premium"]
    assert g[0] == pytest.approx(0.70 + (0.20 - prem))  # the put pays back the fall below 90 %
    assert g[1] == pytest.approx(1.10 - prem)
    short, _ = mc.hedged_returns(
        ret, mc.Hedge(kind="short", ratio=1.0, tenor=12, fee=0.0, short_cost=0.0, tax=0.0), 0.16, 0.02
    )
    assert np.prod(1 + short, axis=0) == pytest.approx([1.0, 1.0])  # fully hedged: flat either way
    with pytest.raises(ValueError):
        mc.hedged_returns(ret, mc.Hedge(kind="straddle"), 0.16, 0.02)

    base = {"start_value": 100_000, "start_age": 40, "horizon_age": 70, "paths": 400, "withdrawal": {"start_age": 60}}
    r = mc.run(mc.Params.from_dict({**base, "hedge": {"kind": "put", "strike": 0.85}}))
    plain = mc.run(mc.Params.from_dict(base))
    assert r["hedge"]["kind"] == "put" and r["hedge"]["cost_per_year"] > 0
    # insurance costs: a lower median at retirement in exchange for a better bad case
    assert r["value_at_withdrawal_start"]["real_median"] < plain["value_at_withdrawal_start"]["real_median"]
