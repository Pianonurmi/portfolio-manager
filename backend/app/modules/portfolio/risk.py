"""Risk analytics of the portfolio as it is today: today's weights applied to each holding's monthly EUR returns.

This is a "current-portfolio backtest" (monthly rebalanced), not the user's realised history; it answers how risky
today's mix has been. Statistics are floats (numpy); money stays Decimal.
Returns are total returns: known dividends are reinvested.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import INVESTMENT_KINDS, Instrument
from app.modules.portfolio import profiles
from app.modules.portfolio.allocation import classify
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing import distributions as dist
from app.modules.pricing.providers import morningstar
from app.modules.pricing.service import PriceBook

Arr = NDArray[np.float64]
MIN_PRICED_SHARE = Decimal("0.5")  # below this share of value with a price series, no report at all
PERIOD_MONTHS = {"1Y": 12, "3Y": 36, "5Y": 60, "10Y": 120, "MAX": 240}
STALE_DAYS = 10  # a month-end price older than this is "no price", not a flat month
TOP_N = 12
CENT = Decimal("0.01")
OTHER = "Other holdings"
MIN_RETURNS = 6  # fewer monthly returns than this say nothing about risk
MIN_OVERLAP = 12  # monthly returns a proxy and a holding must share, and real months a correlation cell needs
MIN_PROXY_CORR = 0.95  # a proxy must track the holding at least this closely

NOTES = [
    "Today's weights are applied to past monthly returns and rebalanced monthly, not your realised history.",
]


def last_month_end(on: date) -> date:
    """The last completed month end (`on` itself when it is one)."""
    if (on + timedelta(days=1)).month != on.month:
        return on
    return on.replace(day=1) - timedelta(days=1)


def month_ends(end: date, count: int) -> list[date]:
    """`count` + 1 month ends ending at `end`, oldest first (count returns need count + 1 values)."""
    out = [end]
    while len(out) <= count:
        out.append(out[-1].replace(day=1) - timedelta(days=1))
    return out[::-1]


def monthly_values(book: PriceBook, instrument_id: int, months: list[date]) -> list[float | None]:
    """EUR value per unit at each month end; None before the first price or when the last price is too old."""
    out: list[float | None] = []
    for d in months:
        p = book.price(instrument_id, d)
        if p is None or (d - p.date).days > STALE_DAYS:
            out.append(None)
            continue
        try:
            unit, _, _ = book.unit_value_eur(instrument_id, d)
        except LookupError:  # no FX rate for its currency
            out.append(None)
            continue
        unit = unit / book.split_factor(instrument_id, d)  # per unit after any later split: returns stay comparable
        out.append(float(unit) if unit > 0 else None)
    return out


def total_return_values(
    book: PriceBook, instrument_id: int, months: list[date], values: list[float | None], series: dist.DistSeries | None
) -> list[float | None]:
    """`values` grown by reinvested distributions: TR = price × Π(1 + D / P_prev) over ex-dates up to each month end.

    P_prev is the last price before the ex-date, D is taken in the price's currency. A distribution with no earlier
    price or no FX rate is skipped. Without distributions (accumulating funds) the values are returned unchanged.
    """
    if series is None or not series.rows:
        return values
    ratios: list[tuple[date, float]] = []
    for x in sorted(series.rows, key=lambda x: x.date):
        p = book.price(instrument_id, x.date - timedelta(days=1))
        if p is None or p.close <= 0:
            continue
        try:
            amount = x.amount
            if x.currency != p.currency:
                amount = amount * book.eur_per(x.currency, x.date)[0] / book.eur_per(p.currency, x.date)[0]
        except LookupError:
            continue
        ratios.append((x.date, float(amount / p.close)))
    out: list[float | None] = []
    k, growth = 0, 1.0
    for d, v in zip(months, values, strict=True):
        while k < len(ratios) and ratios[k][0] <= d:
            growth *= 1 + ratios[k][1]
            k += 1
        out.append(None if v is None else v * growth)
    return out


def distributing_hint(inst: Instrument) -> bool:
    """A fund or ETF whose name says it pays out (Dist, Distributing, Inc, Income); shares are never guessed."""
    if inst.type not in ("fund", "etf"):
        return False
    _, markers = morningstar.fund_words(inst.name, inst.currency)
    return "dist" in markers or "income" in morningstar._tokens(inst.name)


def history_start(values: list[float | None]) -> int | None:
    """Index from which the series is priced without gaps up to the end; None if the end itself has no price."""
    if not values or values[-1] is None:
        return None
    i = len(values) - 1
    while i > 0 and values[i - 1] is not None:
        i -= 1
    return i


def _f(x: float | np.floating[Any] | None) -> float | None:
    """JSON-safe float: NaN and infinities become null."""
    return None if x is None or not np.isfinite(x) else float(x)


def _std(x: Arr) -> float:
    return float(np.std(x, ddof=1)) if len(x) > 1 else 0.0


def _corr(a: Arr, b: Arr) -> float | None:
    if _std(a) < 1e-12 or _std(b) < 1e-12:
        return None
    return _f(np.corrcoef(a, b)[0, 1])


def correlation_matrix(cols: Arr) -> list[list[float | None]]:
    """Pearson correlations of the columns; a column with no variance has none (null), the diagonal is 1."""
    k = cols.shape[1]
    return [[1.0 if i == j else _corr(cols[:, i], cols[:, j]) for j in range(k)] for i in range(k)]


def risk_contributions(cols: Arr, w: Arr) -> tuple[Arr, Arr]:
    """(share of portfolio variance per column summing to 1, annualised volatility per column)."""
    cov = np.atleast_2d(np.cov(cols, rowvar=False, ddof=1))
    var = float(w @ cov @ w)
    vol = np.sqrt(np.diag(cov)) * np.sqrt(12)
    if var <= 1e-18:
        return np.zeros(len(w)), vol
    return w * (cov @ w) / var, vol


def drawdowns(r: Arr) -> tuple[Arr, Arr]:
    """(wealth path starting at 1, drawdown from the running peak at each point)."""
    wealth = np.concatenate(([1.0], np.cumprod(1 + r)))
    return wealth, wealth / np.maximum.accumulate(wealth) - 1


def metrics(r: Arr, dates: list[date], rf: float, bench: Arr | None = None) -> dict[str, Any]:
    """Risk and return statistics of monthly returns `r`; `dates` are the T + 1 month ends around them."""
    t = len(r)
    rf_m = (1 + rf) ** (1 / 12) - 1
    wealth, dd = drawdowns(r)
    cagr = float(wealth[-1]) ** (12 / t) - 1
    vol = _std(r) * np.sqrt(12)
    excess = float(np.mean(r)) - rf_m
    down = float(np.sqrt(np.mean(np.minimum(r - rf_m, 0) ** 2))) * np.sqrt(12)
    trough = int(np.argmin(dd))
    max_dd: dict[str, Any] = {
        "value": float(dd[trough]),
        "peak": None,
        "trough": None,
        "recovery": None,
        "recovered": True,
    }
    if dd[trough] < 0:
        peak = int(np.argmax(wealth[: trough + 1]))
        rec = next((i for i in range(trough + 1, t + 1) if wealth[i] >= wealth[peak]), None)
        max_dd.update(
            peak=dates[peak],
            trough=dates[trough],
            recovery=None if rec is None else dates[rec],
            recovered=rec is not None,
        )
    longest = run = 0
    for x in dd:
        run = run + 1 if x < 0 else 0
        longest = max(longest, run)
    q = float(np.percentile(r, 5))
    tail = r[r <= q]
    best, worst = int(np.argmax(r)), int(np.argmin(r))
    years: dict[int, list[float]] = defaultdict(list)
    for i, x in enumerate(r):
        years[dates[i + 1].year].append(float(x))
    full = {y: float(np.prod(1 + np.array(xs)) - 1) for y, xs in years.items() if len(xs) == 12}
    out: dict[str, Any] = {
        "months": t,
        "cagr": _f(cagr),
        "total_return": float(wealth[-1] - 1),
        "volatility": _f(vol),
        "sharpe": _f(excess * 12 / vol) if vol > 1e-12 else None,
        "sortino": _f(excess * 12 / down) if down > 1e-12 else None,
        "max_drawdown": max_dd,
        "longest_drawdown_months": longest,
        "best_month": {"date": dates[best + 1], "value": float(r[best])},
        "worst_month": {"date": dates[worst + 1], "value": float(r[worst])},
        "best_year": None,
        "worst_year": None,
        "var_95": max(0.0, -q),
        "cvar_95": max(0.0, -float(np.mean(tail))),
        "positive_months": float(np.mean(r > 0)),
    }
    if full:
        by, wy = max(full, key=lambda y: full[y]), min(full, key=lambda y: full[y])
        out["best_year"], out["worst_year"] = {"year": by, "value": full[by]}, {"year": wy, "value": full[wy]}
    if bench is not None:
        vb = float(np.var(bench, ddof=1))
        beta = float(np.cov(r, bench, ddof=1)[0, 1] / vb) if vb > 1e-18 else None
        up, dn = bench > 0, bench < 0
        out["benchmark"] = {
            "beta": _f(beta),
            "correlation": _corr(r, bench),
            "alpha": None
            if beta is None
            else _f((float(np.mean(r)) - rf_m - beta * (float(np.mean(bench)) - rf_m)) * 12),
            "tracking_error": _f(_std(r - bench) * np.sqrt(12)),
            "up_capture": _f(np.mean(r[up]) / np.mean(bench[up])) if up.any() else None,
            "down_capture": _f(np.mean(r[dn]) / np.mean(bench[dn])) if dn.any() else None,
        }
    return out


def _returns(values: list[float | None], s: int) -> Arr:
    """Monthly returns from index s on; the values must be priced from s."""
    v = np.array(values[s:], dtype=float)
    return np.asarray(v[1:] / v[:-1] - 1, dtype=np.float64)


def _best_proxy(
    own: list[float | None], s0: int, cands: dict[int, list[float | None]]
) -> tuple[int, float, int] | None:
    """(id, correlation, overlap) of the candidate that tracks `own` (priced from s0) best and reaches back before s0.

    Ties on correlation go to the longer history. Fewer than MIN_OVERLAP shared returns or a correlation under
    MIN_PROXY_CORR is no proxy at all: a loose stand-in would invent risk rather than fill a gap.
    """
    mine = _returns(own, s0)
    if len(mine) < MIN_OVERLAP:
        return None
    best: tuple[tuple[float, int], int, float] | None = None
    for pid, pv in cands.items():
        ps = history_start(pv)
        if ps is None or ps >= s0:
            continue
        c = _corr(mine, _returns(pv, s0))
        if c is None or c < MIN_PROXY_CORR:
            continue
        key = (round(c, 9), -ps)
        if best is None or key > best[0]:
            best = (key, pid, c)
    return None if best is None else (best[1], best[2], len(mine))


def _splice(own: list[float | None], s0: int, pv: list[float | None]) -> list[float | None]:
    """`own` with the months before s0 filled by running the proxy's monthly returns backwards from own[s0].

    value_{t-1} = value_t / (1 + r_proxy_t) is the same as scaling the proxy's values to meet own[s0]. Months before
    the proxy's own history stay empty.
    """
    ps = history_start(pv)
    first = own[s0]
    anchor = pv[s0]
    assert ps is not None and first is not None and anchor is not None
    out: list[float | None] = [None] * ps + [first * x / anchor for x in pv[ps:s0] if x is not None] + own[s0:]
    return out


def risk_report(
    db: Session,
    scope: Scope,
    period: str,
    rf: float,
    on: date,
    benchmark_id: int | None = None,
    min_coverage: float = 0.95,
    proxies: bool = True,
    capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The risk report; `capture`, when given, receives the raw monthly portfolio returns (`r`), month ends and `parts`
    (per-holding columns, ids, instruments, values) for the optimiser."""
    key = period.upper()
    if key not in PERIOD_MONTHS:
        raise ValueError(f"unknown period {period!r}")
    n = PERIOD_MONTHS[key]
    out: dict[str, Any] = {
        "scope": scope.token,
        "period": period,
        "rf": rf,
        "as_of": on,
        "window": None,
        "excluded": [],
        "proxies": [],
        "holdings": [],
        "metrics": None,
        "benchmark": None,
        "series": None,
        "correlations": None,
        "dividends": None,
        "notes": list(NOTES),
    }
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    by_inst: dict[int, Decimal] = defaultdict(Decimal)
    for h in v.holdings(on):
        if h.value > 0:
            by_inst[h.instrument_id] += h.value
    total = sum(by_inst.values(), Decimal(0))
    if not by_inst or total <= 0:
        out["notes"].append("No investment holdings to analyse.")
        return out
    insts = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(by_inst)))}
    months = month_ends(last_month_end(on), n)
    bid = benchmark_id
    live_ids = [i for i in by_inst if insts[i].type != "cash"]
    dists = dist.distribution_series(db, live_ids + ([bid] if bid else []))
    vals = {
        i: total_return_values(v.book, i, months, monthly_values(v.book, i, months), dists.get(i))
        if insts[i].type != "cash"
        else [1.0] * len(months)
        for i in by_inst
    }
    start: dict[int, int | None] = {i: history_start(vals[i]) for i in by_inst}
    bvals: list[float | None] = []
    if bid:
        bbook = v.book if bid in v.book.prices else PriceBook(db, {bid})
        bvals = total_return_values(bbook, bid, months, monthly_values(bbook, bid, months), dists.get(bid))

    # a holding whose history starts inside the window borrows the monthly returns of a close look-alike (another
    # holding or the benchmark) for the months it is missing; a proxy is always a real series, never itself extended
    real_start: dict[int, int] = {}  # extended holding -> index of its first real price
    extended: dict[int, tuple[int, float, int]] = {}
    if proxies:
        raw = {i: vals[i] for i in by_inst if insts[i].type != "cash"}
        if bid and bid not in raw:
            raw[bid] = bvals
        for i in sorted((i for i in raw if (start.get(i) or 0) > 0), key=lambda i: start[i]):  # type: ignore[arg-type,return-value]
            hit = _best_proxy(vals[i], start[i], {p: pv for p, pv in raw.items() if p != i and p not in extended})  # type: ignore[arg-type]
            if hit is not None:
                extended[i] = hit
                real_start[i] = start[i]  # type: ignore[assignment]
                vals[i] = _splice(vals[i], start[i], raw[hit[0]])  # type: ignore[arg-type]
                start[i] = history_start(vals[i])

    # earliest month where enough of the holdings that have a price series today are priced; holdings with no
    # current series at all (a feed missing) are left out and listed rather than blanking the whole report
    priced = sum((by_inst[i] for i in by_inst if start[i] is not None), Decimal(0))
    s = (
        next(
            (
                s
                for s in range(n + 1)
                if sum((by_inst[i] for i in by_inst if (start[i] is not None and start[i] <= s)), Decimal(0))  # type: ignore[operator]
                >= Decimal(str(min_coverage)) * priced
            ),
            None,
        )
        if priced >= MIN_PRICED_SHARE * total
        else None
    )
    if s is None or n - s < MIN_RETURNS:
        out["notes"].append("Not enough price history to measure risk: too little of the portfolio has monthly prices.")
        out["excluded"] = _excluded(v.book, insts, by_inst, start, total, n + 1)
        return out
    kept = [i for i in by_inst if start[i] is not None and start[i] <= s]  # type: ignore[operator]
    covered = sum((by_inst[i] for i in kept), Decimal(0))
    out["excluded"] = _excluded(v.book, insts, by_inst, start, total, s + 1, kept)
    binst = db.get(Instrument, bid) if bid else None
    for i, (pid, c, overlap) in extended.items():
        if i not in kept or s >= real_start[i]:  # left out anyway, or the window starts on its own prices
            continue
        pname = insts[pid].name if pid in insts else binst.name if binst else "?"
        first = max(s, start[i] or 0)
        out["proxies"].append(
            {
                "instrument_id": i,
                "name": insts[i].name,
                "proxy_id": pid,
                "proxy_name": pname,
                "correlation": c,
                "overlap_months": overlap,
                "from": months[first],
                "to": months[real_start[i] - 1],
            }
        )
        out["notes"].append(
            f"{insts[i].name} before {months[real_start[i]]:%Y-%m} uses {pname} (ρ {c:.2f}, {overlap} months overlap)."
        )
    out["dividends"] = _dividend_coverage(db, [i for i in kept if i in live_ids], insts, dists)
    out["notes"].insert(0, _dividend_note(out["dividends"]))
    if out["dividends"]["missing"]:
        names = ", ".join(m["name"] for m in out["dividends"]["missing"])
        out["notes"].insert(
            1, f"No dividend data for distributing holdings, so their returns are understated: {names}."
        )
    if out["excluded"]:
        names = ", ".join(e["name"] for e in out["excluded"])
        out["notes"].append(f"Left out for lack of price history in the window and re-weighted: {names}.")
    if s > 0 and n < 240:
        out["notes"].append(f"The window is shorter than requested ({n - s} of {n} months) because of that.")
    dates = months[s:]
    t = n - s
    out["window"] = {
        "start": dates[0],
        "end": dates[-1],
        "months": t,
        "requested_months": n,
        "coverage": float(covered / total),
        "min_coverage": min_coverage,
    }

    # columns: one per kept instrument, cash returns nothing
    ids = sorted(kept, key=lambda i: -by_inst[i])
    cols = np.column_stack([np.zeros(t) if insts[i].type == "cash" else _returns(vals[i], s) for i in ids]).astype(
        np.float64
    )
    w = np.array([float(by_inst[i] / covered) for i in ids])
    r = cols @ w
    bench_r: Arr | None = None
    if bid:
        bs = history_start(bvals)
        if bs is not None and bs <= s:
            bench_r = _returns(bvals, s)
        out["benchmark"] = {
            "instrument_id": bid,
            "name": binst.name if binst else "?",
            "available": bench_r is not None,
        }
        if bench_r is None:
            out["notes"].append(
                "The benchmark has no price history over this window; benchmark statistics are left out."
            )
    out["metrics"] = metrics(r, dates, rf, bench_r)
    if capture is not None:
        capture.update(
            r=r,
            dates=dates,
            parts={
                "ids": ids,
                "cols": cols,
                "insts": insts,
                "values": by_inst,
                "covered": covered,
                "months": months,
                "start": s,
            },
        )

    wealth, dd = drawdowns(r)
    roll = [float(np.prod(1 + r[i - 12 : i]) - 1) for i in range(12, t + 1)]
    out["series"] = {
        "dates": dates,
        "drawdown": [float(x) for x in dd],
        "rolling_dates": dates[12:],
        "rolling_12m": roll,
    }

    shares, vols = risk_contributions(cols, w)
    out["holdings"] = [
        {
            "instrument_id": i,
            "name": insts[i].name,
            "type": insts[i].type,
            "value": by_inst[i].quantize(CENT),
            "weight": float(w[k]),
            "volatility": _f(vols[k]),
            "risk_share": _f(shares[k]),
        }
        for k, i in enumerate(ids)
    ]
    first_real = [max(0, real_start[i] - s) if i in extended else 0 for i in ids]
    out["correlations"] = _correlations(db, ids, insts, by_inst, cols, w, first_real)
    return out


