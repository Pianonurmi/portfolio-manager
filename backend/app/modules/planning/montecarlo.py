"""Monte Carlo of contributions and withdrawals on the user's own portfolio (monthly steps, float64 numpy).

Many paths are simulated once, then every withdrawal strategy is run on those same paths so the strategies compare
fairly. Deterministic for (params, seed). The yearly engine in engine.py is a separate, simpler tool and is untouched.

Conventions, so the numbers can be checked by hand:
- a month's flows (lump sum, contribution, withdrawal) happen at its start, then the month's return applies;
- a "year" for the January rules is 12 months from the start of the simulation (birthdays, not the calendar);
- amounts typed in "today's euros" are indexed with inflation; results come out nominal and real (today's euros);
- a withdrawal is the net spending the plan asks of the portfolio: the pension covers part of it, the rest is drawn
  grossed up for the tax drag. A path fails the month it cannot pay the full draw.
"""

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]
CLASSES = ("equity", "bond", "cash", "other")
STRATEGIES = ("fixed_real", "four_percent", "percent_of_portfolio", "guardrails", "vpw")
DEFAULT_RATE = {"four_percent": 0.04, "percent_of_portfolio": 0.04, "guardrails": 0.05}
INDEXED = ("fixed_real", "four_percent", "vpw")  # amount is a real figure indexed month by month
MIN_HISTORY = 36  # months of the portfolio's own history below which a bootstrap says nothing
BLOCK = 12
TOL_REL, TOL_ABS = 1e-9, 1e-6  # a draw this close to the value still counts as paid in full
PCTS = (10, 25, 50, 75, 90)
DEFAULT_MEAN = {"equity": 0.07, "bond": 0.035, "cash": 0.025, "other": 0.05}
DEFAULT_VOL = {"equity": 0.16, "bond": 0.06, "cash": 0.01, "other": 0.20}
DEFAULT_CORR: list[list[float]] = [
    [1.0, 0.2, 0.0, 0.5],
    [0.2, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.5, 0.0, 0.0, 1.0],
]


@dataclass
class Assumptions:
    mean: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MEAN))  # nominal, arithmetic, yearly
    vol: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_VOL))
    corr: list[list[float]] = field(default_factory=lambda: [list(r) for r in DEFAULT_CORR])  # order of CLASSES


@dataclass
class Contributions:
    monthly: float = 0.0
    yearly_increase: float | None = None  # None: as inflation
    until_age: float | None = None  # None: until withdrawals start
    lumps: list[dict[str, float]] = field(default_factory=list)  # {"age", "amount"}, today's euros, + in / - out


@dataclass
class Withdrawal:
    start_age: float = 65
    strategy: str = "four_percent"
    monthly: float = 2000.0  # fixed_real: today's euros per month
    rate: float | None = None  # initial yearly rate; None: the strategy's default
    floor: float = 0.0  # minimum monthly amount, today's euros
    pension_monthly: float = 0.0  # today's euros, after tax
    pension_age: float = 65
    tax: float = 0.0  # share of every draw lost to tax


@dataclass
class Leverage:
    """Borrowing against the portfolio (Nordnet securities credit by default).

    Modelled per euro of your own money: contributions and withdrawals keep the leverage of the moment. Interest is
    charged monthly at the Superluotto tier the loan-to-collateral ratio earns (or a fixed rate), less the
    capital-income deduction. A margin call (loan above the collateral value, checked at month end) sells down to
    `restore` of the collateral value; every sale realises the gain share and pays tax on it.
    """

    ratio: float = 1.0  # exposure / own money at the start (1 = no loan)
    policy: str = "yearly"  # monthly | yearly: borrow or repay back to `ratio`; never: borrow once, then let it drift
    rate: float | None = None  # fixed yearly rate; None: the tier rates below by loan-to-collateral
    tier_rates: dict[str, float] = field(default_factory=lambda: {"kulta": 0.0444, "hopea": 0.0465, "base": 0.0485})
    tier_limits: dict[str, float] = field(default_factory=lambda: {"kulta": 0.20, "hopea": 0.60})
    diversified: bool = True  # the Superluotto concentration limits are met (else always the base rate)
    collateral_rate: float = 0.80  # the portfolio's collateral value per euro of market value
    restore: float = 0.80  # after a margin call the loan is brought to this share of the collateral value
    deductible: float = 0.30  # interest relief (capital-income deduction or deficit credit)
    gain_tax: float = 0.30  # tax on gains realised by forced or rebalancing sales
    gain_share: float = 0.0  # unrealised gain share of today's portfolio (0.3: cost basis is 70 % of value)
    until_age: float | None = None  # repay by selling at this age; None: when withdrawals start
    margin_calls: bool = True  # False: a loan against something else (a mortgage) that never forces a sale


