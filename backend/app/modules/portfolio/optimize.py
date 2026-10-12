"""Portfolio optimisation and the efficient frontier (mean-variance, long-only, weights sum to 1).

Two universes: the four look-through asset classes with assumed returns (the Monte Carlo defaults, editable), and
the current holdings with returns and covariance estimated from their own monthly total returns. Historical means are
mostly noise, so they are shrunk halfway to their grand mean, and the covariance is shrunk towards a constant
correlation (Ledoit-Wolf 2003). Nothing here places trades; it only shows what the model would move.

All maths is float64 numpy/scipy (annual units); money stays Decimal.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linprog, minimize
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.planning import montecarlo as mc
from app.modules.portfolio.allocation import xray
from app.modules.portfolio.risk import _returns, history_start, monthly_values, risk_report, total_return_values
from app.modules.portfolio.valuation import Scope
from app.modules.pricing import distributions as dist
from app.modules.pricing.service import PriceBook

Arr = NDArray[np.float64]
OBJECTIVES = ("max_sharpe", "min_variance", "target_return", "target_vol", "risk_parity")
DEFAULT_RF = 0.02  # holdings: a euro short rate
UNIVERSES = ("classes", "holdings")
PERIODS = ("3Y", "5Y", "10Y", "MAX")
FRONTIER_POINTS = 25
TOP_HOLDINGS = 20
MAX_WEIGHT = {"classes": 1.0, "holdings": 0.4}
MEAN_SHRINK = 0.5  # share of a holding's own historical mean replaced by the grand mean
MIN_MONTHS = 24
TOL = 1e-7
CENT = Decimal("0.01")
CLASS_LABEL = {"equity": "Equity", "bond": "Fixed income", "cash": "Cash", "other": "Other"}
CLASS_OF = {"Equity": "equity", "Fixed income": "bond", "Cash": "cash"}  # everything else (crypto, unknown) is other

NOTES = [
    "A mean-variance model is only as good as its inputs: small changes in expected returns move the answer a lot. "
    "Read the result as a direction, not as exact targets.",
    "Nothing is traded. For a class-level change, the rebalancing suggestion handles new money and taxes.",
]


class OptimizeError(ValueError):
    """Bad input, an infeasible target or an optimiser that did not converge: the message is shown to the user."""


# ---- maths -----------------------------------------------------------------------------------------------------


def stats(w: Arr, mu: Arr, cov: Arr, rf: float) -> tuple[float, float, float | None]:
    """(expected return, volatility, Sharpe) of weights w; Sharpe is None for a riskless portfolio."""
    ret, vol = float(w @ mu), float(np.sqrt(max(float(w @ cov @ w), 0.0)))
    return ret, vol, (ret - rf) / vol if vol > 1e-9 else None


def ledoit_wolf_cc(x: Arr) -> tuple[Arr, float]:
    """(covariance of the columns of `x` per period, shrinkage intensity), Ledoit & Wolf's constant-correlation target.

    The target keeps each asset's own variance and gives every pair the average sample correlation; the intensity is
    their analytic estimate (2003), clipped to [0, 1]. Columns need some variance.
    """
    t, n = x.shape
    x = x - x.mean(axis=0)
    s = x.T @ x / t
    var = np.diag(s).copy()
    sd = np.sqrt(var)
    if n < 2 or t < 3 or np.any(sd < 1e-12):
        return s * t / max(t - 1, 1), 0.0
    corr = s / np.outer(sd, sd)
    rbar = (corr.sum() - n) / (n * (n - 1))
    prior = rbar * np.outer(sd, sd)
    np.fill_diagonal(prior, var)
    y = x**2
    phi_mat = y.T @ y / t - s**2  # pi-hat of each entry of the sample covariance
    phi = phi_mat.sum()
    theta = (x**3).T @ x / t - var[:, None] * s  # Cov(x_i^2, x_i x_j)
    np.fill_diagonal(theta, 0.0)
    rho = np.trace(phi_mat) + rbar * float(((sd[None, :] / sd[:, None]) * theta).sum())
    gamma = float(((s - prior) ** 2).sum())
    k = 1.0 if gamma < 1e-18 else (phi - rho) / gamma
    delta = float(min(1.0, max(0.0, k / t)))
    return (delta * prior + (1 - delta) * s) * t / (t - 1), delta  # back to the unbiased (ddof=1) scale


def _slsqp(fun: Any, x0: Arr, bounds: list[tuple[float, float]], cons: list[dict[str, Any]], jac: Any = None) -> Any:
    """SLSQP, retried with looser tolerances: a very tight ftol can end in a spurious "line search" failure."""
    res = None
    for ftol in (1e-15, 1e-13, 1e-11, 1e-9):
        res = minimize(
            fun, x0, jac=jac, bounds=bounds, constraints=cons, method="SLSQP", options={"ftol": ftol, "maxiter": 500}
        )
        if res.success:
            break
    return res


def _start(lo: Arr, hi: Arr) -> Arr:
    """A feasible point: the lower bounds plus an equal share of the remaining room."""
    room = hi - lo
    if lo.sum() > 1 + 1e-9 or hi.sum() < 1 - 1e-9:
        raise OptimizeError(
            f"The weight limits cannot add up to 100 % (lowest total {lo.sum():.0%}, highest {hi.sum():.0%})"
        )
    t = (1 - lo.sum()) / room.sum() if room.sum() > 0 else 0.0
    return np.asarray(lo + t * room, dtype=np.float64)


def _max_return(mu: Arr, lo: Arr, hi: Arr) -> Arr:
    """The highest-return portfolio within the limits (a linear programme)."""
    res = linprog(-mu, A_eq=np.ones((1, len(mu))), b_eq=[1.0], bounds=list(zip(lo, hi, strict=True)), method="highs")
    if res.status != 0:
        raise OptimizeError("No portfolio satisfies the weight limits")
    return np.asarray(res.x, dtype=np.float64)


def _finish(res: Any, lo: Arr, hi: Arr, what: str) -> Arr:
    """The solution if the solver converged and respects the constraints, else a clear error."""
    w = np.clip(np.asarray(res.x, dtype=np.float64), lo, hi)
    if not res.success or abs(w.sum() - 1) > 1e-6 or not np.all(np.isfinite(w)):
        raise OptimizeError(f"The optimiser did not converge for {what}: {res.message}")
    return w / w.sum() if abs(w.sum() - 1) > 1e-12 else w


def _min_var(cov: Arr, lo: Arr, hi: Arr, x0: Arr | None = None, ret: tuple[Arr, float, bool] | None = None) -> Arr:
    """Minimum variance, optionally with return = target (equality) or >= target."""
    scale = 1.0 / max(float(np.trace(cov)) / len(cov), 1e-12)  # keeps SLSQP's tolerances meaningful for any units
    cons: list[dict[str, Any]] = [{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones_like(w)}]
    if ret is not None:
        mu, target, equal = ret
        cons.append({"type": "eq" if equal else "ineq", "fun": lambda w: mu @ w - target, "jac": lambda w: mu})
    res = _slsqp(
        lambda w: scale * float(w @ cov @ w),
        _start(lo, hi) if x0 is None else x0,
        list(zip(lo, hi, strict=True)),
        cons,
        jac=lambda w: 2 * scale * (cov @ w),
    )
    return _finish(res, lo, hi, "minimum variance")


def _max_sharpe(mu: Arr, cov: Arr, lo: Arr, hi: Arr, rf: float) -> Arr:
    top = _max_return(mu, lo, hi)
    if float(top @ mu) <= rf + 1e-12:
        raise OptimizeError("No portfolio within the limits is expected to beat the risk-free rate, so Sharpe is moot")

    def neg(w: Arr) -> float:
        return float(-(float(mu @ w) - rf) / np.sqrt(max(float(w @ cov @ w), 1e-14)))

    def grad(w: Arr) -> Arr:
        v = max(float(w @ cov @ w), 1e-14)
        return np.asarray(-(mu / np.sqrt(v) - (float(mu @ w) - rf) * (cov @ w) / v**1.5), dtype=np.float64)

    best: Arr | None = None
    starts = [_start(lo, hi), _min_var(cov, lo, hi), top]
    for x0 in starts:
        res = _slsqp(
            neg,
            x0,
            list(zip(lo, hi, strict=True)),
            [{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones_like(w)}],
            jac=grad,
        )
        if res.success and abs(res.x.sum() - 1) < 1e-6:
            w = np.clip(res.x, lo, hi)
            if best is None or neg(w) < neg(best) - 1e-12:
                best = w / w.sum()
    if best is None:
        raise OptimizeError("The optimiser did not converge for maximum Sharpe")
    return best


def _max_return_at_vol(mu: Arr, cov: Arr, lo: Arr, hi: Arr, vol: float) -> Arr:
    floor = _min_var(cov, lo, hi)
    lowest = float(np.sqrt(floor @ cov @ floor))
    if vol < lowest - 1e-9:
        raise OptimizeError(
            f"Target volatility {vol:.1%} is below the lowest reachable with these limits ({lowest:.1%})"
        )
    cons: list[dict[str, Any]] = [
        {"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones_like(w)},
        {"type": "ineq", "fun": lambda w: vol**2 - w @ cov @ w, "jac": lambda w: -2 * (cov @ w)},
    ]
    best: Arr | None = None
    for x0 in (floor, _start(lo, hi)):
        res = _slsqp(lambda w: -float(mu @ w), x0, list(zip(lo, hi, strict=True)), cons, jac=lambda w: -mu)
        if res.success and abs(res.x.sum() - 1) < 1e-6 and res.x @ cov @ res.x <= vol**2 * (1 + 1e-5):
            w = np.clip(res.x, lo, hi)
            if best is None or mu @ w > mu @ best:
                best = w / w.sum()
    if best is None:
        raise OptimizeError("The optimiser did not converge for the target volatility")
    return best


def risk_contributions(w: Arr, cov: Arr) -> Arr:
    """Each asset's share of the portfolio variance (sums to 1)."""
    var = float(w @ cov @ w)
    return np.asarray(w * (cov @ w) / var, dtype=np.float64) if var > 1e-18 else np.zeros_like(w)