def _dividend_coverage(
    db: Session, ids: list[int], insts: dict[int, Instrument], dists: dict[int, dist.DistSeries]
) -> dict[str, Any]:
    """How many holdings have dividend data from each source, and which look distributing but have none."""
    booked = dist.ledger_dividend_instruments(db, ids)
    missing = [i for i in ids if i not in dists and (i in booked or distributing_hint(insts[i]))]
    return {
        "yahoo": sum(1 for i in ids if i in dists and dists[i].source == "yahoo"),
        "ledger": sum(1 for i in ids if i in dists and dists[i].source == "ledger"),
        "missing": [{"instrument_id": i, "name": insts[i].name} for i in missing],
    }


def _dividend_note(c: dict[str, Any]) -> str:
    return (
        "Returns include dividends (reinvested) where known: "
        f"from Yahoo for {c['yahoo']} holdings, from your own dividend bookings for {c['ledger']}."
    )


def portfolio_monthly_returns(
    db: Session, scope: Scope, on: date, proxies: bool = True
) -> tuple[list[date], Arr, dict[str, Any] | None]:
    """(month ends of the returns, monthly returns, window info) of today's weights over the longest history.

    The returns feed the Monte Carlo's historical mode; empty when the risk report has nothing to measure.
    """
    cap: dict[str, Any] = {}
    rep = risk_report(db, scope, "MAX", 0.0, on, proxies=proxies, capture=cap)
    if "r" not in cap:
        return [], np.zeros(0), None
    return cap["dates"][1:], cap["r"], rep["window"]


