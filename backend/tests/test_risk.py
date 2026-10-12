"""Risk analytics: exact statistics on known monthly series, window and exclusions, correlations, the endpoint."""

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from app.modules.portfolio import risk
from app.platform.db import today

R = [0.10, -0.20, 0.10, 0.05, 0.0, 0.02, -0.01, 0.03, 0.04, -0.05, 0.02, 0.01]
DATES = risk.month_ends(date(2025, 12, 31), 12)


def near(a: Any, b: float, tol: float = 1e-9) -> bool:
    return abs(float(a) - b) <= tol


# ---- pure statistics -----------------------------------------------------------------------------


def test_month_ends_and_last_month_end() -> None:
    assert risk.last_month_end(date(2026, 10, 11)) == date(2026, 9, 30)
    assert risk.last_month_end(date(2026, 2, 28)) == date(2026, 2, 28)
    me = risk.month_ends(date(2026, 3, 31), 3)
    assert me == [date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


def test_metrics_known_series() -> None:
    m = risk.metrics(np.array(R), DATES, rf=0.0)
    wealth = math.prod(1 + x for x in R)
    mean = sum(R) / 12
    sd = math.sqrt(sum((x - mean) ** 2 for x in R) / 11)
    assert near(m["total_return"], wealth - 1) and near(m["cagr"], wealth - 1)  # exactly one year
    assert near(m["volatility"], sd * math.sqrt(12))
    assert near(m["sharpe"], mean * 12 / (sd * math.sqrt(12)))
    dd = m["max_drawdown"]
    assert near(dd["value"], 0.88 / 1.1 - 1) and not dd["recovered"] and dd["recovery"] is None
    assert dd["peak"] == DATES[1] and dd["trough"] == DATES[2]
    assert m["longest_drawdown_months"] == 11
    assert near(m["var_95"], 0.1175) and near(m["cvar_95"], 0.2)
    assert near(m["positive_months"], 8 / 12)
    assert m["best_month"]["value"] == 0.10 and m["worst_month"]["value"] == -0.20
    assert m["best_year"]["year"] == 2025 and near(m["best_year"]["value"], wealth - 1)
    down = math.sqrt(sum(min(x, 0) ** 2 for x in R) / 12) * math.sqrt(12)
    assert near(m["sortino"], mean * 12 / down)


def test_drawdown_recovers() -> None:
    m = risk.metrics(np.array([0.1, -0.1, 0.12, 0.01]), risk.month_ends(date(2025, 4, 30), 4), rf=0.0)
    dd = m["max_drawdown"]
    assert dd["recovered"] and dd["recovery"] == date(2025, 3, 31) and near(dd["value"], -0.1)
    assert m["longest_drawdown_months"] == 1


def test_only_full_calendar_years_count() -> None:
    d = risk.month_ends(date(2026, 3, 31), 15)  # 2025 full (12 months), 2026 has 3 months
    m = risk.metrics(np.full(15, 0.01), d, rf=0.0)
    assert m["best_year"]["year"] == 2025 and m["worst_year"]["year"] == 2025


def test_correlation_and_contributions() -> None:
    a = np.array(R)
    cols = np.column_stack([a, a, -a])
    c = risk.correlation_matrix(cols)
    assert near(c[0][1], 1.0) and near(c[0][2], -1.0) and c[0][0] == 1.0
    flat = risk.correlation_matrix(np.column_stack([a, np.zeros(12)]))
    assert flat[0][1] is None  # no variance, no correlation

    b = np.array([0.01, 0.03, -0.02, 0.0, 0.02, -0.01, 0.04, 0.0, -0.03, 0.01, 0.02, 0.0])
    cols = np.column_stack([a, b, a * 0.5 + b])
    w = np.array([0.5, 0.3, 0.2])
    shares, vols = risk.risk_contributions(cols, w)
    assert near(shares.sum(), 1.0) and near(vols[0], np.std(a, ddof=1) * math.sqrt(12))


def test_benchmark_against_itself() -> None:
    a = np.array(R)
    m = risk.metrics(a, DATES, rf=0.02, bench=a)["benchmark"]
    assert near(m["beta"], 1.0) and near(m["correlation"], 1.0) and near(m["alpha"], 0.0)
    assert near(m["tracking_error"], 0.0) and near(m["up_capture"], 1.0) and near(m["down_capture"], 1.0)


def test_history_start() -> None:
    assert risk.history_start([None, 1.0, 2.0]) == 1
    assert risk.history_start([1.0, None, 2.0, 3.0]) == 2  # a gap restarts the history
    assert risk.history_start([1.0, 2.0, None]) is None


# ---- endpoint, on prices written through the API ---------------------------------------------------


def _prices(rets: list[float]) -> list[float]:
    p = [100.0]
    for r in rets:
        p.append(p[-1] * (1 + r))
    return p


def _setup(
    c: TestClient, series: dict[str, tuple[list[float | None], int]], n: int = 36
) -> tuple[dict[str, int], list[date]]:
    """Instruments with a price at every month end (None = no price yet) and `units` held of each."""
    months = risk.month_ends(risk.last_month_end(today()), n)
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts",
        json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()["id"]
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct,
            "date": months[0].isoformat(),
            "kind": "deposit",
            "amount": str(sum(u for _, u in series.values()) * 100),
        },
    )
    ids: dict[str, int] = {}
    for name, (prices, units) in series.items():
        inst = c.post("/api/instruments", json={"currency": "EUR", "type": "etf", "name": name}).json()["id"]
        ids[name] = inst
        r = c.post(
            "/api/ledger/transactions",
            json={
                "account_id": acct,
                "date": months[0].isoformat(),
                "kind": "buy",
                "amount": str(-units * 100),
                "instrument_id": inst,
                "quantity": str(units),
            },
        )
        assert r.status_code < 300, r.text
        for d, p in zip(months, prices, strict=True):
            if p is not None:
                c.post("/api/prices", json={"instrument_id": inst, "date": d.isoformat(), "close": f"{p:.8f}"})
    return ids, months