@dataclass
class Hedge:
    """A rolling hedge on the portfolio itself: protective puts, zero-cost collars or a short index position.

    Each period of `tenor` months the cost (option premium at Black–Scholes with the mix's volatility plus `skew`, or
    the short's financing) is paid and the payoff settled at the end; the hedge's net result is taxed (losses
    credited) at `tax`. The index is taken to be the portfolio itself, so basis risk is left out.
    """

    kind: str = "put"  # put | collar | short
    ratio: float = 1.0
    tenor: int = 12
    strike: float = 0.90
    skew: float = 0.03
    fee: float = 0.002
    short_cost: float = 0.015
    dividend_yield: float = 0.015
    tax: float = 0.30


@dataclass
class Params:
    start_value: float = 0.0
    start_age: int = 40
    horizon_age: int = 95
    mix: dict[str, float] = field(default_factory=lambda: {"equity": 1.0, "bond": 0.0, "cash": 0.0, "other": 0.0})
    # glide path: [{"age", "mix"}]; the mix is interpolated by age between points and held flat outside them
    glide: list[dict[str, Any]] | None = None
    fees: float = 0.0
    inflation: float = 0.02
    return_model: str = "assumptions"
    assumptions: Assumptions = field(default_factory=Assumptions)
    contributions: Contributions = field(default_factory=Contributions)
    withdrawal: Withdrawal = field(default_factory=Withdrawal)
    target: dict[str, float] | None = None  # {"age", "value_real"}
    seed: int = 20261011
    paths: int = 2000
    success_target: float = 0.90
    leverage: Leverage | None = None
    leverage_sweep: list[float] | None = None  # also run these ratios on the same paths
    hedge: Hedge | None = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Params":
        def sub(cls: Any, key: str) -> Any:
            raw = d.get(key) or {}
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

        top = {k: v for k, v in d.items() if k in Params.__dataclass_fields__}
        for k in ("assumptions", "contributions", "withdrawal", "leverage", "hedge"):
            top.pop(k, None)
        p = Params(**top)
        p.assumptions, p.contributions, p.withdrawal = (
            sub(Assumptions, "assumptions"),
            sub(Contributions, "contributions"),
            sub(Withdrawal, "withdrawal"),
        )
        p.mix = {c: float((d.get("mix") or p.mix).get(c, 0.0)) for c in CLASSES}
        if d.get("leverage"):
            p.leverage = sub(Leverage, "leverage")
        if d.get("hedge"):
            p.hedge = sub(Hedge, "hedge")
        if p.glide:
            p.glide = [
                {"age": float(g["age"]), "mix": {c: float((g.get("mix") or {}).get(c, 0.0)) for c in CLASSES}}
                for g in p.glide
            ]
        return p


@dataclass
class History:
    """The portfolio's own monthly returns, newest last; `dates` are the month ends they belong to."""

    dates: list[date]
    returns: Arr


def normalised_mix(mix: dict[str, float]) -> Arr:
    w = np.array([max(0.0, float(mix.get(c, 0.0))) for c in CLASSES])
    if w.sum() <= 0:
        raise ValueError("The mix needs some weight on at least one asset class")
    return np.asarray(w / w.sum(), dtype=np.float64)


def _factor(corr: list[list[float]], vol: Arr) -> Arr:
    """L with L @ L.T = the monthly log-return covariance. Zero-volatility classes simply drop out."""
    c = np.array(corr, dtype=np.float64)
    if c.shape != (4, 4) or not np.allclose(c, c.T, atol=1e-9) or not np.allclose(np.diag(c), 1.0):
        raise ValueError("The correlation matrix must be 4×4, symmetric, with ones on the diagonal")
    vals, vecs = np.linalg.eigh(c)
    if vals.min() < -1e-9:
        raise ValueError("The correlation matrix is not valid (not positive semi-definite)")
    root = vecs * np.sqrt(np.clip(vals, 0.0, None))
    return np.asarray(vol[:, None] * root, dtype=np.float64)


def log_params(mean: Arr, vol: Arr) -> tuple[Arr, Arr]:
    """(monthly log-mean, monthly log-sd) so that the yearly arithmetic mean/vol of the lognormal match the inputs."""
    s2 = np.log1p(vol**2 / (1.0 + mean) ** 2)
    return (np.log1p(mean) - s2 / 2) / 12, np.sqrt(s2 / 12)


