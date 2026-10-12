import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.modules.networth.models import IndexSeries
from app.modules.portfolio import factors as ff
from app.modules.portfolio import risk
from app.modules.pricing.models import FxRate
from app.platform.db import session_factory, today, utcnow

from .test_risk import _setup

SAMPLE5 = (
    "This file was created using the 202608 Bloomberg database.\r\n"
    "\r\n"
    "Missing data are indicated by -99.99.\r\n"
    "\r\n"
    "\r\n"
    "\r\n"
    ",Mkt-RF,SMB,HML,RMW,CMA,RF\r\n"
    "199007    ,0.77    ,0.60   ,-0.36    ,0.21    ,1.35    ,0.68\r\n"
    "199008    ,-99.99  ,0.10   ,0.20    ,0.30    ,0.40    ,0.60\r\n"
    "199009    ,-2.50   ,-1.00  ,0.05    ,-0.10   ,0.00    ,0.55\r\n"
    "\r\n"
    " Annual Factors: January-December \r\n"
    ",Mkt-RF,SMB,HML,RMW,CMA,RF\r\n"
    "1990   ,-9.99  ,1.00  ,2.00    ,3.00   ,4.00    ,5.00\r\n"
    "1991   ,19.99  ,1.00  ,2.00    ,3.00   ,4.00    ,5.00\r\n"
)
SAMPLE_MOM = (
    "Header line\r\n\r\n,WML\r\n199011    ,0.14\r\n199012    ,-1.20\r\n\r\n"
    " Annual Factors: January-December \r\n,WML\r\n1991   ,3.00\r\n"
)


def test_parse_monthly_reads_only_the_monthly_block() -> None:
    d = ff.parse_monthly(SAMPLE5)
    assert set(d) == {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"}
    assert d["Mkt-RF"] == {"1990-07": 0.77, "1990-09": -2.5}  # -99.99 is missing, annual rows are not months
    assert d["RF"] == {"1990-07": 0.68, "1990-08": 0.6, "1990-09": 0.55}
    assert d["HML"]["1990-07"] == -0.36
    assert ff.parse_monthly(SAMPLE_MOM) == {"WML": {"1990-11": 0.14, "1990-12": -1.2}} or ff.parse_monthly(
        SAMPLE_MOM
    ) == {"WML": {"1990-11": 0.14, "1990-12": -1.2}}


def test_parse_monthly_rejects_garbage() -> None:
    with pytest.raises(ff.FactorDataError):
        ff.parse_monthly("<html>moved</html>")
    with pytest.raises(ff.FactorDataError):
        ff.parse_monthly(",A,B\n")


def test_unzip_csv_is_defensive() -> None:
    def zipped(files: dict[str, str]) -> bytes:
        b = BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n, t in files.items():
                z.writestr(n, t)
        return b.getvalue()

    assert ff.unzip_csv(zipped({"x.csv": SAMPLE_MOM})).startswith("Header")
    with pytest.raises(ff.FactorDataError):
        ff.unzip_csv(zipped({"a.csv": "x", "b.csv": "y"}))
    with pytest.raises(ff.FactorDataError):
        ff.unzip_csv(b"not a zip")
    with pytest.raises(ff.FactorDataError):
        ff.unzip_csv(zipped({"big.csv": "x" * (ff.MAX_ZIP + 1)}))


def test_ols_recovers_known_loadings() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.04, (120, 3))
    y = 0.002 + x @ np.array([1.1, 0.4, -0.3]) + rng.normal(0, 0.001, 120)
    res = ff.ols(y, x)
    assert res["beta"] == pytest.approx([0.002, 1.1, 0.4, -0.3], abs=0.01)
    assert res["r2"] > 0.99 and (abs(res["t"][1:]) > 10).all()
    noise = ff.ols(rng.normal(0, 0.04, 120), x)
    assert noise["r2"] < 0.1 and (abs(noise["t"][1:]) < 3.5).all()


def test_eur_to_usd_chains_the_currency_move() -> None:
    r = np.array([0.02, -0.01])
    out = ff.eur_to_usd(r, [1.10, 1.21, 1.21])  # EUR gains 10 % on USD in month one, nothing in month two
    assert out == pytest.approx([1.02 * 1.1 - 1, 0.99 - 1])
    assert np.isnan(ff.eur_to_usd(r, [1.10, None, 1.21])).all()


def test_interpretation_uses_thresholds() -> None:
    fit = {
        "loadings": {"Mkt-RF": 0.98, "SMB": -0.2, "HML": 0.05, "RMW": 0.3, "CMA": 0.3, "WML": 0.0},
        "t": {"Mkt-RF": 40.0, "SMB": -4.0, "HML": 3.0, "RMW": 1.0, "CMA": 2.5, "WML": 0.1},
        "alpha": 0.012,
        "alpha_t": 0.8,
        "r2": 0.55,
    }
    text = {x["factor"]: x["text"] for x in ff.interpret(fit)}
    assert text["Mkt-RF"] == "Market beta 0.98: moves with developed markets"
    assert text["SMB"] == "Size −0.20: tilted to large companies"
    assert "no clear tilt" in text["HML"] and "no clear tilt" in text["RMW"]  # too small / too noisy
    assert "conservatively investing" in text["CMA"]
    assert "not distinguishable from zero" in text["alpha"] and "not equity-factor risk" in text["r2"]


