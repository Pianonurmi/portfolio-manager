"""Tax-aware withdrawal order and "where should new money go" (PLAN-09). Deterministic, real euros, yearly.

Buckets and Finnish taxation at withdrawal:
- cash: no tax;
- taxable book-entry account: a sale's gain is proceeds − max(cost, deemed cost); deemed cost
  (hankintameno-olettama) is 20 % of the price, 40 % once the holding is 10 years old;
- osakesäästötili and kapitalisaatiosopimus: a withdrawal is taxed on its gain share (value − deposits) / value;
- capital income tax 30 %, 34 % above the bracket limit, from the year's rule table.

Simplifications, stated in the result: returns are accumulated (no yearly dividends), one blended return for all
buckets, the deemed-cost switch uses the value-weighted holding age, and the small-disposals exemption is ignored.
"""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Account
from app.modules.portfolio import lots as lotmod
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.tax.service import NON_TAXABLE_WRAPPERS, ost_status
from app.platform.rules import rules_for

STRATEGIES = {
    "taxable_first": "Book-entry account first",
    "wrappers_first": "OST and wrappers first",
    "proportional": "All accounts in proportion",
    "gains_budget": "Book-entry within the 30 % bracket",
}


@dataclass
class Buckets:
    cash: float = 0.0
    taxable: float = 0.0
    taxable_cost: float = 0.0
    taxable_age: float = 0.0  # value-weighted years held today
    wrapper: float = 0.0  # OST + kapitalisaatiosopimus
    wrapper_deposits: float = 0.0

    @property
    def total(self) -> float:
        return self.cash + self.taxable + self.wrapper


@dataclass
class TaxRules:
    rate_low: float = 0.30
    rate_high: float = 0.34
    bracket: float = 30000.0
    deemed_short: float = 0.20
    deemed_long: float = 0.40

    def tax(self, income: float) -> float:
        if income <= 0:
            return 0.0
        return min(income, self.bracket) * self.rate_low + max(0.0, income - self.bracket) * self.rate_high


@dataclass
class Plan:
    age_now: int
    retire_age: int
    horizon_age: int
    real_return: float
    monthly_spending: float
    pension_monthly: float
    pension_age: int
    monthly_savings: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)


def buckets_from_data(db: Session, scope: Scope, on: date) -> Buckets:
    v = Valuer(db, scope)
    kinds = {a.id: a.kind for a in db.scalars(select(Account))}
    b = Buckets()
    book = lotmod.build(db, list(v.accounts), until=on)
    age_weighted = 0.0
    for h in v.holdings(on):
        kind = kinds.get(h.account_id)
        val = float(h.value)
        if h.instrument_id in v.book.cash_ccy:
            b.cash += val
        elif kind in NON_TAXABLE_WRAPPERS:
            b.wrapper += val
        else:
            b.taxable += val
            share = float(v.shares.on(h.account_id, on))
            ls = book.lots(h.account_id, h.instrument_id)
            b.taxable_cost += float(sum((lot.cost for lot in ls), Decimal(0))) * share
            held = sum((float(lot.quantity) for lot in ls), 0.0)
            if held:
                age_weighted += val * sum(float(lot.quantity) * (on - lot.acquired).days / 365.25 for lot in ls) / held
    b.taxable_age = age_weighted / b.taxable if b.taxable else 0.0
    for o in ost_status(db, scope, on):
        b.wrapper_deposits += float(o["deposits"])
    return b


def rules_from_db(db: Session, year: int) -> TaxRules:
    r = rules_for(db, "tax", year)
    br = sorted(r["capital_income"]["brackets"], key=lambda x: float(x["from"]))
    dac = r["deemed_acquisition_cost"]
    return TaxRules(
        rate_low=float(br[0]["rate"]),
        rate_high=float(br[-1]["rate"]),
        bracket=float(br[-1]["from"]) if len(br) > 1 else 1e12,
        deemed_short=float(dac["held_under_10_years"]),
        deemed_long=float(dac["held_10_years_or_more"]),
    )


def _deemed(b: Buckets, years: float, t: TaxRules) -> float:
    return t.deemed_long if b.taxable_age + years >= 10 else t.deemed_short


def _sell_taxable(b: Buckets, gross: float, years: float, t: TaxRules) -> float:
    """Sell `gross` from the taxable bucket; returns the gain (negative = deductible loss). Average cost, and
    the deemed cost when it is higher (it can never create a loss)."""
    gross = min(max(gross, 0.0), b.taxable)
    if gross <= 0:
        return 0.0
    cost = b.taxable_cost * gross / b.taxable
    b.taxable -= gross
    b.taxable_cost -= cost
    return gross - max(cost, gross * _deemed(b, years, t))


