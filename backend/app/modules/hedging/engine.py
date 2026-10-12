"""Hedging a fall without selling: protective puts, zero-cost collars, short mini futures and bear certificates on an
index, against simply selling (which realises taxable gains), simulated over a horizon on the same paths.

The portfolio moves as α + β × index + noise, with β and the noise from its own monthly history against the index
(the basis risk a hedge cannot remove). A hedge covers `ratio` of the β-weighted exposure and is renewed every
`tenor` months. At the horizon the hedge's net result is taxed as capital income (a loss is credited at the same
rate when other capital income absorbs it), while the portfolio's own unrealised gain stays untaxed; selling instead
pays tax on the gains it realises now (nothing inside an osakesäästötili).
"""

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.modules.hedging import options as opt

Arr = np.ndarray[Any, np.dtype[np.float64]]
KINDS = ("none", "put", "collar", "short", "bear", "sell")
LABELS = {
    "none": "No hedge",
    "put": "Protective put",
    "collar": "Zero-cost collar",
    "short": "Short mini future",
    "bear": "Bear certificate",
    "sell": "Sell part now",
}
FALLS = (-0.40, -0.30, -0.20, -0.10, 0.0, 0.10, 0.20)
# Nordnet trades standardised options on the Nordic exchanges only (Helsinki, Stockholm, Oslo, Copenhagen)
NORDNET_OPTIONS = {"OMXH25", "OMXS30"}


def option_cost(s: "Strategy", notional: Any, premium: float, call_premium: float = 0.0) -> Any:
    """Euros of brokerage and clearing to open the options on `notional` (held to expiry: no closing trade)."""
    legs = [premium] + ([call_premium] if s.kind == "collar" else [])
    total = 0.0
    for prem in legs:
        value = notional * prem
        total = total + np.maximum(s.option_fee_min, s.option_fee_pct * value) * (value > 0) + s.clearing_pct * value
    return total


class HedgeError(ValueError):
    pass


@dataclass
class Market:
    value: float  # the portfolio's value now
    beta: float  # to the index
    resid_vol: float  # yearly volatility the index does not explain
    index_mean: float = 0.07  # yearly arithmetic
    index_vol: float = 0.16
    portfolio_mean: float = 0.07
    rate: float = 0.02  # risk-free for option prices and cash
    dividend_yield: float = 0.015
    skew: float = 0.03  # implied volatility of puts above the realised one
    gain_share: float = 0.0  # unrealised gain share of what selling would sell (taxable accounts)
    tax_free_share: float = 0.0  # share of the value inside an osakesäästötili (sold first, no tax)
    gain_tax: float = 0.30
    losses_usable: bool = True  # a hedge loss is deducted from other capital income
    index_key: str | None = None


@dataclass
class Strategy:
    kind: str = "put"
    ratio: float = 1.0  # share of the β-weighted exposure hedged
    tenor: int = 3  # months between renewals (options, certificates)
    strike: float = 0.90  # put strike / today's level
    # Nordnet standardised options (price list, level 4): brokerage 0.80 % of the premium, at least 15 € an order,
    # plus the exchange's clearing fee 0.75 % of the premium; a collar trades two legs
    option_fee_pct: float = 0.008
    option_fee_min: float = 15.0
    clearing_pct: float = 0.0075
    mini_cost: float = 0.015  # yearly financing of a short mini (≈ 3 % margin − the reference rate) + fees
    mini_leverage: float = 5.0  # knock-out about 1/leverage above the start level
    bear_factor: float = 2.0
    bear_cost: float | None = None  # yearly; None: Nordnet's (factor − 1) × financing rate + 0.5 % management fee
    label: str | None = None


@dataclass
class Run:
    months: int = 12
    paths: int = 4000
    seed: int = 20261012
    strategies: list[Strategy] = field(default_factory=list)


def _bear_cost(factor: float) -> float:
    """Nordnet Markets daily leverage financing: (factor − 1) × 3 % (factor > 5), 1.5 % (= 5) or 1 % (< 5), + 0.5 %."""
    rate = 0.03 if factor > 5 else 0.015 if factor == 5 else 0.01
    return (factor - 1) * rate + 0.005