def _risk_parity(cov: Arr, lo: Arr, hi: Arr) -> Arr:
    """Equal risk contributions among the assets that are free to move (fixed ones keep their weight).

    Without binding limits this is the point where every free asset has the same share of the variance; with limits
    that bind it is the closest feasible portfolio, and the caller says so.
    """
    free = hi - lo > 1e-12
    k = int(free.sum())
    if k < 2:
        raise OptimizeError("Risk parity needs at least two assets that can move")
    scale = 1.0 / max(float(np.trace(cov)) / len(cov), 1e-12)
    c = scale * cov
    x0 = _start(lo, hi)
    inv = np.where(free, 1 / np.sqrt(np.maximum(np.diag(c), 1e-12)), 0.0)
    x0 = np.clip(lo + (1 - lo.sum()) * inv / inv.sum(), lo, hi) if (1 - lo.sum()) > 0 else x0
    if abs(x0.sum() - 1) > 1e-9:
        x0 = _start(lo, hi)

    def dev(w: Arr) -> float:
        var = float(w @ c @ w)
        rc = w * (c @ w) / max(var, 1e-14)
        return float(np.sum((rc[free] - 1 / k) ** 2))

    res = _slsqp(
        dev,
        x0,
        list(zip(lo, hi, strict=True)),
        [{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones_like(w)}],
    )
    return _finish(res, lo, hi, "risk parity")