def mix_path(p: Params, months: int) -> Arr:
    """(months, classes) weights month by month: the fixed mix, or the glide path interpolated by age."""
    if not p.glide:
        return np.tile(normalised_mix(p.mix), (months, 1))
    pts = sorted(p.glide, key=lambda g: g["age"])
    ages = p.start_age + np.arange(months) / 12
    w = np.column_stack(
        [np.interp(ages, [g["age"] for g in pts], [max(0.0, g["mix"].get(c, 0.0)) for g in pts]) for c in CLASSES]
    )
    sums = w.sum(axis=1, keepdims=True)
    if (sums <= 0).any():
        raise ValueError("Every glide path point needs some weight on at least one asset class")
    return np.asarray(w / sums, dtype=np.float64)


def assumption_returns(p: Params, months: int, rng: np.random.Generator) -> Arr:
    """(months, paths) net monthly portfolio returns: correlated lognormal classes, mixed, rebalanced monthly."""
    a, w = p.assumptions, mix_path(p, months)
    mean = np.array([a.mean[c] for c in CLASSES])
    vol = np.array([a.vol[c] for c in CLASSES])
    mu_m, sd_m = log_params(mean, vol)
    chol = _factor(a.corr, sd_m)
    out = np.empty((months, p.paths))
    drag = (1.0 - p.fees) ** (1 / 12)
    for s in range(0, months, 120):  # chunked so 10 000 paths x 840 months x 4 classes never sits in memory at once
        e = min(months, s + 120)
        z = rng.standard_normal((e - s, p.paths, 4))
        out[s:e] = (np.einsum("mpc,mc->mp", np.expm1(mu_m + z @ chol.T), w[s:e]) + 1.0) * drag - 1.0
    return out


POLICIES = ("monthly", "yearly", "never")


def lever_rate(lev: Leverage, ltc: Arr) -> Arr:
    """Yearly interest by loan-to-collateral: the cheapest Superluotto tier whose loan limit holds."""
    if lev.rate is not None:
        return np.full(ltc.shape, lev.rate)
    r = np.full(ltc.shape, lev.tier_rates["base"])
    if lev.diversified:
        r = np.where(ltc <= lev.tier_limits["hopea"] + 1e-12, lev.tier_rates["hopea"], r)
        r = np.where(ltc <= lev.tier_limits["kulta"] + 1e-12, lev.tier_rates["kulta"], r)
    return np.asarray(r, dtype=np.float64)


def levered_returns(ret: Arr, lev: Leverage, stop: int | None = None) -> tuple[Arr, dict[str, Any]]:
    """(months, paths) monthly returns on your own money with the loan, and what the loan did on the way.

    At month `stop` the loan is repaid by selling (the gain share is taxed) and the rest of the plan runs unlevered.
    """
    if lev.policy not in POLICIES:
        raise ValueError(f"Leverage policy must be one of {', '.join(POLICIES)}")
    k = lev.ratio
    if not 1.0 <= k <= 10.0:
        raise ValueError("Leverage must be between 1× and 10×")
    if not 0 < lev.collateral_rate <= 0.9 or not 0 < lev.restore < 1:
        raise ValueError("The collateral rate must be 0–90 % and the restore level below 100 %")
    if k > 1 and lev.margin_calls and (k - 1) / k > lev.collateral_rate:
        raise ValueError(f"{k:g}× needs a loan above the collateral value ({lev.collateral_rate:.0%} of the portfolio)")
    months, n = ret.shape
    c, rho = lev.collateral_rate, lev.restore
    a: Any = np.full(n, k)  # assets per euro of own money
    debt: Any = np.full(n, k - 1.0)  # loan per euro of own money
    basis: Any = np.full(n, ((1 - lev.gain_share) + (k - 1)) / k)  # cost basis per euro of assets
    out = np.empty_like(ret)
    called = np.zeros(n, dtype=bool)
    ruined = np.zeros(n, dtype=bool)
    first_call: Any = np.full(n, -1, dtype=np.int64)
    interest = np.zeros(n)
    tax = np.zeros(n)
    end = months if stop is None else max(0, min(stop, months))
    for m in range(end, months):
        out[m] = ret[m]
    for m in range(end):
        a0, d0 = a, debt
        ltc = np.divide(d0, c * a0, out=np.zeros(n), where=a0 > 0)
        i_m = lever_rate(lev, ltc) / 12
        cost = d0 * i_m * (1 - lev.deductible)
        interest += cost
        a1 = a0 * (1 + ret[m])
        d1 = d0 + cost
        b1 = basis * a0  # euros of cost basis, unchanged by price moves

        def sell(amount: Arr, a1: Arr, d1: Arr, b1: Arr) -> tuple[Arr, Arr, Arr, Arr]:
            amount = np.clip(amount, 0.0, a1)
            frac = np.divide(amount, a1, out=np.zeros(n), where=a1 > 0)
            gain = amount - b1 * frac
            t = np.maximum(gain, 0.0) * lev.gain_tax
            return a1 - amount, d1 - amount + t, b1 * (1 - frac), t

        call = (d1 > c * a1) & ~ruined & lev.margin_calls
        if call.any():
            need = np.where(call, (d1 - rho * c * a1) / (1 - rho * c), 0.0)
            a1, d1, b1, t = sell(need, a1, d1, b1)
            tax += t
            called |= call
            first_call = np.where(call & (first_call < 0), m, first_call)
        if m == end - 1 and end < months:  # pay it all back before the unlevered part
            a1, d1, b1, t = sell(np.where(~ruined, d1, 0.0), a1, d1, b1)
            tax += t
        elif k > 1 and (lev.policy == "monthly" or (lev.policy == "yearly" and m % 12 == 11)):
            target = (k - 1) * (a1 - d1)
            over = np.where(~ruined, np.maximum(d1 - target, 0.0), 0.0)
            a1, d1, b1, t = sell(over, a1, d1, b1)
            tax += t
            under = np.where(~ruined & (a1 > d1), np.maximum((k - 1) * (a1 - d1) - d1, 0.0), 0.0)
            a1, d1, b1 = a1 + under, d1 + under, b1 + under
        eq = a1 - d1
        broke = (eq <= 1e-9) & ~ruined
        ruined |= broke
        out[m] = np.where(ruined & ~broke, 0.0, np.maximum(eq, 0.0) - 1.0)
        # a ruined path starts again unlevered on whatever new money comes in
        safe = np.where(eq > 1e-9, eq, 1.0)
        a = np.where(ruined, 1.0, a1 / safe)
        debt = np.where(ruined, 0.0, d1 / safe)
        basis = np.where(ruined, 1.0, np.divide(b1, a1, out=np.ones(n), where=a1 > 0))
    years = max(end, 1) / 12
    info = {
        "ratio": k,
        "policy": lev.policy,
        "margin_call": float(called.mean()),
        "first_call_median_year": float(np.median(first_call[called]) / 12) if called.any() else None,
        "ruin": float(ruined.mean()),
        "interest_per_year": float(interest.mean() / years),  # per euro of own money, after tax relief
        "forced_tax_per_year": float(tax.mean() / years),
        "levered_years": end / 12,
    }
    return np.asarray(out, dtype=np.float64), info


