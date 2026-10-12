"""Allocation advisor: the mix with the most expected return that stays inside your risk limits, and a glide path.

Candidates are the efficient frontier of the asset classes (the Monte Carlo assumptions, editable). Two limits decide:
- the loss in a bad year: the 1-in-20 one-year loss (VaR 95 %) may not exceed `max_loss`;
- the chance of being behind when the money is needed: the probability that a lump invested now is worth less than
  it cost after the remaining horizon may not exceed `max_shortfall`.
The second limit tightens as the horizon shortens, so repeating the choice year by year gives a glide path that
moves from shares towards bonds and cash. Projections with contributions use the same lognormal model as the
Monte Carlo simulator.
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from app.modules.planning import montecarlo as mc
from app.modules.portfolio import optimize as opt

Arr = np.ndarray[Any, np.dtype[np.float64]]

Z95 = 1.6448536269514722  # one-sided 95 % normal quantile
POINTS = 41
PATHS = 2000
LABEL = {"equity": "Equity", "bond": "Fixed income", "cash": "Cash", "other": "Other"}


class AdvisorError(ValueError):
    pass


@dataclass
class Inputs:
    horizon_years: float = 15.0  # until the money is needed
    max_loss: float = 0.20  # acceptable 1-in-20 one-year loss
    max_shortfall: float = 0.10  # acceptable chance of being below what was put in at the horizon
    start_value: float = 0.0
    monthly: float = 0.0
    mean: dict[str, float] = field(default_factory=lambda: dict(mc.DEFAULT_MEAN))
    vol: dict[str, float] = field(default_factory=lambda: dict(mc.DEFAULT_VOL))
    corr: list[list[float]] = field(default_factory=lambda: [list(r) for r in mc.DEFAULT_CORR])
    bounds: dict[str, tuple[float, float]] = field(default_factory=lambda: {"other": (0.0, 0.0)})
    current: dict[str, float] | None = None  # today's look-through mix, by class
    seed: int = 20261012
    # borrowing against the portfolio: extends the frontier past 100 % at the credit rate after the tax deduction
    leverage: bool = False
    max_leverage: float = 2.0
    borrow_rate: float = 0.0485
    deductible: float = 0.30
    collateral_rate: float = 0.80
    max_margin_call: float = 0.02  # acceptable chance of a margin call within a year


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def log_params(mu: float, sigma: float) -> tuple[float, float]:
    """Yearly (log-mean, log-sd) of a lognormal with arithmetic mean `mu` and volatility `sigma`."""
    s2 = math.log1p(sigma**2 / (1 + mu) ** 2)
    return math.log1p(mu) - s2 / 2, math.sqrt(s2)


def metrics(mu: float, sigma: float, horizon: float) -> dict[str, float]:
    """Bad-year loss (VaR and CVaR at 95 %) and the chance a lump is below its cost after `horizon` years."""
    m, s = log_params(mu, sigma)
    var = math.expm1(m - Z95 * s)
    cvar = math.exp(m + s * s / 2) * _norm_cdf(-Z95 - s) / 0.05 - 1 if s > 0 else var
    if s > 0:
        p_loss = _norm_cdf(-m * math.sqrt(horizon) / s)
    else:
        p_loss = 0.0 if m >= 0 else 1.0
    return {
        "return": mu,
        "volatility": sigma,
        "median_return": math.expm1(m),
        "var95": var,
        "cvar95": cvar,
        "p_loss": p_loss,
    }


def _inputs(a: Inputs) -> tuple[Arr, Arr, Arr, Arr]:
    mu, cov = opt.class_inputs({"mean": a.mean, "vol": a.vol, "corr": a.corr})
    lo, hi = np.zeros(len(mc.CLASSES)), np.ones(len(mc.CLASSES))
    for i, c in enumerate(mc.CLASSES):
        if c in a.bounds:
            lo[i], hi[i] = a.bounds[c]
            if not 0 <= lo[i] <= hi[i] <= 1:
                raise AdvisorError(f"Limits for {c} must satisfy 0 ≤ min ≤ max ≤ 100 %")
    if lo.sum() > 1 + 1e-9 or hi.sum() < 1 - 1e-9:
        raise AdvisorError("The class limits cannot add up to 100 %")
    return mu, cov, lo, hi


def _ok(x: dict[str, float], a: Inputs) -> bool:
    return (
        x["var95"] >= -a.max_loss - 1e-12
        and x["p_loss"] <= a.max_shortfall + 1e-12
        and x.get("p_margin_call", 0.0) <= a.max_margin_call + 1e-12
    )


def _metrics(p: dict[str, Any], horizon: float) -> dict[str, float]:
    x = metrics(p["return"], p["volatility"], horizon)
    if "p_margin_call" in p:
        x["p_margin_call"] = p["p_margin_call"]
    return x


def _pick(points: list[dict[str, Any]], a: Inputs, horizon: float, levered: bool = True) -> tuple[int, bool]:
    """Index of the highest-return point inside the limits at this horizon; (lowest risk, False) if none."""
    best = None
    for i, p in enumerate(points):
        if not levered and p.get("leverage", 1.0) > 1.0:
            continue
        if _ok(_metrics(p, horizon), a) and (best is None or p["return"] > points[best]["return"]):
            best = i
    return (0, False) if best is None else (best, True)


def margin_call_probability(mu: float, sigma: float, ratio: float, collateral_rate: float, years: float = 1.0) -> float:
    """Chance that the assets fall far enough within `years` for the loan to exceed the collateral value
    (first passage of a lognormal price below the barrier; interest left out)."""
    if ratio <= 1.0:
        return 0.0
    m, s = log_params(mu, sigma)
    x = math.log((ratio - 1) / (collateral_rate * ratio))  # the barrier as a log fall from today
    if x >= 0:
        return 1.0
    if s <= 0:
        return 1.0 if m * years <= x else 0.0
    sd = s * math.sqrt(years)
    return min(1.0, _norm_cdf((x - m * years) / sd) + math.exp(2 * m * x / s**2) * _norm_cdf((x + m * years) / sd))


def levered_points(points: list[dict[str, Any]], a: Inputs) -> list[dict[str, Any]]:
    """The cash-free mix with the best return per unit of risk against the after-tax credit rate, levered in 0.1×
    steps."""
    b = a.borrow_rate * (1 - a.deductible)
    # cash in a credit account just pays the loan down, so a levered mix holds none
    cash = mc.CLASSES.index("cash")
    risky = [p for p in points if p["weights"][cash] < 0.005 and p["volatility"] > 0]
    if not risky:
        return []
    tan = max(risky, key=lambda p: (p["return"] - b) / p["volatility"])
    if tan["return"] <= b:
        return []  # borrowing costs more than the portfolio is expected to earn
    out = []
    k = 1.1
    while k <= a.max_leverage + 1e-9:
        if (k - 1) / k >= a.collateral_rate:
            break
        out.append(
            {
                "return": k * tan["return"] - (k - 1) * b,
                "volatility": k * tan["volatility"],
                "sharpe": tan["sharpe"],
                "weights": tan["weights"],
                "leverage": round(k, 2),
                "p_margin_call": margin_call_probability(tan["return"], tan["volatility"], k, a.collateral_rate),
            }
        )
        k += 0.1
    return out


def _mix(w: list[float] | Arr) -> dict[str, float]:
    return {c: max(round(float(w[i]), 4), 0.0) + 0.0 for i, c in enumerate(mc.CLASSES)}  # no -0.0


def project(
    a: Inputs, mix: dict[str, float] | None, glide: list[dict[str, Any]] | None, years: float, ratio: float = 1.0
) -> dict[str, Any]:
    """Value at the horizon with monthly contributions, on the Monte Carlo's lognormal paths (borrowing `ratio`×)."""
    months = max(int(round(years * 12)), 1)
    p = mc.Params(
        start_age=0,
        horizon_age=math.ceil(years) + 1,
        mix=mix or {"equity": 1.0},
        glide=glide,
        paths=PATHS,
        seed=a.seed,
        assumptions=mc.Assumptions(mean=a.mean, vol=a.vol, corr=a.corr),
    )
    ret = mc.assumption_returns(p, months, np.random.default_rng(a.seed))
    calls = None
    if ratio > 1.0:
        lev = mc.Leverage(ratio=ratio, rate=a.borrow_rate, deductible=a.deductible, collateral_rate=a.collateral_rate)
        ret, info = mc.levered_returns(ret, lev)
        calls = info["margin_call"]
    v: Arr = np.full(PATHS, a.start_value, dtype=np.float64)
    peak: Arr = v.copy()
    worst: Arr = np.zeros(PATHS)
    for m in range(months):
        v = np.asarray((v + a.monthly) * (1 + ret[m]), dtype=np.float64)
        peak = np.asarray(
            np.maximum(peak + a.monthly, v), dtype=np.float64
        )  # a contribution raises the peak without being a gain
        fall = np.divide(v, peak, out=np.ones_like(v), where=peak > 0) - 1  # no money yet: no fall
        worst = np.minimum(worst, fall)
    invested = a.start_value + a.monthly * months
    return {
        "years": years,
        "invested": invested,
        "p10": float(np.percentile(v, 10)),
        "p50": float(np.percentile(v, 50)),
        "p90": float(np.percentile(v, 90)),
        "p_below_invested": float((v < invested).mean()),
        "drawdown_p50": float(np.percentile(worst, 50)),
        "drawdown_p10": float(np.percentile(worst, 10)),
        "leverage": ratio,
        "margin_call": calls,
    }


