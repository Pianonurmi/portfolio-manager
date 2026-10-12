"""New-money rebalancing: reach the target mix by buying only, so nothing is sold and no gain is realised.

Every holding is looked through, so a fund that is 80 % shares and 20 % bonds counts 80/20 both in today's mix and in
what a purchase of it adds. Each month the contribution is split over the chosen funds to bring the class weights (and,
optionally, the regions of the equity part) as close to target as that money allows: a small non-negative least
squares problem. Holdings then grow at the assumed class returns, so a plan can also be tested against market moves.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
from scipy.optimize import nnls
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import INVESTMENT_KINDS, Account, Instrument
from app.modules.planning import montecarlo as mc
from app.modules.portfolio import profiles
from app.modules.portfolio.allocation import _by_region, class_weights, rebalance
from app.modules.portfolio.valuation import Scope, Valuer
from app.platform.rules import rules_for

Arr = np.ndarray[Any, np.dtype[np.float64]]

CLASS_OF = {"Equity": "equity", "Fixed income": "bond", "Cash": "cash"}  # every other label grows like "other"
ON_TARGET = 0.01  # max drift that counts as "on target"
MAX_MONTHS = 240
TIE = 1e-6  # weight of the preference among funds that reach the target equally well
REGION_WEIGHT = 0.5  # regions of the equity part matter, but less than the asset classes
EQUALITY = 1e3  # weight that makes the month's buys add up to the money available
CASH_KEY = "cash"
SCENARIOS: dict[str, tuple[str, dict[str, float] | None]] = {
    "expected": ("Expected returns", {}),
    "equity_crash": ("Shares −30 % now", {"Equity": -0.30}),
    "equity_rally": ("Shares +20 % now", {"Equity": 0.20}),
    "bond_selloff": ("Bonds −10 % now (rates up)", {"Fixed income": -0.10}),
    "flat": ("Flat markets", None),  # no growth at all: what the contributions alone achieve
}


class PlanError(ValueError):
    pass


@dataclass
class Position:
    key: str
    name: str
    value: float
    split: dict[str, float]  # asset class label -> fraction of the position
    regions: dict[str, float] = field(default_factory=dict)  # equity region -> fraction of the position
    ter: float | None = None
    instrument_id: int | None = None
    buyable: bool = False
    held: bool = True
    account: str | None = None
    type: str = "fund"
    fee_fixed: float = 0.0  # € per buy
    fee_pct: float = 0.0  # fraction of the amount invested


@dataclass
class Params:
    targets: dict[str, float]
    monthly: float = 0.0
    lump: float = 0.0
    months: int = 60
    growth: float = 0.0  # yearly increase of the monthly amount
    min_trade: float = 50.0
    band: float = 0.05
    region_targets: dict[str, float] | None = None  # fractions of the equity part
    prefer: str = "held"  # held: in proportion to what is already owned | equal
    mean: dict[str, float] = field(default_factory=lambda: dict(mc.DEFAULT_MEAN))
    vol: dict[str, float] = field(default_factory=lambda: dict(mc.DEFAULT_VOL))
    fees: dict[str, tuple[float, float]] = field(default_factory=dict)  # instrument type -> (€ per buy, fraction)
    max_fee_share: float = 0.01  # a buy is made only when its fixed fee is at most this share of it
    # types whose fixed fee is per monthly-saving agreement, not per fund: Nordnet charges 2.50 € a month per
    # agreement of up to `agreement_size` ETFs in which something was bought
    agreement_types: tuple[str, ...] = ("etf",)
    agreement_size: int = 4
    glide: list[tuple[date, dict[str, float]]] | None = None  # target mix by date; overrides `targets` over time
    loan: float = 0.0  # securities credit today (a negative cash balance)
    loan_rate: float = 0.0  # its yearly interest after the tax deduction
    repay_first: bool = False  # new money pays the credit down before anything is bought


# ---- one month's purchase --------------------------------------------------------------------------------------


def _solve(rows: Arr, rhs: Arr, money: float) -> Arr:
    a = np.vstack([rows, np.full((1, rows.shape[1]), EQUALITY / money)])
    b = np.append(rhs, EQUALITY)
    x, _ = nnls(a, b, maxiter=50 * rows.shape[1])
    s = float(x.sum())
    return np.asarray(x * money / s if s > 0 else x, dtype=np.float64)


def allocate(
    current: Arr,
    splits: Arr,
    targets: Arr,
    money: float,
    pref: Arr,
    min_trade: float | Arr = 0.0,
    regions: tuple[Arr, Arr, Arr, Arr] | None = None,
    fixed_fee: Arr | None = None,
) -> Arr:
    """Euros per fund (≥ 0, summing to `money`) that bring the class values closest to target; all zeros when the
    money is too little for even one buy worth its fixed fee (it then waits for next month).

    min_trade: the smallest buy, overall or per fund (a fund with a fixed fee needs a bigger one);

    current: class values now (C); splits: per fund, class fractions (n×C); targets: class fractions (C, sum 1);
    pref: the split to use among equally good answers (n, sum 1); regions: (equity value by region now (R),
    per-fund equity-region fractions (n×R), region targets as fractions of equity (R), per-fund equity share (n)).
    """
    n = splits.shape[0]
    if money <= 0 or n == 0:
        return np.zeros(n)
    total = float(current.sum())
    # after buying x: value_c + Σ s_ic x_i  vs  t_c (total + Σ x_i); measured in units of this month's money
    rows = [(splits - targets).T / money]
    rhs = [(targets * total - current) / money]
    if regions is not None:
        reg_now, reg_split, reg_t, eq_share = regions
        w = np.sqrt(REGION_WEIGHT)
        rows.append(w * (reg_split - np.outer(eq_share, reg_t)).T / money)
        rhs.append(w * (reg_t * reg_now.sum() - reg_now) / money)
    # Σ (x_i/money − p_i)² / p_i: among equally good answers this is smallest when x is proportional to p
    root = np.sqrt(np.maximum(pref, 1e-9))
    rows.append(np.sqrt(TIE) * np.diag(1 / root) / money)
    rhs.append(np.sqrt(TIE) * root)
    a, b = np.vstack(rows), np.concatenate(rhs)

    mins = np.broadcast_to(np.asarray(min_trade, dtype=np.float64), (n,))
    fixed = np.zeros(n) if fixed_fee is None else fixed_fee
    active = np.ones(n, dtype=bool)
    while True:
        x = np.zeros(n)
        x[active] = _solve(a[:, active], b, money)
        small = active & (x > 0.005) & (x < mins)
        if not small.any():
            break
        if active.sum() == 1:
            if fixed[active].sum() > 0:
                return np.zeros(n)  # one buy, still too small for its fee: wait
            break
        # drop the buy furthest below its minimum and spread its money over the rest
        idx = np.where(small)[0]
        active[idx[np.argmin(x[idx] / mins[idx])]] = False
    x[x <= 0.005] = 0.0
    return x * money / x.sum() if x.sum() > 0 else x


# ---- the projection ----------------------------------------------------------------------------------------------


@dataclass
class _Run:
    months_to_band: int | None
    months_to_target: int | None
    drift_12m: float
    end_value: float
    schedule: list[dict[str, Any]]
    bought: Arr
    bought_12m: Arr
    fees: Arr
    fees_12m: Arr
    waited: int  # months when nothing was bought because every buy was too small for its fee
    loan_end: float = 0.0
    interest: float = 0.0


def _monthly_rates(labels: list[str], p: Params, flat: bool) -> Arr:
    """Monthly growth per class label from the yearly assumptions (median path: arithmetic mean − variance / 2)."""
    out = []
    for lab in labels:
        c = CLASS_OF.get(lab, "other")
        g = 0.0 if flat else p.mean.get(c, 0.0) - p.vol.get(c, 0.0) ** 2 / 2
        out.append((1 + g) ** (1 / 12) - 1)
    return np.array(out)


def _max_drift(class_vals: Arr, t: Arr) -> float:
    tot = class_vals.sum()
    return float(np.abs(class_vals / tot - t).max()) if tot > 0 else 0.0


def project(
    positions: list[Position],
    labels: list[str],
    p: Params,
    start: date,
    shock: dict[str, float] | None = None,
    flat: bool = False,
    months: int | None = None,
) -> _Run:
    months = p.months if months is None else months
    n_pos, n_cls = len(positions), len(labels)
    pct = np.array([pos.fee_pct for pos in positions], dtype=np.float64)
    share = max(p.max_fee_share, 1e-6)
    grouped = np.array([pos.type in p.agreement_types and pos.fee_fixed > 0 for pos in positions], dtype=bool)
    group_fee = max((pos.fee_fixed for pos in positions if pos.type in p.agreement_types), default=0.0)
    fixed = np.where(grouped, 0.0, np.array([pos.fee_fixed for pos in positions], dtype=np.float64))
    mins = np.maximum(p.min_trade, fixed / share)
    s = np.array([[pos.split.get(lab, 0.0) for lab in labels] for pos in positions]).reshape(n_pos, n_cls)
    vals = np.array([pos.value for pos in positions], dtype=np.float64)
    t = np.array([p.targets.get(lab, 0.0) for lab in labels])
    buy_idx = [i for i, pos in enumerate(positions) if pos.buyable]
    sb = s[buy_idx]
    pref = _pref([positions[i] for i in buy_idx], p.prefer)
    reg_names = sorted(p.region_targets or {})
    eq_col = labels.index("Equity") if "Equity" in labels else None
    reg = np.array([[pos.regions.get(r, 0.0) for r in reg_names] for pos in positions]).reshape(n_pos, len(reg_names))
    reg_t = np.array([(p.region_targets or {}).get(r, 0.0) for r in reg_names])
    eq_share = s[:, eq_col] if eq_col is not None else np.zeros(n_pos)
    class_rates = _monthly_rates(labels, p, flat)
    rate = s @ class_rates  # each position grows at the mix of its classes
    cash_col = labels.index("Cash") if "Cash" in labels else None
    cash_rate = class_rates[cash_col] if cash_col is not None else 0.0

    if shock:
        vals = vals * (1 + s @ np.array([shock.get(lab, 0.0) for lab in labels]))
    bought, bought_12 = np.zeros(n_pos), np.zeros(n_pos)
    fees, fees_12 = np.zeros(n_pos), np.zeros(n_pos)
    pending, waited = 0.0, 0  # money waiting in cash for a buy big enough for its fee
    loan, interest = p.loan, 0.0
    schedule: list[dict[str, Any]] = []
    to_band = to_target = None
    drift_12 = 0.0
    for m in range(months):
        if m:
            vals = vals * (1 + rate)
            pending *= 1 + cash_rate
        when = _add_months(start, m)
        if p.glide:
            t = np.array([target_on(p.glide, when).get(lab, 0.0) for lab in labels])
        money = p.monthly * (1 + p.growth) ** (m // 12) + (p.lump if m == 0 else 0.0) + pending
        if loan > 0:
            cost = loan * p.loan_rate / 12
            loan += cost
            interest += cost
            if p.repay_first:
                pay = min(money, loan)
                loan, money = loan - pay, money - pay
        x = np.zeros(n_pos)
        if buy_idx and money > 0:
            regions = None
            if reg_names and eq_col is not None:
                regions = (vals @ reg, reg[buy_idx], reg_t, eq_share[buy_idx])
            x[buy_idx] = allocate(vals @ s, sb, t, money, pref, mins[buy_idx], regions, fixed[buy_idx])
            g = grouped[buy_idx]
            tot = float(x[buy_idx][g].sum())
            if g.any() and 0 < tot < group_fee / share:  # the month's agreement fee would be too large a share
                keep = [k for k, i in enumerate(buy_idx) if not grouped[i]]
                x[:] = 0.0
                if keep:
                    sub = [buy_idx[k] for k in keep]
                    pk = pref[keep] / pref[keep].sum() if pref[keep].sum() > 0 else pref[keep]
                    rk = None if regions is None else (regions[0], regions[1][keep], regions[2], regions[3][keep])
                    x[sub] = allocate(vals @ s, sb[keep], t, money, pk, mins[sub], rk, fixed[sub])
        if x.sum() > 0 or money <= 0:
            pending = 0.0
        else:
            pending, waited = money, waited + (1 if buy_idx else 0)
        # what reaches the fund after its fixed and percentage fee; the agreement fee is shared by its buys
        fx = fixed.copy()
        bought_g = grouped & (x > 0)
        if bought_g.any():
            total_fee = math.ceil(bought_g.sum() / p.agreement_size) * group_fee
            fx[bought_g] = total_fee * x[bought_g] / x[bought_g].sum()
        net = np.where(x > 0, np.maximum(x - fx, 0.0) / (1 + pct), 0.0)
        fee = x - net
        vals = vals + net
        bought += x
        fees += fee
        if m < 12:
            bought_12 += x
            fees_12 += fee
        cv = vals @ s
        if cash_col is not None:
            cv[cash_col] += pending
        drift = _max_drift(cv, t)
        if to_band is None and drift <= p.band + 1e-9:
            to_band = m
        if to_target is None and drift <= ON_TARGET + 1e-9:
            to_target = m
        if m == min(11, months - 1):
            drift_12 = drift
        tot = cv.sum()
        schedule.append(
            {
                "month": _add_months(start, m).isoformat()[:7],
                "money": money,
                "buys": {positions[i].key: float(x[i]) for i in buy_idx if x[i] > 0},
                "fees": float(fee.sum()),
                "buy_fees": {positions[i].key: float(fee[i]) for i in buy_idx if x[i] > 0},
                "waiting": pending,
                "loan": loan,
                "targets": {lab: float(t[j]) for j, lab in enumerate(labels)},
                "weights": {lab: float(cv[j] / tot) if tot else 0.0 for j, lab in enumerate(labels)},
                "max_drift": drift,
                "value": float(tot),
            }
        )
    return _Run(
        to_band,
        to_target,
        drift_12,
        float(vals.sum() + pending - loan),
        schedule,
        bought,
        bought_12,
        fees,
        fees_12,
        waited,
        loan,
        interest,
    )


def target_on(glide: list[tuple[date, dict[str, float]]], on: date) -> dict[str, float]:
    """The target mix on a date: the glide path's points joined by straight lines, flat before and after."""
    pts = sorted(glide, key=lambda g: g[0])
    if on <= pts[0][0]:
        return dict(pts[0][1])
    for (d0, m0), (d1, m1) in zip(pts, pts[1:], strict=False):
        if on <= d1:
            f = (on - d0).days / max((d1 - d0).days, 1)
            return {k: (1 - f) * m0.get(k, 0.0) + f * m1.get(k, 0.0) for k in set(m0) | set(m1)}
    return dict(pts[-1][1])


