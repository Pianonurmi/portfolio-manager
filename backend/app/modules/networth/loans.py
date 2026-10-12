"""Loan schedules (RE-04, RE-06): annuity, equal principal (tasalyhennys) and fixed payment (kiinteä tasaerä)."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


@dataclass
class Payment:
    date: date
    payment: Decimal
    interest: Decimal
    principal: Decimal
    balance: Decimal


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    import calendar

    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month) - (1 if b.day < a.day else 0)


def schedule(
    balance: Decimal,
    annual_rate: Decimal,
    start: date,
    end: date,
    kind: str = "annuity",
    fixed_payment: Decimal | None = None,
    max_months: int = 600,
) -> list[Payment]:
    """Monthly schedule from `start` (first payment one month later) until paid or `end`."""
    n = max(1, months_between(start, end))
    r = annual_rate / 12
    principal_part = Decimal(0)
    pay = Decimal(0)
    out: list[Payment] = []
    bal = balance
    if kind == "annuity":
        pay = (bal * r / (1 - (1 + r) ** -n)) if r else bal / n
        pay = pay.quantize(CENT, ROUND_HALF_UP)
    elif kind == "equal_principal":
        principal_part = (bal / n).quantize(CENT, ROUND_HALF_UP)
    horizon = max_months if kind == "fixed" else min(n, max_months)
    for i in range(1, horizon + 1):
        if bal <= 0:
            break
        d = add_months(start, i)
        interest = (bal * r).quantize(CENT, ROUND_HALF_UP)
        if kind == "annuity":
            principal = pay - interest
        elif kind == "equal_principal":
            principal = principal_part
        else:  # fixed payment; term stretches when rates rise
            principal = (fixed_payment or Decimal(0)) - interest
            if principal <= 0:
                principal = Decimal(0)
        if (kind != "fixed" and i == n) or principal > bal:
            principal = bal
        bal -= principal
        out.append(Payment(d, interest + principal, interest, principal, bal))
    return out


def loan_terms(meta: dict[str, Any]) -> tuple[Decimal, str, date | None, Decimal | None]:
    """(annual rate, schedule kind, end date, fixed payment) from account meta."""
    rate = Decimal(str(meta.get("rate") or "0"))
    if meta.get("reference_rate") is not None and meta.get("margin") is not None:
        rate = Decimal(str(meta["reference_rate"])) + Decimal(str(meta["margin"]))
    end = date.fromisoformat(meta["end_date"]) if meta.get("end_date") else None
    fixed = Decimal(str(meta["fixed_payment"])) if meta.get("fixed_payment") else None
    return rate, str(meta.get("payment_type") or "annuity"), end, fixed


def estimate_balance(last_balance: Decimal, last_date: date, on: date, meta: dict[str, Any]) -> Decimal | None:
    """Balance on `on` from the last known balance by applying the schedule. None if terms are missing."""
    rate, kind, end, fixed = loan_terms(meta)
    if end is None or on <= last_date:
        return None
    paid = [p for p in schedule(last_balance, rate, last_date, end, kind, fixed) if p.date <= on]
    return paid[-1].balance if paid else last_balance
