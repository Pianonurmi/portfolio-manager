"""Subscriptions: detected from the budget, confirmed by you; yearly cost, price changes and renewals."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.calendar.recurrence import occurrences
from app.modules.subscriptions import service
from app.modules.subscriptions.models import PERIODS, Subscription
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.jobs import JobContext, job
from app.platform.registry import SearchHit, attention_provider, calendar_provider, search_provider, timeline_provider

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])
CENT = Decimal("0.01")


def _out(s: Subscription) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "payee": s.payee,
        "amount": s.amount.quantize(CENT),
        "period": s.period,
        "yearly": service.yearly(s.amount, s.period),
        "next_due": s.next_due,
        "status": s.status,
        "source": s.source,
        "category": s.category,
        "last_seen": s.last_seen,
        "previous_amount": s.previous_amount.quantize(CENT) if s.previous_amount else None,
        "notes": s.notes,
    }


@router.get("")
def list_subscriptions(_: CurrentUser, db: Db) -> dict[str, Any]:
    subs = list(db.scalars(select(Subscription).order_by(Subscription.status, Subscription.name)))
    active = [s for s in subs if s.status == "active"]
    return {
        "items": [_out(s) for s in subs if s.status != "ignored"],
        "yearly_total": sum((service.yearly(s.amount, s.period) for s in active), Decimal(0)),
        "monthly_total": (sum((service.yearly(s.amount, s.period) for s in active), Decimal(0)) / 12).quantize(
            Decimal("0.01")
        ),
        "candidates": sum(1 for s in subs if s.status == "candidate"),
    }


class SubIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0)
    period: str = "monthly"
    next_due: date | None = None
    category: str | None = None
    notes: str | None = None
    status: str = "active"


@router.post("", status_code=201)
def add(body: SubIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if body.period not in PERIODS:
        raise HTTPException(422, f"period must be one of {', '.join(PERIODS)}")
    s = Subscription(**body.model_dump(), source="manual")
    db.add(s)
    db.flush()
    audit(db, user.username, "subscription.add", "subscription", s.id)
    return _out(s)


class SubPatch(BaseModel):
    name: str | None = None
    amount: Decimal | None = None
    period: str | None = None
    next_due: date | None = None
    status: str | None = None
    category: str | None = None
    notes: str | None = None


@router.patch("/{sid}")
def update(sid: int, body: SubPatch, user: CurrentUser, db: Db) -> dict[str, Any]:
    s = db.get(Subscription, sid)
    if s is None:
        raise HTTPException(404, "Subscription not found")
    patch = body.model_dump(exclude_unset=True)
    if "status" in patch and patch["status"] not in ("candidate", "active", "ignored", "cancelled"):
        raise HTTPException(422, "Unknown status")
    if "period" in patch and patch["period"] not in PERIODS:
        raise HTTPException(422, "Unknown period")
    for k, v in patch.items():
        setattr(s, k, v)
    if "amount" in patch:
        s.previous_amount = None
    audit(db, user.username, "subscription.update", "subscription", s.id, after={k: str(v) for k, v in patch.items()})
    return _out(s)


@router.delete("/{sid}", status_code=204)
def delete(sid: int, _: CurrentUser, db: Db) -> None:
    s = db.get(Subscription, sid)
    if s:
        db.delete(s)


@router.post("/detect")
def detect_now(_: CurrentUser, db: Db) -> dict[str, int]:
    return service.refresh(db)


@job("subscriptions.detect", "Find recurring payments in budget data", cron="30 6 * * *")
def detect_job(ctx: JobContext) -> None:
    r = service.refresh(ctx.db)
    ctx.summary = f"{r['added']} new candidates, {r['updated']} updated"


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    rows = db.scalars(
        select(Subscription).where(Subscription.name.ilike(f"%{q}%"), Subscription.status != "ignored").limit(limit)
    )
    return [
        SearchHit(
            kind="subscription",
            id=str(s.id),
            title=s.name,
            subtitle=f"{s.amount.quantize(CENT)} € {s.period}",
            route="/household",
        )
        for s in rows
    ]


@timeline_provider
def _timeline(db: Session, limit: int) -> list[dict[str, Any]]:
    horizon = today() + timedelta(days=60)
    return [
        {
            "date": s.next_due,
            "kind": "subscription.due",
            "title": f"{s.name} {s.amount.quantize(CENT)} €",
            "ref": None,
            "route": "/household",
        }
        for s in db.scalars(
            select(Subscription).where(Subscription.status == "active", Subscription.next_due <= horizon).limit(limit)
        )
        if s.next_due
    ]


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = today()
    n = db.scalar(select(Subscription.id).where(Subscription.status == "candidate").limit(1))
    if n:
        out.append(
            {
                "severity": "info",
                "title": "Recurring payments found",
                "detail": "Confirm or ignore them",
                "route": "/household",
                "chip": "review",
            }
        )
    for s in db.scalars(select(Subscription).where(Subscription.status == "active")):
        if s.previous_amount and s.amount > s.previous_amount:
            pct = (s.amount / s.previous_amount - 1) * 100
            out.append(
                {
                    "severity": "warn",
                    "title": f"Price went up: {s.name}",
                    "detail": f"{s.previous_amount.quantize(CENT)} € → {s.amount.quantize(CENT)} € (+{pct:.0f} %)",
                    "route": "/household",
                    "chip": "price",
                }
            )
        if s.period == "yearly" and s.next_due and t <= s.next_due <= t + timedelta(days=21):
            out.append(
                {
                    "severity": "warn",
                    "title": f"Renews soon: {s.name}",
                    "detail": f"{s.amount.quantize(CENT)} € on {s.next_due:%-d.%-m.%Y}",
                    "route": "/household",
                    "chip": "renewal",
                }
            )
    return out


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Active subscriptions: the next charge date and its repeats, one per period."""
    out = []
    step = {"monthly": ("monthly", 1), "quarterly": ("monthly", 3), "yearly": ("yearly", 1), "weekly": ("weekly", 1)}
    for s in db.scalars(
        select(Subscription).where(Subscription.status == "active", Subscription.next_due.is_not(None))
    ):
        assert s.next_due is not None
        rec, n = step[s.period]
        # quarterly = every third monthly step
        dates = [d for i, d in enumerate(occurrences(s.next_due, rec, s.next_due, end)) if i % n == 0]
        for d in dates:
            if d >= start:
                out.append(
                    {
                        "date": d,
                        "title": f"{s.name} {s.amount:.2f} €",
                        "source": "subscriptions",
                        "category": "finance",
                        "route": "/household",
                    }
                )
    return out