def risk_parity_exact(w: Arr, cov: Arr, lo: Arr, hi: Arr) -> bool:
    """Whether the free assets' risk shares are equal (within a tenth of a percentage point)."""
    free = hi - lo > 1e-12
    rc = risk_contributions(w, cov)[free]
    return bool(np.all(np.abs(rc - 1 / free.sum()) < 1e-3))


def solve(objective: str, mu: Arr, cov: Arr, lo: Arr, hi: Arr, rf: float = 0.0, target: float | None = None) -> Arr:
    """Weights for one objective within [lo, hi] summing to 1; OptimizeError when infeasible or not converged."""
    if objective not in OBJECTIVES:
        raise OptimizeError(f"Unknown objective {objective!r}")
    if objective in ("target_return", "target_vol") and target is None:
        raise OptimizeError("This objective needs a target")
    _start(lo, hi)  # feasibility of the limits themselves
    if objective == "min_variance":
        return _min_var(cov, lo, hi)
    if objective == "max_sharpe":
        return _max_sharpe(mu, cov, lo, hi, rf)
    if objective == "risk_parity":
        return _risk_parity(cov, lo, hi)
    assert target is not None
    if objective == "target_vol":
        return _max_return_at_vol(mu, cov, lo, hi, target)
    top = float(_max_return(mu, lo, hi) @ mu)
    if target > top + 1e-9:
        raise OptimizeError(f"Target return {target:.2%} is above the highest reachable with these limits ({top:.2%})")
    return _min_var(cov, lo, hi, ret=(mu, target, False))