def _take_wrapper(b: Buckets, gross: float) -> float:
    gross = min(max(gross, 0.0), b.wrapper)
    if gross <= 0:
        return 0.0
    gain_share = max(0.0, (b.wrapper - b.wrapper_deposits) / b.wrapper)
    b.wrapper_deposits -= gross * (1 - gain_share)
    b.wrapper -= gross
    return gross * gain_share


def _apply(b: Buckets, gross: float, strategy: str, years: float, t: TaxRules) -> float:
    """Take `gross` from the buckets in the strategy's order; returns the year's taxable capital income."""
    g = gross
    cash = min(b.cash, g)
    b.cash -= cash
    g -= cash
    income = 0.0
    if g <= 0:
        return 0.0
    if strategy == "taxable_first":
        s = min(g, b.taxable)
        income += _sell_taxable(b, s, years, t)
        income += _take_wrapper(b, g - s)
    elif strategy == "wrappers_first":
        w = min(g, b.wrapper)
        income += _take_wrapper(b, w)
        income += _sell_taxable(b, g - w, years, t)
    elif strategy == "gains_budget":
        per_euro = 1.0 - max(b.taxable_cost / b.taxable if b.taxable else 1.0, _deemed(b, years, t))
        cap = t.bracket / per_euro if per_euro > 0 else g
        s = min(g, b.taxable, cap)
        income += _sell_taxable(b, s, years, t)
        w = min(g - s, b.wrapper)
        income += _take_wrapper(b, w)
        income += _sell_taxable(b, g - s - w, years, t)
    else:  # proportional
        tot = b.taxable + b.wrapper
        s = g * b.taxable / tot if tot else 0.0
        income += _sell_taxable(b, s, years, t)
        income += _take_wrapper(b, g - s)
    return income


def _withdraw(b: Buckets, need: float, strategy: str, years: float, t: TaxRules) -> tuple[float, float]:
    """Withdraw the gross amount that leaves `need` after this year's tax (bisection). Returns (gross, tax)."""

    def trial(g: float) -> tuple[float, Buckets, float]:
        x = Buckets(**b.__dict__)
        tax = t.tax(_apply(x, g, strategy, years, t))
        return g - tax, x, tax

    total = b.total
    net_all, after_all, tax_all = trial(total)
    if net_all <= need:  # not enough left: everything goes
        b.__dict__.update(after_all.__dict__)
        return total, tax_all
    lo, hi = need, total
    for _ in range(50):
        mid = (lo + hi) / 2
        if trial(mid)[0] < need:
            lo = mid
        else:
            hi = mid
    _, after, tax = trial(hi)
    b.__dict__.update(after.__dict__)
    return hi, tax


def simulate(plan: Plan, start: Buckets, strategy: str, t: TaxRules) -> dict[str, Any]:
    b = Buckets(**start.__dict__)
    ages, wealth, taxes = [], [], []
    depleted: int | None = None
    total_tax = 0.0
    for age in range(plan.age_now, plan.horizon_age + 1):
        years = age - plan.age_now
        growth = 1 + plan.real_return
        b.taxable *= growth
        b.wrapper *= growth
        lump = sum(float(e["amount"]) for e in plan.events if int(e.get("age", -1)) == age)
        b.cash += max(0.0, lump)
        tax = 0.0
        if age <= plan.retire_age:
            b.taxable += plan.monthly_savings * 12  # new savings go to the taxable account in this view
            b.taxable_cost += plan.monthly_savings * 12
        else:
            need = plan.monthly_spending * 12 - (plan.pension_monthly * 12 if age >= plan.pension_age else 0.0)
            need += max(0.0, -lump)
            if need > 0:
                gross, tax = _withdraw(b, need, strategy, years, t)
                if gross - tax < need - 1 and depleted is None:
                    depleted = age
            else:
                b.cash += -need
        total_tax += tax
        ages.append(age)
        wealth.append(round(b.total))
        taxes.append(round(tax))
    # what the heirs or you would net if everything were sold at the horizon
    end = Buckets(**b.__dict__)
    gain = _sell_taxable(end, end.taxable, plan.horizon_age - plan.age_now, t) + _take_wrapper(end, end.wrapper)
    after_tax_end = b.total - t.tax(gain)
    return {
        "strategy": strategy,
        "label": STRATEGIES[strategy],
        "ages": ages,
        "wealth": wealth,
        "tax": taxes,
        "total_tax": round(total_tax),
        "depleted_age": depleted,
        "end_wealth": round(b.total),
        "end_after_tax": round(after_tax_end),
    }


def compare(plan: Plan, start: Buckets, t: TaxRules) -> dict[str, Any]:
    runs = [simulate(plan, start, s, t) for s in STRATEGIES]
    best = max(runs, key=lambda r: (r["depleted_age"] or 999, r["end_after_tax"]))
    return {
        "buckets": {k: round(v, 2) for k, v in start.__dict__.items()},
        "runs": runs,
        "best": best["strategy"],
        "assumptions": [
            "real returns, the same for every account",
            "returns accumulate (no yearly dividends)",
            "savings before retirement go to the taxable account",
            "taxes computed on real gains (nominal gains, and so taxes, are somewhat higher)",
            "small-disposals exemption ignored",
        ],
    }


