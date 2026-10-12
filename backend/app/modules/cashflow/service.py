"""Spending analytics from YNAB data (CF-03)."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetAccount, BudgetCategory, BudgetTransaction

ZERO = Decimal(0)
INCOME_NAMES = {"inflow: ready to assign", "inflow: to be budgeted", "ready to assign", "to be budgeted"}


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def monthly(db: Session, months: int = 12, until: date | None = None) -> dict[str, Any]:
    """Income, spending and savings per month; spending by category. Transfers between budget accounts are excluded."""
    cats = {c.id: c for c in db.scalars(select(BudgetCategory))}
    on_budget = {a.id for a in db.scalars(select(BudgetAccount).where(BudgetAccount.on_budget.is_(True)))}
    end = until or date.today()  # noqa: DTZ011
    y, m = end.year, end.month
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    start = date(int(keys[0][:4]), int(keys[0][5:]), 1)
    income: dict[str, Decimal] = defaultdict(Decimal)
    spend: dict[str, Decimal] = defaultdict(Decimal)
    by_cat: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for t in db.scalars(
        select(BudgetTransaction).where(
            BudgetTransaction.deleted.is_(False), BudgetTransaction.date >= start, BudgetTransaction.date <= end
        )
    ):
        if t.account_id not in on_budget:
            continue
        if t.transfer_account_id and t.transfer_account_id in on_budget:
            continue  # money moved between budget accounts
        k = month_key(t.date)
        if k not in keys:
            continue
        c = cats.get(t.category_id or "")
        if c is not None and (c.is_income or c.name.lower() in INCOME_NAMES):
            income[k] += t.amount
        elif t.amount < 0 or c is not None:
            spend[k] += -t.amount
            label = f"{c.group_name} · {c.name}" if c else "Uncategorised"
            by_cat[label][k] += -t.amount
    rows = [
        {
            "month": k,
            "income": income[k],
            "spending": spend[k],
            "saved": income[k] - spend[k],
            "rate": float((income[k] - spend[k]) / income[k]) if income[k] > 0 else None,
        }
        for k in keys
    ]
    full = rows[:-1] if len(rows) > 1 else rows  # current month is partial
    n = max(1, len(full))
    full_keys = keys[:-1] if len(keys) > 1 else keys
    avg_spend = sum((spend[k] for k in full_keys), ZERO) / n
    avg_income = sum((income[k] for k in full_keys), ZERO) / n
    cats_out = sorted(
        (
            {
                "category": k,
                "total": sum(v.values(), ZERO),
                "monthly_avg": sum(v.values(), ZERO) / n,
                "by_month": {m: v.get(m, ZERO) for m in keys},
            }
            for k, v in by_cat.items()
        ),
        key=lambda x: -sum(by_cat[str(x["category"])].values(), ZERO),
    )
    return {
        "months": rows,
        "avg_income": avg_income,
        "avg_spending": avg_spend,
        "savings_rate": float((avg_income - avg_spend) / avg_income) if avg_income > 0 else None,
        "categories": cats_out,
    }


def average_spending(db: Session, months: int = 12) -> Decimal | None:
    if db.scalar(select(BudgetTransaction.id).limit(1)) is None:
        return None
    s = monthly(db, months + 1)["avg_spending"]
    return s if s > 0 else None