def hedged_returns(ret: Arr, h: Hedge, vol: float, rate: float) -> tuple[Arr, dict[str, Any]]:
    """Monthly returns with the rolling hedge; the period's net result (after tax) lands in its last month."""
    from app.modules.hedging import options as opt

    if h.kind not in ("put", "collar", "short"):
        raise ValueError("The hedge is a put, a collar or a short")
    if not 0 <= h.ratio <= 2 or not 1 <= h.tenor <= 24:
        raise ValueError("Hedge 0–200 % with a 1–24 month period")
    t = h.tenor / 12
    iv_put, iv_call = vol + h.skew, max(vol - h.skew / 2, 0.02)
    premium = opt.put(h.strike, t, rate, h.dividend_yield, iv_put) if h.kind != "short" else 0.0
    cap = opt.collar_cap(h.strike, t, rate, h.dividend_yield, iv_put, iv_call) if h.kind == "collar" else None
    cost = h.ratio * ((premium if h.kind == "put" else 0.0) + h.fee + (h.short_cost * t if h.kind == "short" else 0.0))
    out = ret.copy()
    months = ret.shape[0]
    paid = got = 0.0
    for m0 in range(0, months, h.tenor):
        m1 = min(m0 + h.tenor, months)
        g = np.prod(1.0 + ret[m0:m1], axis=0)  # the portfolio's growth over the period
        if h.kind == "short":
            payoff = h.ratio * (1.0 - g)
        else:
            payoff = h.ratio * np.maximum(h.strike - g, 0.0)
            if cap is not None:
                payoff = payoff - h.ratio * np.maximum(g - cap, 0.0)
        pnl = payoff - cost
        after = pnl * (1.0 - h.tax)
        out[m1 - 1] = (1.0 + ret[m1 - 1]) * (g + after) / g - 1.0
        paid += cost
        got += float(np.mean(payoff))
    years = months / 12
    return np.asarray(out, dtype=np.float64), {
        "kind": h.kind,
        "ratio": h.ratio,
        "premium": premium,
        "cap": cap,
        "cost_per_year": paid / years,
        "payoff_per_year": got / years,
    }


def _lever_stop(p: Params) -> int:
    """Month the loan is repaid: its own age, else when withdrawals start."""
    lev = p.leverage or Leverage()
    age = lev.until_age if lev.until_age is not None else p.withdrawal.start_age
    return int(round((max(age, p.start_age) - p.start_age) * 12))


