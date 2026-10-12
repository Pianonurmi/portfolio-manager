import pytest
from fastapi.testclient import TestClient

from app.modules.core.models import Instrument
from app.modules.portfolio import benchmarks as bm
from app.platform.db import session_factory

from .test_risk import _prices, _rets, _setup


def test_mix_rebalances_monthly() -> None:
    s = {"A": [1.0, 1.1, 1.21], "B": [1.0, 1.0, 1.0]}
    m = bm._mix(s, {"A": 0.5, "B": 0.5}, 3)  # type: ignore[arg-type]
    assert m == pytest.approx([1.0, 1.05, 1.05 * 1.05])
    assert bm._mix({"A": [None, 1.0], "B": [1.0, 1.0]}, {"A": 0.5, "B": 0.5}, 2) == [None, 1.0]


def test_benchmark_comparison(auth_client: TestClient) -> None:
    c = auth_client
    ids, months = _setup(
        c, {"Mine": (_prices(_rets([0.01, -0.005])), 50), "ACWI IMI ETF": (_prices(_rets([0.008])), 1)}
    )
    with session_factory()() as db:
        inst = db.get(Instrument, ids["ACWI IMI ETF"])
        assert inst is not None
        inst.meta = {**inst.meta, "benchmark": "ACWI_IMI"}
        db.commit()
    d = c.get("/api/portfolio/benchmarks?period=3Y").json()
    assert d["portfolio"]["cagr"] > 0 and len(d["portfolio"]["index"]) == len(d["months"])
    acwi = next(r for r in d["benchmarks"] if r["key"] == "ACWI_IMI")
    assert acwi["available"] and acwi["cagr"] == pytest.approx(1.008**12 - 1, rel=0.02)
    assert acwi["volatility"] == pytest.approx(0, abs=1e-9) and acwi["max_drawdown"] == 0
    assert next(r for r in d["benchmarks"] if r["key"] == "SP500")["available"] is False  # no prices for it
    assert next(r for r in d["benchmarks"] if r["key"] == "MIX_60_40")["available"] is False
    assert c.get("/api/portfolio/benchmarks?period=2Y").status_code == 422
    assert c.post("/api/portfolio/benchmarks/refresh").status_code == 200
