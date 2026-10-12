"""Benchmarks to compare the portfolio's own return with: common indices, tracked by accumulating EUR UCITS ETFs.

An ETF stands in for each index, so the comparison is with what you could actually have bought: dividends
reinvested, the fund's costs paid, in euros. The ETFs are instruments marked meta["benchmark"] and priced by the
normal price job. The portfolio's side is its time-weighted return (deposits and withdrawals do not count as
performance), month by month over the same window.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import INVESTMENT_KINDS, Instrument
from app.modules.portfolio import risk
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing import distributions as dist
from app.modules.pricing.service import PriceBook

PERIOD_MONTHS = {"1Y": 12, "3Y": 36, "5Y": 60, "10Y": 120, "MAX": 240}


@dataclass(frozen=True)
class Bench:
    key: str
    name: str
    isin: str
    fund: str  # the ETF that tracks it
    group: str


BENCHMARKS = [
    Bench("ACWI_IMI", "MSCI ACWI IMI", "IE00B3YLTY66", "SPDR MSCI ACWI IMI UCITS ETF", "Global equity"),
    Bench("ACWI", "MSCI ACWI", "IE00B6R52259", "iShares MSCI ACWI UCITS ETF (Acc)", "Global equity"),
    Bench("FTSE_AW", "FTSE All-World", "IE00BK5BQT80", "Vanguard FTSE All-World UCITS ETF (Acc)", "Global equity"),
    Bench("WORLD", "MSCI World", "IE00B4L5Y983", "iShares Core MSCI World UCITS ETF (Acc)", "Global equity"),
    Bench("WORLD_SC", "MSCI World Small Cap", "IE00BF4RFH31", "iShares MSCI World Small Cap UCITS ETF",
          "Global equity"),
    Bench("SP500", "S&P 500", "IE00B5BMR087", "iShares Core S&P 500 UCITS ETF (Acc)", "Regions"),
    Bench("NDX", "Nasdaq 100", "IE00B53SZB19", "iShares Nasdaq 100 UCITS ETF (Acc)", "Regions"),
    Bench("EUROPE", "MSCI Europe", "IE00B4K48X80", "iShares Core MSCI Europe UCITS ETF (Acc)", "Regions"),
    Bench("EM_IMI", "MSCI EM IMI", "IE00BKM4GZ66", "iShares Core MSCI EM IMI UCITS ETF (Acc)", "Regions"),
    Bench("OMXH25", "OMX Helsinki 25", "FI0008805627", "Seligson & Co OMX Helsinki 25 UCITS ETF", "Regions"),
    Bench("GLOBAL_AGG", "Bloomberg Global Aggregate (EUR hedged)", "IE00BDBRDM35",
          "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)", "Bonds"),
]  # fmt: skip
BY_KEY = {b.key: b for b in BENCHMARKS}
MIXES = {"MIX_60_40": ("60/40: ACWI IMI and global bonds", {"ACWI_IMI": 0.6, "GLOBAL_AGG": 0.4})}


def ensure(db: Session) -> dict[str, Instrument]:
    """The benchmark ETFs as instruments (an existing holding with the same ISIN is reused and marked)."""
    out: dict[str, Instrument] = {}
    for b in BENCHMARKS:
        inst = db.scalar(select(Instrument).where(Instrument.isin == b.isin))
        if inst is None:
            inst = Instrument(type="etf", name=b.fund, isin=b.isin, currency="EUR", meta={})
            db.add(inst)
        if (inst.meta or {}).get("benchmark") != b.key:
            inst.meta = {**(inst.meta or {}), "benchmark": b.key}
        out[b.key] = inst
    db.flush()
    return out


def _twr_monthly(db: Session, scope: Scope, months: list[date]) -> list[float | None]:
    """The portfolio's time-weighted growth index at each month end (None before it existed)."""
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    if v.first_date is None:
        return [None] * len(months)
    dates, values, flows = v.daily(v.first_date, months[-1])
    idx, acc = {}, 1.0
    for i in range(len(dates)):
        if i and float(values[i - 1]) >= 1.0:
            acc *= float((values[i] - flows[i]) / values[i - 1])
        idx[dates[i]] = acc
    out: list[float | None] = []
    for m in months:
        d = m
        while d not in idx and d >= dates[0]:
            d -= timedelta(days=1)
        out.append(idx.get(d) if d >= dates[0] and float(values[dates.index(d)]) > 0 else None)
    return out