def bootstrap_returns(h: History, months: int, paths: int, rng: np.random.Generator) -> Arr:
    """(months, paths) circular block bootstrap of the portfolio's own monthly returns, blocks of 12 months."""
    n = len(h.returns)
    blocks = -(-months // BLOCK)
    starts = rng.integers(0, n, size=(blocks, 1, paths))
    idx = (starts + np.arange(BLOCK)[None, :, None]) % n
    return np.asarray(h.returns[idx].reshape(blocks * BLOCK, paths)[:months], dtype=np.float64)


def guardrails_step(w_prev: Arr, value: Arr, r0: float, losing: NDArray[np.bool_], infl: float) -> Arr:
    """Guyton–Klinger yearly update of the yearly withdrawal `w_prev` (nominal) against the portfolio `value`.

    Inflation raise is skipped after a losing year when the current rate is above the initial rate; then a rate
    above 120 % of the initial one cuts the withdrawal 10 %, below 80 % raises it 10 %.
    """
    safe = np.where(value > 0, value, np.inf)
    skip = losing & (w_prev / safe > r0)
    w = np.where(skip, w_prev, w_prev * (1.0 + infl))
    rate = w / safe
    return np.asarray(np.where(rate > r0 * 1.2, w * 0.9, np.where(rate < r0 * 0.8, w * 1.1, w)), dtype=np.float64)


def vpw_payment(value_real: Arr, r_real: float, months_left: int) -> Arr:
    """Level monthly payment (paid at month start) that empties `value_real` in `months_left` months at `r_real`."""
    rm = (1.0 + r_real) ** (1 / 12) - 1.0
    if abs(rm) < 1e-12:
        return np.asarray(value_real / months_left, dtype=np.float64)
    return np.asarray(value_real * rm / ((1.0 - (1.0 + rm) ** -months_left) * (1.0 + rm)), dtype=np.float64)


@dataclass
class Ctx:
    """Everything the strategies share: the paths, the deflator, the flows and the accumulation-phase result."""

    p: Params
    months: int  # total months to the horizon
    mw: int  # month index where withdrawals start
    ret: Arr  # (months, paths) net monthly returns
    defl: Arr  # (months + 1) price level, today = 1
    contrib: Arr  # (months,) nominal monthly contribution
    lump: Arr  # (months,) nominal lump sums
    pension: Arr  # (months,) nominal monthly pension
    v_pre: Arr  # value at month mw before that month's flows
    yg: Arr  # growth over the 12 months before mw
    samples: Arr  # (years + 1, paths) nominal values at whole years, filled up to mw
    r_real: float  # the mix's expected real return, for VPW


@dataclass
class Outcome:
    samples: Arr  # (years + 1, paths) nominal
    short: NDArray[np.int64]  # month of the first unpaid draw, -1 if none
    yearly: Arr  # (withdrawal years, paths) net withdrawals in today's euros
    end_real: Arr


def _clamp(v: Arr) -> Arr:
    return np.asarray(np.maximum(v, 0.0), dtype=np.float64)


def build_ctx(p: Params, ret: Arr) -> Ctx:
    """Flows and the accumulation phase (shared by every strategy, since they only differ from withdrawal start)."""
    months = ret.shape[0]
    n = p.paths
    wd = p.withdrawal
    mw = int(round((max(wd.start_age, p.start_age) - p.start_age) * 12))
    defl = (1.0 + p.inflation) ** (np.arange(months + 1) / 12)
    m_idx = np.arange(months)
    c = p.contributions
    until = wd.start_age if c.until_age is None else c.until_age
    inc = p.inflation if c.yearly_increase is None else c.yearly_increase
    stop = int(round((until - p.start_age) * 12))
    contrib = np.where(m_idx < stop, c.monthly * (1.0 + inc) ** (m_idx // 12), 0.0)
    lump = np.zeros(months)
    for lp in c.lumps:
        i = int(round((float(lp["age"]) - p.start_age) * 12))
        if 0 <= i < months:
            lump[i] += float(lp["amount"]) * defl[i]
    pm = int(round((wd.pension_age - p.start_age) * 12))
    pension = np.where(m_idx >= max(pm, mw), wd.pension_monthly * defl[:months], 0.0)
    years = months // 12
    samples = np.zeros((years + 1, n))
    v: Arr = np.full(n, float(p.start_value))
    yg = np.ones(n)
    for m in range(mw):
        if m % 12 == 0:
            samples[m // 12] = v
            yg = np.ones(n)
        v = _clamp(v + lump[m]) + contrib[m]
        g = 1.0 + ret[m]
        v = v * g
        yg = yg * g
    if mw % 12 == 0:
        samples[mw // 12] = v
    # VPW plans with the growth a portfolio compounds at (arithmetic mean less half its variance); the arithmetic
    # mean would pay out more than the median path earns and empty the pot early
    w = mix_path(p, months)[min(mw, months - 1)]  # with a glide path: the mix withdrawals start from
    a = p.assumptions
    mean = np.array([a.mean[k] for k in CLASSES])
    chol = _factor(a.corr, np.array([a.vol[k] for k in CLASSES]))
    var = float(w @ (chol @ chol.T) @ w)
    geo = float(w @ mean) - var / 2
    r_real = (1.0 + geo) * (1.0 - p.fees) / (1.0 + p.inflation) - 1.0
    return Ctx(p, months, mw, ret, defl, contrib, lump, pension, v, yg, samples, r_real)


def rate_of(w: Withdrawal) -> float:
    return DEFAULT_RATE.get(w.strategy, 0.0) if w.rate is None else w.rate


def run_strategy(c: Ctx, w: Withdrawal) -> Outcome:
    """The withdrawal phase of one strategy on the shared paths."""
    n, mw, months, d = c.p.paths, c.mw, c.months, c.defl
    samples = c.samples.copy()
    v, yg = c.v_pre.copy(), c.yg.copy()
    short = np.full(n, -1, dtype=np.int64)
    yearly = np.zeros((max(1, -(-(months - mw) // 12)), n))
    r0, keep = rate_of(w), 1.0 - w.tax
    tn: Any = np.zeros(n)  # nominal monthly target held between resets
    tr: Any = np.zeros(n)  # target in today's euros, for strategies indexed month by month
    gn: Any = np.zeros(n)  # guardrails: nominal yearly amount
    for m in range(mw, months):
        k = m - mw
        if m % 12 == 0 and m > mw:
            samples[m // 12] = v
        if c.lump[m]:
            v = _clamp(v + c.lump[m])
        if k % 12 == 0:  # first month and every January: (re)set the rule
            if w.strategy == "four_percent" and k == 0:
                tr = r0 * v / 12 / d[m]
            elif w.strategy == "percent_of_portfolio":
                tn = r0 * v / 12
            elif w.strategy == "guardrails":
                gn = r0 * v if k == 0 else guardrails_step(gn, v, r0, yg < 1.0, c.p.inflation)
                tn = gn / 12
            elif w.strategy == "vpw":
                tr = vpw_payment(v / d[m], c.r_real, months - m)
            yg = np.ones(n)
        t = (w.monthly if w.strategy == "fixed_real" else tr) * d[m] if w.strategy in INDEXED else tn
        if w.floor > 0:
            t = np.maximum(t, w.floor * d[m])
        need = np.maximum(t - c.pension[m], 0.0) / keep
        bad = (need > v * (1.0 + TOL_REL) + TOL_ABS) & (short < 0)
        if w.strategy == "vpw" and m >= months - 12:
            bad[:] = (
                False  # VPW spends the pot down to the horizon by design: a shortfall in the last year is no failure
            )
        short[bad] = m
        draw = np.minimum(need, v)
        yearly[k // 12] += draw * keep / d[m]
        v = (v - draw) * (1.0 + c.ret[m])
        yg = yg * (1.0 + c.ret[m])
    samples[months // 12] = v
    return Outcome(samples, short, yearly, v / d[months])


def lean_rate_run(c: Ctx, rate: float) -> tuple[float, float]:
    """(success share, median real ending value) of an inflation-indexed draw of `rate` of each path's start value.

    A stripped-down copy of the indexed strategy loop: the safe-rate search calls it dozens of times.
    """
    mw, months, d = c.mw, c.months, c.defl
    keep = 1.0 - c.p.withdrawal.tax
    v = c.v_pre.copy()
    if c.lump[mw]:
        v = _clamp(v + c.lump[mw])
    base = rate * v / 12 / d[mw]
    ok = np.ones(c.p.paths, dtype=bool)
    for m in range(mw, months):
        if m > mw and c.lump[m]:
            v = _clamp(v + c.lump[m])
        need = np.maximum(base * d[m] - c.pension[m], 0.0) / keep
        ok &= need <= v * (1.0 + TOL_REL) + TOL_ABS
        v = (v - np.minimum(need, v)) * (1.0 + c.ret[m])
    return float(ok.mean()), float(np.median(v / d[months]))


def search_rate(good: Any, hi: float = 0.5) -> float | None:
    """Largest rate (to 0.01 %) for which good(rate) holds, assuming it only gets harder as the rate rises."""
    if not good(0.0):
        return None
    if good(hi):
        return hi
    lo = 0.0
    while hi - lo > 1e-4:
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if good(mid) else (lo, mid)
    return lo


def _pct(a: Arr, axis: int = 1) -> dict[str, list[int]]:
    q = np.percentile(a, PCTS, axis=axis)
    return {f"p{k}": [int(round(float(x))) for x in row] for k, row in zip(PCTS, q, strict=True)}


def _summary(c: Ctx, o: Outcome) -> dict[str, Any]:
    """Success, the withdrawals and the ending value of one strategy's outcome."""
    p = c.p
    ok = o.short < 0
    per_path_mean, per_path_min = o.yearly.mean(axis=0), o.yearly.min(axis=0)
    first = o.yearly[0]
    failed = o.short[~ok]
    return {
        "success": round(float(ok.mean()), 4),
        "median_depletion_age": round(p.start_age + float(np.median(failed)) / 12, 1) if len(failed) else None,
        "first_year": {
            "median": int(round(float(np.median(first)))),
            "p10": int(round(float(np.percentile(first, 10)))),
        },
        "phase_average": {
            "median": int(round(float(np.median(per_path_mean)))),
            "p10": int(round(float(np.percentile(per_path_mean, 10)))),
        },
        "worst_year_p10": int(round(float(np.percentile(per_path_min, 10)))),
        "total_real_median": int(round(float(np.median(o.yearly.sum(axis=0))))),
        "ending_real": {f"p{k}": int(round(float(np.percentile(o.end_real, k)))) for k in (10, 50, 90)},
    }


def run(p: Params, history: History | None = None) -> dict[str, Any]:
    """Run the simulation; `history` is required (>= 36 months) in historical mode."""
    t0 = time.perf_counter()
    if p.horizon_age <= p.start_age:
        raise ValueError("The horizon age must be above your age")
    if p.withdrawal.strategy not in STRATEGIES:
        raise ValueError(f"Unknown withdrawal strategy {p.withdrawal.strategy!r}")
    if p.withdrawal.start_age >= p.horizon_age:
        raise ValueError("Withdrawals must start before the horizon age")
    if p.target and not p.start_age <= p.target["age"] <= p.horizon_age:
        raise ValueError("The target age must lie between your age and the horizon")
    months = (p.horizon_age - p.start_age) * 12
    rng = np.random.default_rng(p.seed)
    hist_info: dict[str, Any] | None = None
    if p.glide and p.return_model == "historical":
        raise ValueError("A glide path needs the assumptions model: your own history has one fixed mix")
    if p.return_model == "historical":
        if history is None or len(history.returns) < MIN_HISTORY:
            have = 0 if history is None else len(history.returns)
            raise ValueError(
                f"Historical mode needs at least {MIN_HISTORY} months of your portfolio's own price history; "
                f"{have} are available. Use the assumptions model instead."
            )
        ret = bootstrap_returns(history, months, p.paths, rng)
        n_h = len(history.returns)
        cagr = float(np.prod(1.0 + history.returns)) ** (12 / n_h) - 1.0
        hist_info = {"months": n_h, "from": history.dates[0], "to": history.dates[-1], "cagr": cagr}
        if n_h * 3 < months:  # a few good (or bad) years stretched over a lifetime
            hist_info["warning"] = (
                f"Only {n_h // 12} years of your portfolio's history ({history.dates[0]:%Y}–{history.dates[-1]:%Y}, "
                f"{cagr:.1%} a year) are resampled over {months // 12} years; that period may be far better or worse "
                "than the long run. For long plans, compare with the assumptions model."
            )
    elif p.return_model == "assumptions":
        ret = assumption_returns(p, months, rng)
    else:
        raise ValueError(f"Unknown return model {p.return_model!r}")
    hedge_info = None
    if p.hedge is not None and p.hedge.ratio > 0:
        a = p.assumptions
        w = mix_path(p, 1)[0]
        cov = _factor(a.corr, np.array([a.vol[k] for k in CLASSES]))
        vol = float(np.sqrt(w @ (cov @ cov.T) @ w))
        ret, hedge_info = hedged_returns(ret, p.hedge, vol, float(a.mean["cash"]))
    base_ret = ret
    lev_info = None
    if p.leverage is not None and p.leverage.ratio > 1.0:
        ret, lev_info = levered_returns(base_ret, p.leverage, _lever_stop(p))
    c = build_ctx(p, ret)
    if lev_info is not None:  # VPW plans with what the levered paths compound at
        geo = float(np.expm1(12 * np.mean(np.log1p(np.maximum(ret, -0.999999)))))
        c.r_real = (1.0 + geo) / (1.0 + p.inflation) - 1.0
    wd = p.withdrawal
    main = run_strategy(c, wd)
    res = _summary(c, main)

    ages = list(range(p.start_age, p.horizon_age + 1))
    defl_y = c.defl[::12][: len(ages)]
    real = main.samples / defl_y[:, None]
    wi = int(round(max(wd.start_age, p.start_age))) - p.start_age
    target = None
    if p.target:
        j = int(round(p.target["age"])) - p.start_age
        target = {
            "age": p.target["age"],
            "value_real": p.target["value_real"],
            "probability": round(float((real[j] >= p.target["value_real"]).mean()), 4),
        }
    compare = []
    for s in STRATEGIES:
        w2 = wd if s == wd.strategy else Withdrawal(**{**wd.__dict__, "strategy": s, "rate": None})
        o = main if s == wd.strategy else run_strategy(c, w2)
        sm = res if s == wd.strategy else _summary(c, o)
        compare.append(
            {
                "strategy": s,
                "selected": s == wd.strategy,
                "rate": rate_of(w2) if s in DEFAULT_RATE else None,
                "success": sm["success"],
                "median_yearly_withdrawal": sm["phase_average"]["median"],
                "first_year_median": sm["first_year"]["median"],
                "p10_worst_year": sm["worst_year_p10"],
                "median_ending_real": sm["ending_real"]["p50"],
            }
        )
    start_real = float(np.median(real[wi]))
    safe = search_rate(lambda r: lean_rate_run(c, r)[0] >= p.success_target)
    perpetual = search_rate(lambda r: lean_rate_run(c, r)[1] >= start_real)
    sweep = []
    for k in p.leverage_sweep or []:
        lv = Leverage(**{**(p.leverage or Leverage()).__dict__, "ratio": float(k)})
        r2, info = levered_returns(base_ret, lv, _lever_stop(p)) if k > 1.0 else (base_ret, None)
        c2 = build_ctx(p, r2)
        o2 = run_strategy(c2, wd)
        s2 = _summary(c2, o2)
        real2 = o2.samples / defl_y[:, None]
        sw = search_rate(lambda r, c2=c2: lean_rate_run(c2, r)[0] >= p.success_target)
        before_tax = None
        if info is not None and lv.gain_tax > 0:  # the same loan with the repayment and forced sales untaxed
            r3, _ = levered_returns(base_ret, Leverage(**{**lv.__dict__, "gain_tax": 0.0}), _lever_stop(p))
            before_tax = int(round(float(np.median(build_ctx(p, r3).v_pre / c.defl[c.mw]))))
        sweep.append(
            {
                "ratio": float(k),
                "success": s2["success"],
                "at_withdrawal_real_p50_before_tax": before_tax,
                "at_withdrawal_real_p50": int(round(float(np.median(real2[wi])))),
                "at_withdrawal_real_p10": int(round(float(np.percentile(real2[wi], 10)))),
                "ending_real_p50": s2["ending_real"]["p50"],
                "ending_real_p10": s2["ending_real"]["p10"],
                "safe_withdrawal_rate": None if sw is None else round(sw, 4),
                "margin_call": info["margin_call"] if info else 0.0,
                "ruin": info["ruin"] if info else 0.0,
                "interest_per_year": info["interest_per_year"] if info else 0.0,
            }
        )
    return {
        "ages": ages,
        "withdrawal_start_age": ages[wi],
        "nominal": _pct(main.samples),
        "real": _pct(real),
        "value_at_withdrawal_start": {
            "real_median": int(round(start_real)),
            "nominal_median": int(round(float(np.median(main.samples[wi])))),
        },
        **res,
        "target": target,
        "compare": compare,
        "safe_withdrawal_rate": None if safe is None else round(safe, 4),
        "perpetual_withdrawal_rate": None if perpetual is None else round(perpetual, 4),
        "success_target": p.success_target,
        "history": hist_info,
        "leverage": lev_info,
        "leverage_sweep": sweep,
        "hedge": hedge_info,
        "inputs": {
            "mix": dict(zip(CLASSES, (round(float(x), 4) for x in normalised_mix(p.mix)), strict=True)),
            "glide": p.glide,
            "assumptions": {"mean": p.assumptions.mean, "vol": p.assumptions.vol, "corr": p.assumptions.corr},
            "return_model": p.return_model,
            "fees": p.fees,
            "inflation": p.inflation,
            "expected_real_return": round(c.r_real, 4),
            "strategy": wd.strategy,
            "rate": rate_of(wd) if wd.strategy in DEFAULT_RATE else None,
        },
        "seed": p.seed,
        "paths": p.paths,
        "ms": round((time.perf_counter() - t0) * 1000),
    }