def frontier(mu: Arr, cov: Arr, lo: Arr, hi: Arr, rf: float, points: int = FRONTIER_POINTS) -> list[dict[str, Any]]:
    """`points` portfolios from minimum variance to the highest reachable return, each the least risky at its return."""
    first = _min_var(cov, lo, hi)
    top = _max_return(mu, lo, hi)
    r0, r1 = float(first @ mu), float(top @ mu)
    out: list[tuple[Arr, bool]] = [(first, True)]
    if r1 - r0 > 1e-9:
        prev = first
        for k in range(1, points - 1):
            try:
                prev = _min_var(cov, lo, hi, x0=prev, ret=(mu, r0 + (r1 - r0) * k / (points - 1), True))
            except OptimizeError:
                continue  # a point that does not converge is left out rather than faked
            out.append((prev, True))
        out.append((top, True))
    rows = []
    for w, _ in out:
        r, v, s = stats(w, mu, cov, rf)
        rows.append({"return": r, "volatility": v, "sharpe": s, "weights": [float(x) for x in w]})
    return rows


# ---- inputs ----------------------------------------------------------------------------------------------------


def class_inputs(assumptions: dict[str, Any] | None) -> tuple[Arr, Arr]:
    """(annual mean vector, covariance matrix) in the order of the Monte Carlo classes, from the request or defaults."""
    a = assumptions or {}
    mean, vol = {**mc.DEFAULT_MEAN, **(a.get("mean") or {})}, {**mc.DEFAULT_VOL, **(a.get("vol") or {})}
    corr = a.get("corr") or mc.DEFAULT_CORR
    try:
        mu = np.array([float(mean[c]) for c in mc.CLASSES])
        sd = np.array([float(vol[c]) for c in mc.CLASSES])
        mc._factor(corr, sd)  # validates shape, symmetry, unit diagonal and positive semi-definiteness
    except (TypeError, ValueError, KeyError) as exc:
        raise OptimizeError(str(exc)) from exc
    if np.any(sd < 0):
        raise OptimizeError("Volatilities cannot be negative")
    return mu, np.asarray(np.outer(sd, sd) * np.array(corr, dtype=np.float64), dtype=np.float64)


