"""Detect recurring payments in budget data: the same payee at a steady interval with a steady amount."""

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetAccount, BudgetTransaction
from app.modules.subscriptions.models import PERIODS, Subscription
from app.platform.db import today

TOLERANCE = {"weekly": 2, "monthly": 5, "quarterly": 10, "yearly": 20}


def normalise(payee: str) -> str:
    """'NETFLIX.COM 866-579' and 'Netflix.com' are the same payee."""
    s = payee.lower()
    s = re.sub(r"\d{3,}|[*#]+\S*", " ", s)
    s = re.sub(r"\b(oy|ab|oyj|inc|ltd|gmbh|com|www|fi)\b", " ", s)
    s = re.sub(r"[^a-zåäö0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def yearly(amount: Decimal, period: str) -> Decimal:
    return (amount * Decimal(365) / Decimal(PERIODS[period])).quantize(Decimal("0.01"))


def _period(gaps: list[int]) -> str | None:
    med = statistics.median(gaps)
    for name, days in PERIODS.items():
        if abs(med - days) <= TOLERANCE[name] and all(abs(g - days) <= TOLERANCE[name] * 2 for g in gaps):
            return name
    return None


def detect(db: Session, since_days: int = 400) -> list[dict[str, Any]]:
    budget_accounts = {a.id for a in db.scalars(select(BudgetAccount))}
    start = today() - timedelta(days=since_days)
    by_payee: dict[str, list[tuple[date, Decimal, str]]] = defaultdict(list)
    for t in db.scalars(
        select(BudgetTransaction).where(
            BudgetTransaction.deleted.is_(False),
            BudgetTransaction.date >= start,
            BudgetTransaction.amount < 0,
            BudgetTransaction.transfer_account_id.is_(None),
        )
    ):
        if t.account_id not in budget_accounts or not t.payee:
            continue
        by_payee[normalise(t.payee)].append((t.date, -t.amount, t.payee))
    out = []
    for key, rows in by_payee.items():
        if not key or len(rows) < 3:
            continue
        rows.sort()
        gaps = [(b[0] - a[0]).days for a, b in zip(rows, rows[1:], strict=False)]
        if min(gaps) < 4:  # several purchases a week: a shop, not a subscription
            continue
        period = _period(gaps)
        if period is None:
            continue
        amounts = [float(a) for _, a, _ in rows]
        recent = amounts[-3:]
        if statistics.pstdev(recent) > 0.15 * statistics.mean(recent):
            continue
        last_date, last_amount, raw = rows[-1]
        if (today() - last_date).days > PERIODS[period] * 2:  # stopped
            continue
        out.append(
            {
                "payee": key,
                "name": raw[:128],
                "period": period,
                "amount": last_amount,
                "previous_amount": rows[-2][1] if rows[-2][1] != last_amount else None,
                "last_seen": last_date,
                "next_due": last_date + timedelta(days=PERIODS[period]),
                "count": len(rows),
            }
        )
    return sorted(out, key=lambda x: -float(yearly(x["amount"], x["period"])))


def refresh(db: Session) -> dict[str, int]:
    """Add new candidates, update amounts, dates and price changes of known subscriptions."""
    known = {s.payee: s for s in db.scalars(select(Subscription).where(Subscription.payee.is_not(None)))}
    added = updated = 0
    for d in detect(db):
        s = known.get(d["payee"])
        if s is None:
            db.add(
                Subscription(
                    name=d["name"],
                    payee=d["payee"],
                    amount=d["amount"],
                    period=d["period"],
                    next_due=d["next_due"],
                    last_seen=d["last_seen"],
                    previous_amount=d["previous_amount"],
                    status="candidate",
                    source="detected",
                )
            )
            added += 1
            continue
        if s.status in ("ignored",):
            continue
        if s.last_seen is None or d["last_seen"] > s.last_seen:
            if d["amount"] != s.amount:
                s.previous_amount = s.amount
                s.amount = d["amount"]
            s.last_seen, s.next_due = d["last_seen"], d["next_due"]
            if s.status == "cancelled":  # it is still being charged
                s.status = "active"
                s.notes = ((s.notes or "") + f"\nCharged again on {d['last_seen']} after cancelling.").strip()
            updated += 1
    db.flush()
    return {"added": added, "updated": updated}