# ---- endpoint on seeded factor data ----------------------------------------------------------------


def _seed_factors(months: list[str], f: dict[str, np.ndarray]) -> None:
    with session_factory()() as s:
        for name, group in ff.SOURCE_OF.items():
            s.add(
                IndexSeries(
                    key=ff.key_of(group, name),
                    label=name,
                    unit="pct_month",
                    points={m: float(v) for m, v in zip(months, f[name], strict=True)},
                    trades={},
                    fetched_at=utcnow(),
                )
            )
        s.commit()


def _seed_usd(dates: list[date]) -> None:
    with session_factory()() as s:
        s.add_all([FxRate(quote="USD", date=d, rate=Decimal("1.1")) for d in dates])
        s.commit()


def _factor_world(n: int = 60) -> tuple[list[date], list[str], dict[str, np.ndarray]]:
    dates = risk.month_ends(risk.last_month_end(today()), n)
    months = [f"{d:%Y-%m}" for d in dates[1:]]
    rng = np.random.default_rng(3)
    f = {name: rng.normal(0.4, 3.0, n) for name in ff.FACTORS}
    f["RF"] = np.full(n, 0.2)
    return dates, months, f


def _prices_from(dates: list[date], months: list[str], f: dict[str, np.ndarray], load: dict[str, float]) -> list[float]:
    p = [100.0]
    for k in range(len(months)):
        r = f["RF"][k] / 100 + sum(b * f[n][k] / 100 for n, b in load.items()) + 0.0005 * ((-1) ** k)
        p.append(p[-1] * (1 + r))
    return p


def test_factor_endpoint_recovers_portfolio_and_holding_loadings(auth_client: TestClient) -> None:
    c = auth_client
    dates, months, f = _factor_world()
    value_load = {"Mkt-RF": 1.0, "SMB": 0.5, "HML": 0.4}
    growth_load = {"Mkt-RF": 1.0, "SMB": -0.3, "HML": -0.4}
    _setup(
        c,
        {
            "Value tilt": (_prices_from(dates, months, f, value_load), 50),
            "Growth tilt": (_prices_from(dates, months, f, growth_load), 50),
        },
        n=60,
    )
    _seed_factors(months, f)
    _seed_usd(dates)  # a flat 1.10: EUR returns are USD returns
    r = c.get("/api/portfolio/factors?period=MAX")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "ok" and d["window"]["months"] == 60
    p = d["portfolio"]
    assert p["loadings"]["Mkt-RF"] == pytest.approx(1.0, abs=0.03)
    by = {h["name"]: h for h in d["holdings"]}
    wv, wg = by["Value tilt"]["weight"], by["Growth tilt"]["weight"]  # today's weights, monthly rebalanced
    assert p["loadings"]["SMB"] == pytest.approx(0.5 * wv - 0.3 * wg, abs=0.03)
    assert p["loadings"]["HML"] == pytest.approx(0.4 * wv - 0.4 * wg, abs=0.03)
    assert p["r2"] > 0.98 and p["alpha"] == pytest.approx(0.0, abs=0.01)
    assert by["Value tilt"]["loadings"]["HML"] == pytest.approx(0.4, abs=0.03)
    assert by["Growth tilt"]["loadings"]["SMB"] == pytest.approx(-0.3, abs=0.03)
    assert sum(h["weight"] for h in d["holdings"]) == pytest.approx(1.0)
    assert any(x["text"].startswith("Market beta 1.0") for x in d["interpretation"])
    assert d["data"]["last_month"] == months[-1] and any("French" in n for n in d["notes"])


def test_factor_endpoint_explains_too_short_overlap(auth_client: TestClient) -> None:
    c = auth_client
    dates, months, f = _factor_world(n=24)
    _setup(c, {"Short": (_prices_from(dates, months, f, {"Mkt-RF": 1.0}), 50)}, n=24)
    _seed_factors(months, f)
    _seed_usd(dates)
    d = c.get("/api/portfolio/factors?period=MAX").json()
    assert d["status"] == "insufficient" and d["portfolio"] is None
    assert "24 months overlap" in d["explanation"] and "36" in d["explanation"]


def test_factor_endpoint_without_data_or_network_says_so(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = auth_client
    dates, months, f = _factor_world(n=40)
    _setup(c, {"Any": (_prices_from(dates, months, f, {"Mkt-RF": 1.0}), 50)}, n=40)

    def boom(*a: Any, **k: Any) -> None:
        raise ff.FactorDataError("offline")

    monkeypatch.setattr(ff, "refresh_factors", boom)
    r = c.get("/api/portfolio/factors")
    assert r.status_code == 503 and "offline" in r.json()["detail"]
    assert c.get("/api/portfolio/factors?period=2D").status_code == 422