def _lots_gain(lots: list[tuple[float, float, int]], t: TaxRules) -> float:
    """[(cost, value, years held)] sold at once: per lot the better of actual and deemed cost."""
    return sum(v - max(c, v * (t.deemed_long if held >= 10 else t.deemed_short)) for c, v, held in lots)


def new_money(
    monthly: float,
    years: int,
    real_return: float,
    t: TaxRules,
    ost_room: float,
    dividend_yield: float = 0.0,
    loan_rate: float | None = None,
    inflation: float = 0.02,
) -> list[dict[str, Any]]:
    """After-tax value (real euros) of saving `monthly` for `years` and selling at the end, per destination.
    Each year's savings form their own lot, so only lots held 10 years get the 40 % deemed cost."""
    n, contrib = max(1, years), monthly * 12
    inv = contrib * n

    def acc_lots(amounts: list[float]) -> list[tuple[float, float, int]]:
        # contribution at the end of year k grows for the remaining n-1-k years
        return [(c, c * (1 + real_return) ** (n - 1 - k), n - 1 - k) for k, c in enumerate(amounts) if c > 0]

    out: list[dict[str, Any]] = []
    # 1. osakesäästötili until the deposit cap, the rest into an accumulating ETF
    room, to_ost, overflow = ost_room, [], []
    for _ in range(n):
        d = min(contrib, max(0.0, room))
        room -= d
        to_ost.append(d)
        overflow.append(contrib - d)
    ost_value = sum(v for _, v, _ in acc_lots(to_ost))
    ost_gain = max(0.0, ost_value - sum(to_ost))
    of = acc_lots(overflow)
    of_value = sum(v for _, v, _ in of)
    total_income = ost_gain + _lots_gain(of, t)
    out.append(
        {
            "destination": "Osakesäästötili",
            "value": round(ost_value + of_value - t.tax(total_income)),
            "invested": round(inv),
            "note": "gain share taxed at withdrawal"
            + (
                f" · {round(sum(overflow))} € over the deposit cap goes to the book-entry account"
                if sum(overflow)
                else ""
            ),
        }
    )
    # 2. accumulating ETF on the book-entry account
    lots = acc_lots([contrib] * n)
    value = sum(v for _, v, _ in lots)
    out.append(
        {
            "destination": "Book-entry account, accumulating ETF",
            "value": round(value - t.tax(_lots_gain(lots, t))),
            "invested": round(inv),
            "note": "no tax until sold; 40 % deemed cost on lots held 10 years",
        }
    )
    # 3. dividend stocks: dividends taxed every year (85 % taxable), reinvested after tax as new lots
    if dividend_yield > 0:
        held: list[list[float]] = []  # [cost, value, age]
        net_share = 1 - 0.85 * t.rate_low
        for _ in range(n):
            div = 0.0
            for lot in held:
                div += lot[1] * dividend_yield
                lot[1] *= 1 + real_return - dividend_yield
                lot[2] += 1
            held.append([contrib + div * net_share, contrib + div * net_share, 0])
        dl = [(c, v, int(a)) for c, v, a in held]
        dv = sum(v for _, v, _ in dl)
        out.append(
            {
                "destination": "Book-entry account, dividend stocks",
                "value": round(dv - t.tax(_lots_gain(dl, t))),
                "invested": round(inv),
                "note": f"{dividend_yield:.1%} dividends taxed every year, reinvested after tax",
            }
        )
    # 4. extra repayment: a risk-free, tax-free return at the loan rate (housing loan interest is not deductible)
    if loan_rate is not None:
        real_loan = (1 + loan_rate) / (1 + inflation) - 1
        v = sum(contrib * (1 + real_loan) ** (n - 1 - k) for k in range(n))
        out.append(
            {
                "destination": "Extra mortgage repayment",
                "value": round(v),
                "invested": round(inv),
                "note": f"interest saved at {loan_rate:.2%} ({real_loan:.2%} after inflation), no risk, no tax",
            }
        )
    return sorted(out, key=lambda x: -float(x["value"]))


def plan_from_params(p: dict[str, Any]) -> Plan:
    return Plan(
        age_now=int(p["age_now"]),
        retire_age=int(p["retire_age"]),
        horizon_age=int(p.get("horizon_age", 95)),
        real_return=float(p["real_return"]),
        monthly_spending=float(p["monthly_spending"]),
        pension_monthly=float(p["pension_monthly"]),
        pension_age=int(p["pension_age"]),
        monthly_savings=float(p.get("monthly_savings", 0)),
        events=[e for e in p.get("events") or [] if "age" in e],
    )


def with_overrides(plan: Plan, **kw: Any) -> Plan:
    return replace(plan, **{k: v for k, v in kw.items() if v is not None})