def simulate(mkt: Market, run: Run) -> dict[str, Any]:
    if not 1 <= run.months <= 60:
        raise HedgeError("The horizon is 1–60 months")
    if mkt.value <= 0:
        raise HedgeError("There is nothing to hedge")
    rng = np.random.default_rng(run.seed)
    n, months = run.paths, run.months
    s2 = math.log1p(mkt.index_vol**2 / (1 + mkt.index_mean) ** 2)
    mu_m, sd_m = (math.log1p(mkt.index_mean) - s2 / 2) / 12, math.sqrt(s2 / 12)
    idx = np.exp(mu_m + sd_m * rng.standard_normal((months, n))) - 1  # monthly index returns
    alpha = (mkt.portfolio_mean - mkt.beta * mkt.index_mean) / 12
    noise = rng.standard_normal((months, n)) * mkt.resid_vol / math.sqrt(12)
    port = alpha + mkt.beta * idx + noise
    results = [_one(mkt, run, s, idx, port) for s in (run.strategies or default_strategies())]
    base = next((r for r in results if r["kind"] == "none"), results[0])
    for r in results:
        r["cost"] = r["mean"] - base["mean"]  # expected value given up for the protection
    return {
        "months": months,
        "paths": n,
        "strategies": results,
        "scenarios": [
            {"fall": f, "values": {r["key"]: r["scenario"][i] for r in results}} for i, f in enumerate(FALLS)
        ],
        "market": mkt.__dict__,
    }


def default_strategies() -> list[Strategy]:
    return [
        Strategy("none"),
        Strategy("put", tenor=3, strike=0.90),
        Strategy("put", tenor=12, strike=0.85, label="Protective put, 1 year, 15 % down"),
        Strategy("collar", tenor=12, strike=0.90),
        Strategy("short", ratio=0.5, label="Short mini future, half the exposure"),
        Strategy("bear", ratio=0.5, bear_factor=2.0, label="Bear certificate ×2, half the exposure"),
        Strategy("sell", ratio=0.5, label="Sell half the shares now"),
    ]


def _one(mkt: Market, run: Run, s: Strategy, idx: Arr, port: Arr) -> dict[str, Any]:
    if s.kind not in KINDS:
        raise HedgeError(f"Unknown strategy {s.kind!r}")
    if not 0 <= s.ratio <= 2:
        raise HedgeError("Hedge 0–200 % of the exposure")
    months, n = idx.shape
    v = np.full(n, mkt.value)  # the portfolio itself
    cash = np.zeros(n)  # premiums, hedge results and money from selling
    pnl = np.zeros(n)  # the hedge's own result, for tax
    peak = v.copy()
    worst = np.zeros(n)
    tax_now = 0.0
    knock = np.zeros(n)
    iv_put = mkt.index_vol + mkt.skew
    iv_call = max(mkt.index_vol - mkt.skew / 2, 0.05)
    t = s.tenor / 12
    cap = opt.collar_cap(s.strike, t, mkt.rate, mkt.dividend_yield, iv_put, iv_call) if s.kind == "collar" else None
    premium = opt.put(s.strike, t, mkt.rate, mkt.dividend_yield, iv_put) if s.kind in ("put", "collar") else 0.0
    call_prem = opt.call(cap, t, mkt.rate, mkt.dividend_yield, iv_call) if cap is not None else 0.0
    upfront = 0.0
    if s.kind == "sell":
        sold = min(s.ratio * mkt.beta, 1.0) * mkt.value
        free = min(sold, mkt.tax_free_share * mkt.value)
        tax_now = (sold - free) * mkt.gain_share * mkt.gain_tax
        v = v - sold
        cash = cash + sold - tax_now
    notional = np.zeros(n)
    level = np.ones(n)  # index level relative to the start of the current hedge
    ko_level = np.full(n, np.inf)
    for m in range(months):
        if s.kind != "sell" and m % s.tenor == 0:  # (re)open the hedge on today's exposure
            if s.kind in ("put", "collar") and m > 0:
                payoff = _option_payoff(s, level, cap)
                cash += notional * payoff
                pnl += notional * payoff
            notional = s.ratio * mkt.beta * v
            level = np.ones(n)
            if s.kind in ("put", "collar"):
                cost = notional * (premium if s.kind == "put" else 0.0) + option_cost(s, notional, premium, call_prem)
                cash -= cost
                pnl -= cost
                upfront = float(np.mean(notional * (premium if s.kind == "put" else 0.0))) if m == 0 else upfront
            if s.kind == "short":
                ko_level = np.full(n, 1 + 0.95 / s.mini_leverage)
        r = idx[m]
        level = level * (1 + r)
        v = v * (1 + port[m])
        cash = cash * (1 + mkt.rate / 12)
        if s.kind == "short":
            gain = -notional * r - notional * s.mini_cost / 12
            cash += gain
            pnl += gain
            out = level >= ko_level  # knocked out: the hedge ends at its stop level and is opened again
            knock += out
            notional = np.where(out, s.ratio * mkt.beta * v, notional)
            level = np.where(out, 1.0, level)
        elif s.kind == "bear":
            factor = s.bear_factor
            dt = 1 / 12
            g = (1 + r) ** (-factor) * math.exp((-factor - factor**2) * mkt.index_vol**2 * dt / 2) - 1
            g = g - (s.bear_cost or _bear_cost(factor)) * dt
            gain = notional / factor * g  # the certificate holds notional / factor of money
            cash += gain
            pnl += gain
        w = v + cash
        peak = np.maximum(peak, w)
        worst = np.minimum(worst, w / peak - 1)
    if s.kind in ("put", "collar"):  # settle the last one at the horizon (time value counted as zero)
        payoff = _option_payoff(s, level, cap)
        cash += notional * payoff
        pnl += notional * payoff
    tax = np.where(pnl > 0, pnl * mkt.gain_tax, pnl * mkt.gain_tax if mkt.losses_usable else 0.0)
    w = v + cash - tax
    change = w / mkt.value - 1
    key = f"{s.kind}-{s.ratio:g}-{s.tenor}-{s.strike:g}-{s.bear_factor:g}"
    return {
        "key": key,
        "kind": s.kind,
        "label": s.label or _label(s),
        "ratio": s.ratio,
        "tenor": s.tenor,
        "strike": s.strike if s.kind in ("put", "collar") else None,
        "cap": cap,
        "premium": premium if s.kind in ("put", "collar") else None,
        "upfront": upfront if s.kind != "sell" else float(tax_now),
        "tax_now": tax_now,
        "mean": float(change.mean()),
        "p5": float(np.percentile(change, 5)),
        "p10": float(np.percentile(change, 10)),
        "p50": float(np.percentile(change, 50)),
        "p90": float(np.percentile(change, 90)),
        "loss_10": float((change < -0.10).mean()),
        "loss_20": float((change < -0.20).mean()),
        "drawdown_p50": float(np.percentile(worst, 50)),
        "drawdown_p10": float(np.percentile(worst, 10)),
        "hedge_tax_mean": float(tax.mean()),
        "knockouts": float(knock.mean()),
        "capital": (s.ratio * mkt.beta * mkt.value / s.mini_leverage)
        if s.kind == "short"
        else (s.ratio * mkt.beta * mkt.value / s.bear_factor)
        if s.kind == "bear"
        else None,
        "scenario": [_scenario(mkt, s, f, cap, premium, call_prem) for f in FALLS],
        "available": s.kind not in ("put", "collar") or mkt.index_key in NORDNET_OPTIONS,
    }