def _add_months(d: date, m: int) -> date:
    y, mo = divmod(d.month - 1 + m, 12)
    return date(d.year + y, mo + 1, 1)


def _pref(buyable: list[Position], prefer: str) -> Arr:
    """How to split money among funds that serve the target equally: by what is owned (new funds count as an average
    holding) or equally."""
    n = len(buyable)
    if n == 0:
        return np.zeros(0)
    if prefer == "equal":
        return np.full(n, 1 / n)
    held = [b.value for b in buyable if b.held and b.value > 0]
    avg = sum(held) / len(held) if held else 1.0
    w = np.array([b.value if b.held and b.value > 0 else avg for b in buyable], dtype=np.float64)
    return np.asarray(w / w.sum(), dtype=np.float64)


def best_mix(positions: list[Position], labels: list[str], targets: dict[str, float]) -> tuple[dict[str, float], float]:
    """The class mix of new money closest to target that these funds can make, and its largest gap.

    In the long run the portfolio drifts towards the mix it is fed, so a target outside reach is never met."""
    buy = [b for b in positions if b.buyable]
    if not buy:
        return {}, 1.0
    s = np.array([[b.split.get(lab, 0.0) for lab in labels] for b in buy])
    t = np.array([targets.get(lab, 0.0) for lab in labels])
    x = allocate(np.zeros(len(labels)), s, t, 1.0, np.full(len(buy), 1 / len(buy)))
    mix = x @ s
    return {lab: float(mix[j]) for j, lab in enumerate(labels)}, float(np.abs(mix - t).max())