def _excluded(
    book: PriceBook,
    insts: dict[int, Instrument],
    by_inst: dict[int, Decimal],
    start: dict[int, int | None],
    total: Decimal,
    limit: int,
    kept: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Holdings left out, with their first price date; `limit` is the window's first index + 1."""
    rows = []
    for i in sorted(by_inst, key=lambda i: -by_inst[i]):
        if (kept is not None and i in kept) or (kept is None and start[i] is not None and start[i] < limit):  # type: ignore[operator]
            continue
        pts = book.prices.get(i)
        rows.append(
            {
                "instrument_id": i,
                "name": insts[i].name,
                "weight": float(by_inst[i] / total),
                "first_price": pts[0].date if pts else None,
                "reason": "no recent price" if start[i] is None else "history starts after the window",
            }
        )
    return rows


def _correlations(
    db: Session,
    ids: list[int],
    insts: dict[int, Instrument],
    by_inst: dict[int, Decimal],
    cols: Arr,
    w: Arr,
    first_real: list[int],
) -> dict[str, Any]:
    """Holdings matrix (top 12, the rest as one series) and an asset-class sleeve matrix.

    `first_real[k]` is the first row of column k that is not spliced from a proxy. Each pair uses only the rows both
    hold real data for when that is at least MIN_OVERLAP months; otherwise the spliced rows are used and both labels
    are flagged `extended`.
    """
    live = [k for k, i in enumerate(ids) if insts[i].type != "cash"]
    top, rest = live[:TOP_N], live[TOP_N:]
    labels = [insts[ids[k]].name for k in top]
    series = [cols[:, k] for k in top]
    real = [first_real[k] for k in top]
    if rest:
        rw = w[rest] / w[rest].sum()
        labels.append(OTHER)
        series.append(cols[:, rest] @ rw)
        real.append(max(first_real[k] for k in rest))
    holdings = _matrix(labels, series, real)
    eff = profiles.effective_many(db, [insts[ids[k]] for k in live])
    groups: dict[str, list[int]] = defaultdict(list)
    for k in live:
        groups[classify(insts[ids[k]], "asset_class", eff[ids[k]].data)].append(k)
    names = sorted(groups, key=lambda g: -sum(by_inst[ids[k]] for k in groups[g]))
    sleeves = [cols[:, groups[g]] @ (w[groups[g]] / w[groups[g]].sum()) for g in names]
    classes = _matrix(names, sleeves, [max(first_real[k] for k in groups[g]) for g in names])
    return {"holdings": holdings, "classes": classes}


def _matrix(labels: list[str], series: list[Arr], first_real: list[int]) -> dict[str, Any]:
    """Correlation matrix with a per-label `extended` flag for labels whose cells rest on proxy-spliced months."""
    k, t = len(series), len(series[0])
    flags = [False] * k
    matrix: list[list[float | None]] = []
    for i in range(k):
        row: list[float | None] = []
        for j in range(k):
            if i == j:
                row.append(1.0)
                continue
            lo = max(first_real[i], first_real[j])
            if lo > 0 and t - lo < MIN_OVERLAP:  # too few real months together: fall back to the spliced series
                lo = 0
                flags[i] = flags[i] or first_real[i] > 0
                flags[j] = flags[j] or first_real[j] > 0
            row.append(_corr(series[i][lo:], series[j][lo:]))
        matrix.append(row)
    return {"labels": labels, "matrix": matrix if k > 1 else [], "extended": flags}