def _option_payoff(s: Strategy, level: Arr, cap: float | None) -> Arr:
    p = np.maximum(s.strike - level, 0.0)
    if s.kind == "collar" and cap is not None:
        p = p - np.maximum(level - cap, 0.0)
    return np.asarray(p, dtype=np.float64)


def _scenario(
    mkt: Market, s: Strategy, fall: float, cap: float | None, premium: float, call_prem: float = 0.0
) -> float:
    """Change in wealth after tax if the index moves by `fall` evenly over the horizon (no noise), one hedge period."""
    port = mkt.beta * fall
    v = 1.0 + port
    n = s.ratio * mkt.beta
    pnl = 0.0
    tax_now = 0.0
    lvl = 1 + fall
    if s.kind in ("put", "collar"):
        payoff = max(s.strike - lvl, 0.0) - (max(lvl - (cap or 9.0), 0.0) if s.kind == "collar" else 0.0)
        fees = float(option_cost(s, n * mkt.value, premium, call_prem)) / mkt.value
        pnl = n * (payoff - (premium if s.kind == "put" else 0.0)) - fees
    elif s.kind == "short":
        pnl = n * (-fall - s.mini_cost * s.tenor / 12)
    elif s.kind == "bear":
        k = s.bear_factor
        pnl = n / k * ((lvl ** (-k)) - 1 - (s.bear_cost or _bear_cost(k)) * s.tenor / 12)
    elif s.kind == "sell":
        sold = min(n, 1.0)
        free = min(sold, mkt.tax_free_share)
        tax_now = (sold - free) * mkt.gain_share * mkt.gain_tax
        v = (1 - sold) * (1 + port) + sold - tax_now
    tax = pnl * mkt.gain_tax if (pnl > 0 or mkt.losses_usable) else 0.0
    return v + pnl - tax - 1.0


def _label(s: Strategy) -> str:
    base = LABELS[s.kind]
    if s.kind in ("put", "collar"):
        return f"{base}, {s.tenor} mo, {1 - s.strike:.0%} down" + (f", {s.ratio:.0%}" if s.ratio != 1 else "")
    if s.kind in ("short", "bear", "sell"):
        return f"{base}, {s.ratio:.0%} of the exposure"
    return base