def needed_monthly(
    positions: list[Position], labels: list[str], p: Params, start: date, within: int = 12
) -> float | None:
    """The smallest monthly amount that brings the portfolio inside the band within `within` months (expected
    returns, the same lump sum); None when no amount up to the portfolio's own value does."""
    total = sum(pos.value for pos in positions)

    def ok(m: float) -> bool:
        r = project(positions, labels, replace(p, monthly=m), start, months=within)
        return r.months_to_band is not None

    hi = max(total, 1000.0)
    if not ok(hi):
        return None
    lo = 0.0
    if ok(lo):
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
        if hi - lo < 1:
            break
    return float(np.ceil(hi))


def plan(
    positions: list[Position],
    p: Params,
    start: date,
    variants: list[tuple[str, set[str]]] | None = None,
) -> dict[str, Any]:
    """The month-by-month buying plan under expected returns, plus how it fares in other markets and with other
    sets of funds."""
    if p.monthly < 0 or p.lump < 0:
        raise PlanError("Amounts cannot be negative")
    if not 1 <= p.months <= MAX_MONTHS:
        raise PlanError(f"The plan covers 1–{MAX_MONTHS} months")
    if not any(pos.buyable for pos in positions):
        raise PlanError("Choose at least one fund to buy")
    tsum = sum(p.targets.values())
    if tsum <= 0 or any(v < 0 for v in p.targets.values()):
        raise PlanError("Targets must be non-negative and add up to more than zero")
    p = replace(p, targets={k: v / tsum for k, v in p.targets.items()})
    if p.glide:
        glide = []
        for d, mix in p.glide:
            msum = sum(mix.values())
            if msum <= 0 or any(v < 0 for v in mix.values()):
                raise PlanError("Every glide path point needs a non-negative mix")
            glide.append((d, {k: v / msum for k, v in mix.items()}))
        p = replace(p, glide=glide)
    if not 0 < p.max_fee_share <= 1:
        raise PlanError("The fee limit must be above 0 and at most 100 %")
    positions = [
        replace(pos, fee_fixed=p.fees.get(pos.type, (0.0, 0.0))[0], fee_pct=p.fees.get(pos.type, (0.0, 0.0))[1])
        for pos in positions
    ]
    if p.region_targets:
        rsum = sum(p.region_targets.values())
        if rsum <= 0 or any(v < 0 for v in p.region_targets.values()):
            raise PlanError("Region targets must be non-negative")
        p = replace(p, region_targets={k: v / rsum for k, v in p.region_targets.items()})
    labels = sorted(
        {lab for pos in positions for lab in pos.split}
        | set(p.targets)
        | {lab for _, mix in (p.glide or []) for lab in mix},
        key=_label_order,
    )

    main = project(positions, labels, p, start)
    total_now = sum(pos.value for pos in positions)
    cls_now = np.array([sum(pos.value * pos.split.get(lab, 0.0) for pos in positions) for lab in labels])
    t = np.array([p.targets.get(lab, 0.0) for lab in labels])
    mix, gap = best_mix(positions, labels, p.targets)

    def buys(amounts: Arr, fees: Arr | None = None) -> list[dict[str, Any]]:
        out = [
            {
                "key": pos.key,
                "name": pos.name,
                "amount": float(amounts[i]),
                "fee": float(fees[i]) if fees is not None else pos.fee_fixed,
                "account": pos.account,
            }
            for i, pos in enumerate(positions)
            if amounts[i] > 0.005
        ]
        return sorted(out, key=lambda r: -r["amount"])

    spent = main.bought.sum()
    ter_known = [(main.bought[i], pos.ter) for i, pos in enumerate(positions) if pos.ter is not None]
    ter_cover = sum(a for a, _ in ter_known)
    ter_new = sum(a * float(ter) for a, ter in ter_known) / ter_cover if ter_cover > 0 else None

    scenarios = []
    for key, (label, shock) in SCENARIOS.items():
        r = main if key == "expected" else project(positions, labels, p, start, shock=shock or {}, flat=shock is None)
        scenarios.append(
            {
                "key": key,
                "label": label,
                "months_to_band": r.months_to_band,
                "months_to_target": r.months_to_target,
                "drift_12m": r.drift_12m,
                "end_value": r.end_value,
                "fees": float(r.fees.sum()),
            }
        )

    out_variants = []
    for name, keys in variants or []:
        sub = [replace(pos, buyable=pos.buyable and pos.key in keys) for pos in positions]
        if not any(pos.buyable for pos in sub):
            continue
        r = project(sub, labels, p, start)
        vmix, vgap = best_mix(sub, labels, p.targets)
        vt = [(r.bought[i], pos.ter) for i, pos in enumerate(sub) if pos.ter is not None]
        vc = sum(a for a, _ in vt)
        out_variants.append(
            {
                "name": name,
                "funds": sorted(keys),
                "months_to_band": r.months_to_band,
                "months_to_target": r.months_to_target,
                "drift_12m": r.drift_12m,
                "best_gap": vgap,
                "ter_new_money": float(sum(a * float(x) for a, x in vt) / vc) if vc > 0 else None,
                "fees": float(r.fees.sum()),
                "first_month": buys(np.array([r.schedule[0]["buys"].get(pos.key, 0.0) for pos in sub])),
            }
        )

    first_fee = np.array([main.schedule[0]["buy_fees"].get(pos.key, 0.0) for pos in positions])
    fee_funds = sorted({(pos.type, pos.fee_fixed) for pos in positions if pos.buyable and pos.fee_fixed > 0})
    notes = [
        "Nothing is sold, so no gains are realised: drift is closed only with new money.",
        "Funds count by what they hold: a fund that is 80 % shares and 20 % bonds adds both with every purchase.",
        "Holdings grow at the assumed yearly class returns less half the variance (a typical, median path).",
    ]
    if gap > p.band:
        notes.append(
            "These funds cannot make the target mix: the closest they can buy is "
            + ", ".join(f"{lab} {mix[lab]:.0%}" for lab in labels if mix.get(lab, 0) > 0.0005)
            + ". Add a fund of the missing class to get there."
        )
    if p.min_trade > 0:
        notes.append(f"Buys under {p.min_trade:.0f} € are folded into the others.")
    for typ, fee in fee_funds:
        if typ in p.agreement_types:
            notes.append(
                f"{typ.upper()} monthly saving costs {fee:.2f} € a month per agreement of up to {p.agreement_size} "
                f"{typ.upper()}s (Nordnet), so {typ.upper()}s are bought only in months with at least "
                f"{fee / p.max_fee_share:.0f} € for them ({p.max_fee_share:.1%} fee at most); otherwise the money goes "
                "to fee-free funds doing the same job, or waits in cash."
            )
            continue
        notes.append(
            f"Each {typ} buy costs {fee:.2f} €, so one is made only from {fee / p.max_fee_share:.0f} € "
            f"({p.max_fee_share:.1%} fee at most); smaller amounts go elsewhere or wait in cash."
        )
    if main.waited:
        notes.append(f"In {main.waited} of {p.months} months nothing was bought: the money waited for a bigger buy.")
    if p.glide:
        notes.append("Targets follow your glide path, so the plan aims at a moving mix.")
    if p.loan > 0:
        exp = p.mean.get("equity", 0.0) - p.vol.get("equity", 0.0) ** 2 / 2
        notes.append(
            f"Your credit costs {p.loan_rate:.2%} a year after the tax deduction"
            + (
                f"; paying it down first earns that rate for sure, against an expected {exp:.1%} (median) from shares."
                if p.repay_first
                else f"; repaying it first would earn that for sure, against {exp:.1%} (median) from shares."
            )
        )

    return {
        "as_of": start.isoformat(),
        "total": total_now,
        "classes": labels,
        "targets": {lab: p.targets.get(lab, 0.0) for lab in labels},
        "band": p.band,
        "now": {lab: float(cls_now[j] / total_now) if total_now else 0.0 for j, lab in enumerate(labels)},
        "drift_now": _max_drift(cls_now, t),
        "best_mix": mix,
        "best_gap": gap,
        "first_month": buys(np.array([main.schedule[0]["buys"].get(pos.key, 0.0) for pos in positions]), first_fee),
        "first_month_waiting": main.schedule[0]["waiting"],
        "fees": float(main.fees.sum()),
        "fees_12m": float(main.fees_12m.sum()),
        "fee_share": float(main.fees.sum() / spent) if spent > 0 else 0.0,
        "months_waited": main.waited,
        "loan": {"now": p.loan, "end": main.loan_end, "interest_after_tax": main.interest, "rate": p.loan_rate}
        if p.loan > 0
        else None,
        "after_first_month": main.schedule[0]["weights"],
        "schedule": main.schedule,
        "totals_12m": buys(main.bought_12m, main.fees_12m),
        "totals": buys(main.bought, main.fees),
        "spent": float(spent),
        "months_to_band": main.months_to_band,
        "months_to_target": main.months_to_target,
        "needed_monthly": needed_monthly(positions, labels, p, start) if gap <= p.band else None,
        "ter_new_money": ter_new,
        "scenarios": scenarios,
        "variants": out_variants,
        "funds": [
            {
                "key": pos.key,
                "name": pos.name,
                "instrument_id": pos.instrument_id,
                "value": pos.value,
                "split": pos.split,
                "regions": pos.regions,
                "ter": pos.ter,
                "held": pos.held,
                "buyable": pos.buyable,
                "account": pos.account,
                "type": pos.type,
                "fee": pos.fee_fixed,
            }
            for pos in positions
        ],
        "notes": notes,
    }