A = [0.02, -0.01, 0.03, -0.02]
E = [0.01, 0.02, -0.015, 0.03, -0.02, 0.0]


def _rets(pattern: list[float], n: int = 36) -> list[float]:
    return [pattern[i % len(pattern)] for i in range(n)]


def test_endpoint_window_exclusion_correlation_and_risk_share(auth_client: TestClient) -> None:
    c = auth_client
    a, e = _rets(A), _rets(E)
    short: list[float | None] = [None] * 31 + _prices(_rets([0.01, -0.01], 5))  # six month ends of history
    ids, months = _setup(
        c,
        {
            "Alpha": (_prices(a), 40),
            "Anti": (_prices([-x for x in a]), 100),
            "Echo": (_prices(e), 45),
            "Alpha twin": (_prices(a), 10),
            "Newcomer": (short, 3),
        },
    )
    r = c.get("/api/portfolio/risk?period=3Y")
    assert r.status_code == 200, r.text
    d = r.json()
    assert {"window", "excluded", "holdings", "metrics", "series", "correlations", "notes", "benchmark"} <= d.keys()
    assert d["window"]["months"] == 36 and d["window"]["start"] == months[0].isoformat()
    assert [x["name"] for x in d["excluded"]] == ["Newcomer"]
    assert d["excluded"][0]["first_price"] == months[31].isoformat()
    assert d["window"]["coverage"] < 1 and d["window"]["coverage"] >= 0.95
    assert any("Returns include dividends" in n for n in d["notes"]) and any("Newcomer" in n for n in d["notes"])
    assert near(sum(h["weight"] for h in d["holdings"]), 1.0)
    assert near(sum(h["risk_share"] for h in d["holdings"]), 1.0, 1e-6)

    cor = d["correlations"]["holdings"]
    lab = cor["labels"]
    m = cor["matrix"]
    assert near(m[lab.index("Alpha")][lab.index("Alpha twin")], 1.0, 1e-6)
    assert near(m[lab.index("Alpha")][lab.index("Anti")], -1.0, 1e-6)
    assert len(d["series"]["drawdown"]) == 37 and len(d["series"]["rolling_12m"]) == 25
    assert d["correlations"]["classes"]["labels"]  # one Equity sleeve at least