def _stats(vals: list[float], rf: float) -> dict[str, float | None]:
    r = np.array(vals[1:]) / np.array(vals[:-1]) - 1
    years = len(r) / 12
    total = vals[-1] / vals[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years >= 1 else total
    vol = float(r.std(ddof=1) * math.sqrt(12)) if len(r) > 2 else 0.0
    wealth = np.cumprod(1 + r)
    dd = float(np.min(wealth / np.maximum.accumulate(np.concatenate([[1.0], wealth]))[1:] - 1)) if len(r) else 0.0
    return {
        "total": total,
        "cagr": cagr,
        "volatility": vol,
        "max_drawdown": min(dd, 0.0),
        "sharpe": (cagr - rf) / vol if vol > 0 else None,
    }


def _mix(series: dict[str, list[float | None]], weights: dict[str, float], n: int) -> list[float | None]:
    """A growth index of the weights rebalanced every month; None until every part has a price."""
    out: list[float | None] = [None] * n
    for i in range(n):
        parts = [series.get(k, [None] * n)[i] for k in weights]
        if any(x is None for x in parts):
            continue
        prev = out[i - 1] if i else None
        if prev is None:
            out[i] = 1.0
            continue
        g = 0.0
        for k, w in weights.items():
            a, b = series[k][i - 1], series[k][i]
            g += w * (float(b) / float(a)) if a and b else w
        out[i] = prev * g
    return out


def compare(db: Session, scope: Scope, on: date, period: str, rf: float) -> dict[str, Any]:
    if period not in PERIOD_MONTHS:
        raise ValueError(f"period must be one of {', '.join(PERIOD_MONTHS)}")
    insts = {
        str(i.meta["benchmark"]): i
        for i in db.scalars(select(Instrument).where(Instrument.meta["benchmark"].as_string().is_not(None)))
    }
    months = risk.month_ends(risk.last_month_end(on), PERIOD_MONTHS[period])
    mine = _twr_monthly(db, scope, months)
    start = next((k for k, x in enumerate(mine) if x is not None), None)
    if start is None or len(months) - start < 3:
        return {"period": period, "months": [], "portfolio": None, "benchmarks": [], "notes": ["Not enough history."]}
    book = PriceBook(db, {i.id for i in insts.values()})
    dists = dist.distribution_series(db, [i.id for i in insts.values()])
    series: dict[str, list[float | None]] = {}
    for key, inst in insts.items():
        series[key] = risk.total_return_values(
            book, inst.id, months, risk.monthly_values(book, inst.id, months), dists.get(inst.id)
        )
    # a benchmark counts from where both it and the portfolio have values; shorter ones are compared on their own span
    ms = months[start:]
    port = [float(x) for x in mine[start:]]  # type: ignore[arg-type]
    pr = np.array(port[1:]) / np.array(port[:-1]) - 1
    rows: list[dict[str, Any]] = []

    def row(key: str, name: str, fund: str | None, group: str, vals: list[float | None]) -> None:
        v = vals[start:]
        first = next((k for k, x in enumerate(v) if x is not None), None)
        if first is None or any(x is None for x in v[first:]) or len(v) - first < 3:
            rows.append({"key": key, "name": name, "fund": fund, "group": group, "available": False})
            return
        bv = [float(x) for x in v[first:]]  # type: ignore[arg-type]
        br = np.array(bv[1:]) / np.array(bv[:-1]) - 1
        p_span = port[first:]
        p_stats = _stats(p_span, rf)
        b_stats = _stats(bv, rf)
        prs = pr[first:]
        beta = float(np.cov(prs, br)[0, 1] / np.var(br, ddof=1)) if len(br) > 2 and np.var(br) > 0 else None
        corr = float(np.corrcoef(prs, br)[0, 1]) if len(br) > 2 else None
        te = float((prs - br).std(ddof=1) * math.sqrt(12)) if len(br) > 2 else None
        rows.append(
            {
                "key": key,
                "name": name,
                "fund": fund,
                "group": group,
                "available": True,
                "from": ms[first],
                **b_stats,
                "excess_cagr": float(p_stats["cagr"] or 0) - float(b_stats["cagr"] or 0),
                "beta": beta,
                "correlation": corr,
                "tracking_error": te,
                "index": [x / bv[0] for x in bv],
            }
        )

    for b in BENCHMARKS:
        row(b.key, b.name, b.fund, b.group, series.get(b.key, [None] * len(months)))
    for key, (name, weights) in MIXES.items():
        row(key, name, None, "Mixes", _mix(series, weights, len(months)))
    return {
        "period": period,
        "months": ms,
        "portfolio": {**_stats(port, rf), "index": [x / port[0] for x in port]},
        "benchmarks": rows,
        "notes": [
            "Each index is represented by an accumulating EUR UCITS ETF that tracks it: dividends reinvested and the "
            "fund's costs paid, in euros, which is what you could actually have bought.",
            "Your portfolio's return is time-weighted: deposits and withdrawals are not counted as performance.",
            "Beta, correlation and tracking error compare your monthly returns with the benchmark's over its span.",
        ],
    }