def _label_order(lab: str) -> tuple[int, str]:
    order = ["Equity", "Fixed income", "Cash", "Other"]
    return (order.index(lab) if lab in order else len(order), lab)


# ---- from the ledger ---------------------------------------------------------------------------------------------


def is_candidate(inst: Instrument) -> bool:
    return bool((inst.meta or {}).get("candidate"))


def positions_for(
    db: Session,
    scope: Scope,
    on: date,
    chosen: set[int] | None = None,
    cash: bool | None = None,
    targets: dict[str, float] | None = None,
) -> list[Position]:
    """Today's holdings by instrument (cash as one), plus candidate funds not yet owned. Buyable: `chosen`
    instruments, else every held ETF/fund and every candidate; cash is buyable when it has a target."""
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    holdings = v.holdings(on)
    cands = [i for i in db.scalars(select(Instrument).where(Instrument.type.in_(("etf", "fund")))) if is_candidate(i)]
    ids = {h.instrument_id for h in holdings} | {c.id for c in cands}
    insts = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(ids)))}
    eff = profiles.effective_many(db, insts.values())

    value: dict[int, float] = defaultdict(float)
    by_acct: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    cash_value = 0.0
    for h in holdings:
        if insts[h.instrument_id].type == "cash":
            cash_value += max(float(h.value), 0.0)  # a negative balance is credit, reported by `credit_for`
            continue
        value[h.instrument_id] += float(h.value)
        by_acct[h.instrument_id][h.account_id] += float(h.value)
    fund_home = _fund_account(v.accounts, by_acct, insts)

    out: list[Position] = []
    for iid in sorted(ids, key=lambda i: -value.get(i, 0.0)):
        inst = insts[iid]
        if inst.type == "cash":
            continue
        held = value.get(iid, 0.0) > 0
        if not held and not is_candidate(inst):
            continue
        data = eff[iid].data
        split = {k: float(x) for k, x in class_weights(inst, data, "Other").items()}
        eq = split.get("Equity", 0.0)
        regs = data.get("regions") or _by_region(data.get("countries"))
        rs = sum(x for x in regs.values() if x and x > 0)
        regions = {r: eq * x / rs for r, x in regs.items() if x and x > 0} if eq and rs > 0 else {}
        if chosen is not None:
            buyable = iid in chosen
        else:
            buyable = inst.type in ("etf", "fund")
        accts = by_acct.get(iid)
        account = v.accounts[max(accts, key=lambda a: accts[a])].name if accts else fund_home
        out.append(
            Position(
                key=str(iid),
                name=inst.name,
                value=value.get(iid, 0.0),
                split=split,
                regions=regions,
                ter=float(data["ter"]) if data.get("ter") is not None else None,
                instrument_id=iid,
                buyable=buyable,
                held=held,
                account=account,
                type=inst.type,
            )
        )
    t = targets or {}
    out.append(
        Position(
            key=CASH_KEY,
            name="Cash (keep in the account)",
            value=cash_value,
            split={"Cash": 1.0},
            ter=0.0,
            buyable=cash if cash is not None else t.get("Cash", 0.0) > 0,
            held=cash_value > 0,
            type="cash",
        )
    )
    return out


