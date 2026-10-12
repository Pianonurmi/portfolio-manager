"""Two tax-and-financing tools: harvesting losses before the year ends, and borrowing against the home instead of
the portfolio.

Harvesting: Finland has no wash-sale rule, so a holding standing at a loss can be sold and bought straight back. The
realised loss is deducted from this year's capital income (or carried forward five years); the cost basis falls by
the same amount, so the tax comes back when the holding is finally sold, unless it is sold with the deemed
acquisition cost (hankintameno-olettama) or never sold. Rebuying restarts the ten-year clock of the 40 % deemed cost.

Home equity: an extra mortgage taken to invest is investment debt like a Nordnet credit, so its interest is deducted
from capital income too (the purpose decides, not the collateral). The mortgage has no margin call but has fixed
repayments and puts the home at stake; the Nordnet credit can be repaid at will but sells when the collateral falls
short.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.portfolio.valuation import Scope
from app.platform.rules import rules_for

ZERO = Decimal(0)


def harvest_plan(db: Session, scope: Scope, on: date, fee_pct: float = 0.002, fee_min: float = 9.0) -> dict[str, Any]:
    """Each open loss, what selling and rebuying it now saves this year after trading costs, and the catches."""
    from app.modules.tax.service import harvest_candidates, small_disposals, year_summary

    rules = rules_for(db, "tax", on.year)
    rate = float(rules["capital_income"]["brackets"][0]["rate"])
    ys = year_summary(db, scope, on.year)
    room = float(ys["capital_income"])  # what this year's losses can still be deducted from
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for c in harvest_candidates(db, scope, on):
        g = groups.setdefault(
            (c["instrument"], c["account"]),
            {"instrument": c["instrument"], "account": c["account"], "loss": 0.0, "value": 0.0, "lots": 0,
             "since": c["acquired"]},
        )  # fmt: skip
        g["loss"] += float(-c["unrealised"])
        g["value"] += float(c["value"])
        g["lots"] += 1
        g["since"] = min(g["since"], c["acquired"])
    rows = []
    left = room
    for g in sorted(groups.values(), key=lambda g: -g["loss"]):
        loss = g["loss"]
        now = min(loss, max(left, 0.0))
        left -= now
        cost = 2 * max(fee_min, fee_pct * g["value"])  # sell and buy back at the broker's fee level
        years = (on - g["since"]).days / 365.25
        rows.append(
            {
                **g,
                "since": g["since"].isoformat(),
                "years_held": round(years, 1),
                "deducted_now": now,
                "carried_forward": loss - now,
                "tax_saved_now": now * rate,
                "tax_saved_later": (loss - now) * rate,
                "trading_cost": cost,
                "net_now": now * rate - cost,
                "resets_deemed_cost_clock": 8 <= years < 10,
            }
        )
    ss_note = None
    exempt, losses_count = small_disposals(ys["sales"], rules)
    if not losses_count and ys["sales"]:
        ss_note = (
            "This year's sales are so small that their losses would not count (both sale prices and costs at most "
            f"{rules['capital_income']['small_disposals_loss_limit_total_cost']} €); a bigger sale changes that."
        )
    total = sum(r["tax_saved_now"] for r in rows)
    return {
        "year": on.year,
        "rate": rate,
        "capital_income_so_far": room,
        "gains_so_far": float(ys["gains"]),
        "losses_so_far": float(ys["losses"]),
        "carry_in": float(ys["loss_carry_in"]),
        "small_disposals_exempt": exempt,
        "candidates": rows,
        "tax_saved_now": total,
        "notes": [
            n
            for n in (
                "Finland has no wash-sale rule: you can sell at a loss and buy the same fund back at once.",
                "The tax saved now returns when the rebought holding is sold, unless it is then sold with the deemed "
                "acquisition cost or never sold; the gain is the time in between (and any 34 % → 30 % bracket "
                "difference).",
                "Rebuying restarts the holding period: after 10 years the deemed acquisition cost rises from 20 % to "
                "40 % of the sale price, so harvesting a holding close to 10 years old can cost more later.",
                "Only book-entry accounts count; sales inside an osakesäästötili are not taxed and not deductible.",
                ss_note,
                f"Sell before 31 December {on.year} for the loss to count this year.",
            )
            if n
        ],
    }


def annuity(principal: float, yearly_rate: float, years: float) -> float:
    """Monthly payment of an annuity loan."""
    n = max(int(round(years * 12)), 1)
    r = yearly_rate / 12
    return principal / n if abs(r) < 1e-12 else principal * r / (1 - (1 + r) ** -n)


def home_equity_compare(
    amount: float,
    nordnet_rate: float,
    nordnet_margin_call_drop: float | None,
    euribor_12m: float,
    mortgage_margin: float,
    years: float,
    home_value: float,
    mortgage_now: float,
    bank_ltv: float,
    deductible: float,
) -> dict[str, Any]:
    """Borrowing `amount` to invest: a mortgage top-up against the Nordnet credit."""
    m_rate = euribor_12m + mortgage_margin
    room = max(bank_ltv * home_value - mortgage_now, 0.0)
    pay = annuity(amount, m_rate, years)
    first_year_interest = amount * m_rate  # roughly: the balance falls during the year
    return {
        "amount": amount,
        "nordnet": {
            "rate": nordnet_rate,
            "rate_after_tax": nordnet_rate * (1 - deductible),
            "interest_after_tax": amount * nordnet_rate * (1 - deductible),
            "repayment": "flexible: any time, any amount",
            "margin_call_drop": nordnet_margin_call_drop,
            "risk": "sold automatically when the collateral value falls short; the sale realises gains",
        },
        "mortgage": {
            "rate": m_rate,
            "rate_after_tax": m_rate * (1 - deductible),
            "interest_after_tax": first_year_interest * (1 - deductible),
            "monthly_payment": pay,
            "years": years,
            "room": room,
            "fits": amount <= room + 1e-6,
            "loan_to_value_after": (mortgage_now + amount) / home_value if home_value > 0 else None,
            "risk": "no margin call; fixed payments each month, and the home is the collateral",
        },
        "cheaper": "mortgage" if m_rate < nordnet_rate else "nordnet",
        "notes": [
            "Interest on debt taken to invest is deducted from capital income whatever secures it; home-loan interest "
            "for living in the home is not (since 2023). Keep the purpose documented and tell the tax office.",
            "A mortgage top-up pays the debt down every month, so the leverage falls over time; the Nordnet credit "
            "stays until you repay it.",
            "Euribor moves: a 12-month Euribor mortgage resets once a year, the Nordnet rate whenever Nordnet changes "
            "its price list.",
        ],
    }