def _limits(
    keys: list[str],
    fixed: Arr,
    cap: float,
    bounds: dict[str, Any] | None,
    current: Arr | None = None,
) -> tuple[Arr, Arr]:
    """Per-asset (min, max): [0, cap] for free assets, the current weight for fixed ones, then per-asset overrides."""
    lo, hi = np.zeros(len(keys)), np.full(len(keys), cap)
    for i, k in enumerate(keys):
        if fixed[i] and current is not None:
            lo[i] = hi[i] = current[i]
        elif bounds and k in bounds:
            b = bounds[k]
            try:
                lo[i] = float(b[0]) if b[0] is not None else 0.0
                hi[i] = float(b[1]) if b[1] is not None else cap
            except (TypeError, ValueError, IndexError) as exc:
                raise OptimizeError(f"Limits for {k} must be [min, max]") from exc
            if not 0 <= lo[i] <= hi[i] <= 1:
                raise OptimizeError(f"Limits for {k} must satisfy 0 ≤ min ≤ max ≤ 1")
    return lo, hi


def _candidate_columns(
    db: Session, held: set[int], months: list[date], start: int
) -> tuple[list[tuple[Instrument, Arr]], list[str]]:
    """Monthly total returns over the window of candidate funds not yet owned (marked in the ETF screener); those
    without prices for the whole window are named, not guessed."""
    cands = [
        i
        for i in db.scalars(select(Instrument).where(Instrument.type.in_(("etf", "fund"))).order_by(Instrument.name))
        if (i.meta or {}).get("candidate") and i.id not in held
    ]
    if not cands:
        return [], []
    book = PriceBook(db, {c.id for c in cands})
    dists = dist.distribution_series(db, [c.id for c in cands])
    cols, short = [], []
    for c in cands:
        vals = total_return_values(book, c.id, months, monthly_values(book, c.id, months), dists.get(c.id))
        first = history_start(vals)
        if first is None or first > start:
            short.append(c.name)
            continue
        cols.append((c, _returns(vals, start)))
    return cols, short


def _holding_inputs(
    db: Session, scope: Scope, period: str, rf: float, on: date, candidates: bool = False
) -> tuple[list[dict[str, Any]], Arr, Arr, dict[str, Any], list[str]]:
    """(asset rows, mean vector, covariance, window, notes) from the portfolio's own monthly total returns."""
    cap: dict[str, Any] = {}
    rep = risk_report(db, scope, period, rf, on, proxies=True, capture=cap)
    if "parts" not in cap or rep["window"] is None:
        raise OptimizeError("Not enough price history to estimate returns and risk for the holdings")
    p = cap["parts"]
    ids: list[int] = p["ids"]
    cols: Arr = p["cols"]
    insts, values, covered = p["insts"], p["values"], p["covered"]
    t = cols.shape[0]
    if t < MIN_MONTHS:
        raise OptimizeError(f"Only {t} months of history are shared by the holdings; at least {MIN_MONTHS} are needed")
    live = [k for k, i in enumerate(ids) if insts[i].type != "cash"]
    cash = [k for k, i in enumerate(ids) if insts[i].type == "cash"]
    top, rest = live[:TOP_HOLDINGS], live[TOP_HOLDINGS:]
    rows: list[dict[str, Any]] = []
    series: list[Arr] = []
    for k in top:
        rows.append({"key": str(ids[k]), "name": insts[ids[k]].name, "value": values[ids[k]], "fixed": False})
        series.append(cols[:, k])
    if rest:
        rv = np.array([float(values[ids[k]]) for k in rest])
        rows.append(
            {
                "key": "rest",
                "name": f"Rest ({len(rest)} smaller holdings)",
                "value": sum((values[ids[k]] for k in rest), Decimal(0)),
                "fixed": True,
            }
        )
        series.append(cols[:, rest] @ (rv / rv.sum()))
    short: list[str] = []
    if candidates:
        extra, short = _candidate_columns(db, set(ids), p["months"], p["start"])
        for c, col in extra:
            rows.append({"key": str(c.id), "name": f"{c.name} (candidate)", "value": Decimal(0), "fixed": False})
            series.append(col)
    x = np.column_stack(series)
    cov_m, delta = ledoit_wolf_cc(x)
    cov: Arr
    raw = x.mean(axis=0) * 12
    mu = (1 - MEAN_SHRINK) * raw + MEAN_SHRINK * raw.mean()
    if cash:
        rows.append(
            {
                "key": "cash",
                "name": "Cash",
                "value": sum((values[ids[k]] for k in cash), Decimal(0)),
                "fixed": True,
            }
        )
        n = len(rows)
        full = np.zeros((n, n))
        full[: n - 1, : n - 1] = cov_m * 12
        cov = full
        mu = np.append(mu, rf)
    else:
        cov = cov_m * 12
    notes = [
        f"Returns and risk come from {t} months of the holdings' own total returns (dividends reinvested), "
        f"{rep['window']['start']:%m/%Y}–{rep['window']['end']:%m/%Y}; missing early history is borrowed from "
        "close look-alikes.",
        f"Raw historical means are mostly luck, so each is shrunk {MEAN_SHRINK:.0%} of the way to the average of the "
        "holdings; differences in expected return are therefore muted on purpose.",
        f"The covariance is shrunk towards a constant correlation (Ledoit-Wolf, intensity {delta:.0%}) to tame "
        "estimation noise.",
    ]
    if cash:
        notes.append(f"Cash is held at today's weight and assumed to earn the risk-free rate ({rf:.1%}).")
    if rest:
        notes.append(f"The {len(rest)} smallest holdings are kept as one block at today's weight.")
    if candidates:
        notes.append("Candidate funds from the ETF screener are included at 0 % today, so the optimum may add them.")
    if short:
        notes.append(f"Candidates left out for lack of price history over the window: {', '.join(short)}.")
    if rep["excluded"]:
        names = ", ".join(e["name"] for e in rep["excluded"])
        notes.append(f"Left out for lack of history and not part of these figures: {names}.")
    window = {**rep["window"], "covered_value": covered, "delta": delta}
    return rows, np.asarray(mu, dtype=np.float64), np.asarray(cov, dtype=np.float64), window, notes