def test_endpoint_matches_manual_portfolio_and_benchmark_beta(auth_client: TestClient) -> None:
    c = auth_client
    a = _rets(A)
    ids, months = _setup(c, {"Alpha": (_prices(a), 50)})
    c.put("/api/portfolio/benchmark", json={"instrument_id": ids["Alpha"]})
    d = c.get("/api/portfolio/risk?period=3Y&rf=0").json()
    wealth = math.prod(1 + x for x in a)
    assert near(d["metrics"]["cagr"], wealth ** (1 / 3) - 1, 1e-6)
    assert near(d["metrics"]["volatility"], float(np.std(a, ddof=1)) * math.sqrt(12), 1e-6)
    assert near(d["benchmark"]["available"], 1) and d["metrics"]["benchmark"] is not None
    assert near(d["metrics"]["benchmark"]["beta"], 1.0, 1e-6)
    assert near(d["metrics"]["benchmark"]["correlation"], 1.0, 1e-6)


def test_short_history_shrinks_window(auth_client: TestClient) -> None:
    c = auth_client
    long_p = _prices(_rets(A))
    half: list[float | None] = [None] * 18 + _prices(_rets(E, 18))
    _, months = _setup(c, {"Old": (long_p, 10), "Young": (half, 50)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert d["window"]["start"] == months[18].isoformat() and d["window"]["months"] == 18
    assert d["excluded"] == []
    assert any("shorter than requested" in n for n in d["notes"])


def test_endpoint_empty_and_bad_period(auth_client: TestClient) -> None:
    d = auth_client.get("/api/portfolio/risk").json()
    assert d["metrics"] is None and d["window"] is None and d["notes"]
    assert auth_client.get("/api/portfolio/risk?period=7Y").status_code == 422


def test_holding_without_a_price_series_is_listed_not_blocking(auth_client: TestClient) -> None:
    """A holding whose feed delivers nothing (here ~15 % of value) is left out and named; the rest is measured."""
    c = auth_client
    dead: list[float | None] = [100.0] + [None] * 36  # one old trade price, nothing since
    _setup(c, {"Alpha": (_prices(_rets(A)), 50), "Echo": (_prices(_rets(E)), 35), "No feed": (dead, 15)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert d["metrics"] is not None and d["window"]["months"] == 36
    assert [x["name"] for x in d["excluded"]] == ["No feed"]
    assert near(d["window"]["coverage"], 85 / 100, 0.02)  # of today's value; shown next to the figures


def test_no_report_when_most_of_the_portfolio_has_no_prices(auth_client: TestClient) -> None:
    c = auth_client
    dead: list[float | None] = [100.0] + [None] * 36
    _setup(c, {"Alpha": (_prices(_rets(A)), 10), "No feed": (dead, 90)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert d["metrics"] is None and any("Not enough price history" in n for n in d["notes"])


# ---- proxies for short histories -------------------------------------------------------------------


def _young(rets: list[float], start: int = 30, n: int = 36) -> list[float | None]:
    return [None] * start + _prices(rets[start:n])[: n - start + 1]


def test_young_holding_is_extended_with_a_correlated_long_one(auth_client: TestClient) -> None:
    c = auth_client
    a = _rets(A)
    near_a = [x + (0.001 if i % 2 else -0.001) for i, x in enumerate(a)]  # tracks Alpha closely, not identically
    ids, months = _setup(c, {"Alpha": (_prices(a), 50), "Young": (_young(near_a, 12), 50)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert d["window"]["months"] == 36 and d["window"]["start"] == months[0].isoformat()
    assert d["excluded"] == []
    [p] = d["proxies"]
    assert p["name"] == "Young" and p["proxy_name"] == "Alpha" and p["proxy_id"] == ids["Alpha"]
    assert p["correlation"] >= 0.95 and p["overlap_months"] == 24 and p["from"] == months[0].isoformat()
    assert p["to"] == months[11].isoformat()
    assert any("Young before" in n and "uses Alpha" in n for n in d["notes"])
    assert d["correlations"]["holdings"]["extended"] == [False, False]  # 24 real months in common: no fallback
    p_off = c.get("/api/portfolio/risk?period=3Y&proxies=false").json()
    assert p_off["proxies"] == []


def test_synthetic_values_reproduce_the_proxy_returns() -> None:
    own: list[float | None] = [None, None, None, 50.0, 55.0, 49.5]
    pv: list[float | None] = [100.0, 102.0, 99.0, 101.0, 103.0, 100.0]
    out = risk._splice(own, 3, pv)
    assert out[3:] == own[3:] and all(x is not None for x in out)
    syn = np.array(out[:4], dtype=float)
    assert np.allclose(syn[1:] / syn[:-1] - 1, np.array(pv[1:4], dtype=float) / np.array(pv[:3], dtype=float) - 1)


def test_proxies_off_shrinks_the_window(auth_client: TestClient) -> None:
    c = auth_client
    a = _rets(A)
    near_a = [x + (0.001 if i % 2 else -0.001) for i, x in enumerate(a)]
    _, months = _setup(c, {"Alpha": (_prices(a), 10), "Young": (_young(near_a, 18), 50)})
    on = c.get("/api/portfolio/risk?period=3Y").json()
    off = c.get("/api/portfolio/risk?period=3Y&proxies=false").json()
    assert on["window"]["months"] == 36 and len(on["proxies"]) == 1
    assert off["window"]["start"] == months[18].isoformat() and off["window"]["months"] == 18


def test_uncorrelated_holding_is_not_a_proxy(auth_client: TestClient) -> None:
    c = auth_client
    _, months = _setup(c, {"Echo": (_prices(_rets(E)), 10), "Young": (_young(_rets(A), 18), 50)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert d["proxies"] == []
    assert d["window"]["start"] == months[18].isoformat()


def test_extended_holding_never_becomes_a_proxy(auth_client: TestClient) -> None:
    """Mid follows Alpha and is extended; Late follows only Mid, which must not serve as its (chained) proxy."""
    c = auth_client
    a = _rets(A)
    dev = [0.0] * 24 + [0.013 * [1, -1, 0.5, -0.5, 0, 1][k % 6] for k in range(12)]  # Mid drifts from Alpha late on
    mid = [x + y for x, y in zip(a, dev, strict=True)]  # rho 0.96 with Alpha over its history, 0.94 over Late's
    late = mid  # exactly Mid, so only a chained proxy could extend it
    ids, months = _setup(c, {"Alpha": (_prices(a), 10), "Mid": (_young(mid, 12), 50), "Late": (_young(late, 24), 2)})
    d = c.get("/api/portfolio/risk?period=3Y").json()
    assert [p["name"] for p in d["proxies"]] == ["Mid"]
    assert d["window"]["months"] == 36 and [x["name"] for x in d["excluded"]] == ["Late"]


def test_correlation_cells_use_real_months_and_flag_the_fallback() -> None:
    rng = np.random.default_rng(1)
    x, y = rng.normal(0, 0.03, 36), rng.normal(0, 0.03, 36)
    y[:20] = x[:20]  # the spliced months mirror x perfectly; the real months are independent
    ok = risk._matrix(["x", "y"], [x, y], [0, 20])  # 16 real months together: use only those
    assert ok["extended"] == [False, False] and near(ok["matrix"][0][1], np.corrcoef(x[20:], y[20:])[0, 1])
    short = risk._matrix(["x", "y"], [x, y], [0, 30])  # 6 real months: fall back to the spliced series, flag y
    assert short["extended"] == [False, True] and near(short["matrix"][0][1], np.corrcoef(x, y)[0, 1])


# ---- total return -----------------------------------------------------------------------------------


def _book_with(db: Any, prices: dict[date, str], divs: list[tuple[date, str]]) -> tuple[Any, int, Any]:
    from decimal import Decimal

    from app.modules.core.models import Instrument
    from app.modules.pricing.distributions import Dist, DistSeries
    from app.modules.pricing.models import Price
    from app.modules.pricing.service import PriceBook

    inst = Instrument(type="etf", name="Payer", currency="EUR")
    db.add(inst)
    db.flush()
    for d, p in prices.items():
        db.add(Price(instrument_id=inst.id, date=d, close=Decimal(p), currency="EUR", source="yahoo"))
    db.flush()
    return PriceBook(db, {inst.id}), inst.id, DistSeries("yahoo", [Dist(d, Decimal(a), "EUR") for d, a in divs])


def test_total_return_adds_the_dividend_yield_to_a_flat_price(db: Any) -> None:
    months = risk.month_ends(date(2025, 6, 30), 3)  # 2025-03-31 .. 2025-06-30
    prices = {m: "100" for m in months}
    book, iid, series = _book_with(db, prices, [(date(2025, 5, 15), "2")])  # 2 % of the 100 before it
    plain = risk.monthly_values(book, iid, months)
    tr = risk.total_return_values(book, iid, months, plain, series)
    r = risk._returns(tr, 0)
    assert np.allclose(r, [0.0, 0.02, 0.0])  # price return 0 + 2 % in the ex-dividend month
    assert risk.total_return_values(book, iid, months, plain, None) == plain  # accumulating: unchanged


def test_total_return_compounds_with_price_moves_and_skips_unpriced_dividends(db: Any) -> None:
    months = risk.month_ends(date(2025, 6, 30), 3)
    prices = {months[0]: "100", months[1]: "100", months[2]: "110", months[3]: "110"}
    book, iid, series = _book_with(db, prices, [(months[2] - timedelta(days=3), "2"), (date(2020, 1, 1), "5")])
    tr = risk.total_return_values(book, iid, months, risk.monthly_values(book, iid, months), series)
    r = risk._returns(tr, 0)
    assert near(r[1], 1.1 * 1.0 / 1.0 * (1 + 2 / 100) - 1)  # month 2: price 100 → 110, dividend 2 on P_prev 100
    assert near(r[0], 0.0) and near(r[2], 0.0)  # the 2020 dividend has no earlier price: skipped


def test_risk_report_includes_dividends_and_says_where_from(auth_client: TestClient) -> None:
    from decimal import Decimal

    from app.modules.ledger.service import TxnSpec, record
    from app.modules.pricing.models import Distribution
    from app.platform.db import session_factory

    c = auth_client
    flat: list[float | None] = [100.0] * 37
    ids, months = _setup(c, {"Yahoo payer": (flat, 10), "Ledger payer": (flat, 10), "Accumulator": (flat, 10)})
    silent = c.post("/api/instruments", json={"currency": "EUR", "type": "etf", "name": "World Dist UCITS ETF"}).json()[
        "id"
    ]
    acct = c.get("/api/accounts").json()[0]["id"]
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct,
            "date": months[0].isoformat(),
            "kind": "buy",
            "amount": "-100",
            "instrument_id": silent,
            "quantity": "1",
        },
    )
    c.post("/api/prices", json={"instrument_id": silent, "date": months[-1].isoformat(), "close": "100"})
    for m in months:
        c.post("/api/prices", json={"instrument_id": silent, "date": m.isoformat(), "close": "100"})
    ex = months[10].replace(day=15)
    with session_factory()() as s:
        s.add(
            Distribution(
                instrument_id=ids["Yahoo payer"], ex_date=ex, amount=Decimal("2"), currency="EUR", source="yahoo"
            )
        )
        record(
            s,
            TxnSpec(
                account_id=acct,
                date=ex,
                kind="dividend",
                currency="EUR",
                amount=Decimal("20"),
                instrument_id=ids["Ledger payer"],
            ),
        )
        s.commit()
    d = c.get("/api/portfolio/risk?period=3Y&rf=0").json()
    assert d["dividends"]["yahoo"] == 1 and d["dividends"]["ledger"] == 1
    assert [m["name"] for m in d["dividends"]["missing"]] == ["World Dist UCITS ETF"]
    assert (
        "Returns include dividends (reinvested) where known: "
        "from Yahoo for 1 holdings, from your own dividend bookings for 1." in d["notes"]
    )
    assert any(n.startswith("No dividend data for distributing holdings") and "World Dist" in n for n in d["notes"])
    assert not any("Prices exclude" in n for n in d["notes"])
    # holdings worth 1000 + 1000 gained 2 % once, out of 3100 in all
    assert near(d["metrics"]["total_return"], 0.02 * 2000 / 3100, 1e-6)