def advise(a: Inputs, today: date) -> dict[str, Any]:
    if not 0.5 <= a.horizon_years <= 60:
        raise AdvisorError("The horizon must be between half a year and 60 years")
    if not 0 < a.max_loss < 1 or not 0 < a.max_shortfall < 0.5:
        raise AdvisorError("The loss limit must be 0–100 % and the shortfall chance 0–50 %")
    mu, cov, lo, hi = _inputs(a)
    points = opt.frontier(mu, cov, lo, hi, 0.0, POINTS)
    if not points:
        raise AdvisorError("No mix satisfies the class limits")
    if a.leverage:
        points = sorted(points + levered_points(points, a), key=lambda p: p["return"])
    rows: list[dict[str, Any]] = []
    for p in points:
        x = _metrics(p, a.horizon_years)
        k = p.get("leverage", 1.0)
        rows.append({**x, "mix": _mix(p["weights"]), "leverage": k, "ok": _ok(x, a)})
    best, found = _pick(points, a, a.horizon_years)

    # glide path: the same choice each year with the remaining horizon; never more risk than the year before
    glide: list[dict[str, Any]] = []
    prev = best
    years = int(math.ceil(a.horizon_years))
    for y in range(years + 1):
        left = max(a.horizon_years - y, 1.0)  # the last year's choice holds from a year out onwards
        i, _ = _pick(points, a, left, levered=False)  # the glide path itself never borrows
        i = min(i, prev)
        prev = i
        pt = points[i]
        glide.append(
            {
                "date": date(today.year + y, today.month, 1).isoformat(),
                "years_left": round(a.horizon_years - y, 2),
                "mix": _mix(pt["weights"]),
                "return": pt["return"],
                "volatility": pt["volatility"],
            }
        )
    # drop the points where nothing changes, keeping the first and last
    kept = [g for k, g in enumerate(glide) if k in (0, len(glide) - 1) or g["mix"] != glide[k - 1]["mix"]]

    current: dict[str, Any] | None = None
    if a.current and sum(a.current.values()) > 0:
        w = np.array([max(a.current.get(c, 0.0), 0.0) for c in mc.CLASSES])
        w = w / w.sum()
        r, v = float(w @ mu), float(np.sqrt(w @ cov @ w))
        current = {**metrics(r, v, a.horizon_years), "mix": _mix(w), "ok": _ok(metrics(r, v, a.horizon_years), a)}

    rec_mix = rows[best]["mix"]
    rec_k = rows[best]["leverage"]
    glide_ages = [{"age": float(y), "mix": g["mix"]} for y, g in enumerate(glide)]
    projection = {
        "recommended": project(a, rec_mix, None, a.horizon_years, rec_k),
        "glide": project(a, None, glide_ages, a.horizon_years),
        "current": project(a, current["mix"], None, a.horizon_years) if current else None,
    }
    notes = [
        "Expected returns, volatilities and correlations are the assumptions below, not forecasts; the limits are "
        "yours. Change them and the advice moves.",
        f"Chosen: the mix with the highest expected return whose 1-in-20 bad year loses at most {a.max_loss:.0%} and "
        f"whose chance of being below cost after {a.horizon_years:g} years is at most {a.max_shortfall:.0%}.",
        "The glide path repeats that choice each year with the time that is left, so it takes risk off as the date "
        "nears and never adds it back; from one year out it holds.",
        "Real markets have fatter tails than this model: world shares fell about 40 % in 2008. The simulated "
        "drawdowns (the deepest fall along the way) show how far a portfolio can drop before it recovers.",
    ]
    if a.leverage:
        notes.append(
            f"Borrowing is priced at {a.borrow_rate:.2%} less the {a.deductible:.0%} tax deduction, against the mix "
            "with the best return per unit of risk. A margin call sells at the worst time and realises taxable gains. "
            "The glide path itself never borrows."
        )
    if not found:
        notes.insert(
            0, "No mix meets your limits; this is the lowest-risk mix. Loosen a limit or lengthen the horizon."
        )
    return {
        "as_of": today.isoformat(),
        "classes": list(mc.CLASSES),
        "labels": LABEL,
        "inputs": {
            "horizon_years": a.horizon_years,
            "max_loss": a.max_loss,
            "max_shortfall": a.max_shortfall,
            "start_value": a.start_value,
            "monthly": a.monthly,
            "mean": a.mean,
            "vol": a.vol,
        },
        "points": rows,
        "recommended": best,
        "found": found,
        "recommended_targets": {LABEL[c]: x for c, x in rec_mix.items() if x > 0},
        "current": current,
        "glide": kept,
        "glide_targets": [
            {"date": g["date"], "mix": {LABEL[c]: x for c, x in g["mix"].items() if x > 0}} for g in kept
        ],
        "projection": projection,
        "notes": notes,
    }