def credit_for(db: Session, scope: Scope, on: date) -> float:
    """Securities credit in the investment accounts: the sum of negative cash balances."""
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    cash_ids = {i.id for i in db.scalars(select(Instrument).where(Instrument.type == "cash"))}
    return float(-sum((h.value for h in v.holdings(on) if h.instrument_id in cash_ids and h.value < 0), Decimal(0)))


def _fund_account(
    accounts: dict[int, Account], by_acct: dict[int, dict[int, float]], insts: dict[int, Instrument]
) -> str | None:
    """Where a fund not yet owned would be bought: the account holding the most funds (an osakesäästötili cannot
    hold ETFs or funds), else any book-entry account."""
    fund_value: dict[int, float] = defaultdict(float)
    for iid, per in by_acct.items():
        if insts[iid].type in ("etf", "fund"):
            for a, x in per.items():
                if accounts[a].kind != "ost":
                    fund_value[a] += x
    if fund_value:
        return accounts[max(fund_value, key=lambda a: fund_value[a])].name
    aot = [a for a in accounts.values() if a.kind == "aot"]
    return aot[0].name if aot else None


def sell_alternative(db: Session, scope: Scope, targets: dict[str, float], band: float, on: date) -> dict[str, Any]:
    """What rebalancing by selling today would cost in tax (realised gains × the lower capital income rate,
    before any deemed acquisition cost)."""
    r = rebalance(db, scope, {k: Decimal(str(v)) for k, v in targets.items()}, on, Decimal(0), Decimal(str(band)))
    sells = [tr for tr in r["trades"] if tr["action"] == "sell"]
    gain = sum((tr["taxable_gain_estimate"] for tr in sells), Decimal(0))
    rate = Decimal(rules_for(db, "tax", on.year)["capital_income"]["brackets"][0]["rate"])
    return {
        "sold": float(sum((tr["amount"] for tr in sells), Decimal(0))),
        "taxable_gain": float(gain),
        "tax_estimate": float(gain * rate),
        "rate": float(rate),
        "sells": [
            {"instrument": tr["instrument"], "account": tr["account"], "amount": float(tr["amount"])} for tr in sells
        ],
    }