def _class_rows(db: Session, scope: Scope, on: date) -> list[dict[str, Any]]:
    """The four classes with today's look-through value (crypto and unknown count as other)."""
    xr = xray(db, scope, on)
    val = dict.fromkeys(mc.CLASSES, Decimal(0))
    for b in xr["dimensions"]["asset_class"]:
        if b["key"] == "Not applicable":
            continue
        val[CLASS_OF.get(b["key"], "other")] += b["value"]
    return [{"key": c, "name": CLASS_LABEL[c], "value": val[c], "fixed": False} for c in mc.CLASSES]


# ---- the report ------------------------------------------------------------------------------------------------


def optimize(
    db: Session,
    scope: Scope,
    on: date,
    universe: str = "classes",
    objective: str = "max_sharpe",
    target: float | None = None,
    period: str = "5Y",
    rf: float | None = None,
    max_weight: float | None = None,
    assumptions: dict[str, Any] | None = None,
    bounds: dict[str, Any] | None = None,
    candidates: bool = False,
) -> dict[str, Any]:
    """The optimum for one objective, the frontier, and the moves from today's mix; OptimizeError on bad input."""
    if universe not in UNIVERSES:
        raise OptimizeError(f"universe must be one of {', '.join(UNIVERSES)}")
    if objective not in OBJECTIVES:
        raise OptimizeError(f"objective must be one of {', '.join(OBJECTIVES)}")
    if period.upper() not in PERIODS:
        raise OptimizeError(f"period must be one of {', '.join(PERIODS)}")
    if rf is not None and not -0.1 <= rf <= 0.25:
        raise OptimizeError("The risk-free rate must be between -10 % and 25 %")
    cap = MAX_WEIGHT[universe] if max_weight is None else max_weight
    if not 0 < cap <= 1:
        raise OptimizeError("The maximum weight must be above 0 and at most 100 %")
    if objective in ("target_return", "target_vol") and target is None:
        raise OptimizeError("This objective needs a target")
    notes = list(NOTES)
    out: dict[str, Any] = {
        "universe": universe,
        "objective": objective,
        "target": target,
        "period": period.upper(),
        "rf": None,
        "max_weight": cap,
        "as_of": on,
        "window": None,
        "assumptions": None,
    }
    if universe == "classes":
        rows = _class_rows(db, scope, on)
        mu, cov = class_inputs(assumptions)
        a = assumptions or {}
        out["assumptions"] = {
            "classes": list(mc.CLASSES),
            "mean": dict(zip(mc.CLASSES, map(float, mu), strict=True)),
            "vol": dict(zip(mc.CLASSES, map(float, np.sqrt(np.diag(cov))), strict=True)),
            "corr": a.get("corr") or [list(r) for r in mc.DEFAULT_CORR],
        }
        notes.insert(0, "Expected returns, volatilities and correlations are assumptions you can edit, not forecasts.")
        if rf is None:
            # cash is the risk-free asset here: measured against a lower rate it would look like the best "risky"
            # asset and the max-Sharpe answer would be nearly all cash
            rf = float(mu[mc.CLASSES.index("cash")])
            notes.insert(
                1, f"Sharpe ratios are measured against cash's expected return ({rf:.1%}), the risk-free asset."
            )
    else:
        rf = DEFAULT_RF if rf is None else rf
        rows, mu, cov, out["window"], more = _holding_inputs(db, scope, out["period"], rf, on, candidates)
        notes = more + notes
    out["rf"] = rf
    total = sum((r["value"] for r in rows), Decimal(0))
    if total <= 0:
        raise OptimizeError("There are no investment holdings to optimise")
    w0 = np.array([float(r["value"] / total) for r in rows])
    keys = [r["key"] for r in rows]
    fixed = np.array([r["fixed"] for r in rows])
    lo, hi = _limits(keys, fixed, cap, bounds, w0)
    if universe == "holdings" and cap * (~fixed).sum() < 1 - w0[fixed].sum() - 1e-9:
        raise OptimizeError(
            f"A {cap:.0%} limit over {int((~fixed).sum())} holdings cannot reach 100 %: raise the maximum weight"
        )
    w = solve(objective, mu, cov, lo, hi, rf, target)
    if objective == "risk_parity" and not risk_parity_exact(w, cov, lo, hi):
        notes.append(
            "The weight limits stop the risk shares from being exactly equal; this is the closest feasible mix."
        )
    front = frontier(mu, cov, lo, hi, rf)
    if len(front) < 3:
        notes.append("The frontier is short: the limits leave little room to move.")
    cur, opt = stats(w0, mu, cov, rf), stats(w, mu, cov, rf)
    vols = np.sqrt(np.maximum(np.diag(cov), 0.0))
    assets = []
    for i, r in enumerate(rows):
        move = (Decimal(str(w[i])) - Decimal(str(w0[i]))) * total
        assets.append(
            {
                "key": r["key"],
                "name": r["name"],
                "fixed": r["fixed"],
                "value": r["value"].quantize(CENT),
                "current_weight": float(w0[i]),
                "suggested_weight": float(w[i]),
                "suggested_value": (Decimal(str(w[i])) * total).quantize(CENT),
                "move": move.quantize(CENT),
                "expected_return": float(mu[i]),
                "volatility": float(vols[i]),
                "min_weight": float(lo[i]),
                "max_weight": float(hi[i]),
                "risk_share": float(risk_contributions(w, cov)[i]),
            }
        )
    out.update(
        total=total.quantize(CENT),
        assets=assets,
        current={"return": cur[0], "volatility": cur[1], "sharpe": cur[2]},
        optimum={"return": opt[0], "volatility": opt[1], "sharpe": opt[2]},
        turnover=float(np.abs(w - w0).sum() / 2),
        frontier=front,
        notes=notes,
    )
    return out
