"""Home maintenance: what was done and when, what is due next, warranties, and improvement costs for tax."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Account
from app.modules.home.models import KINDS, MaintenanceItem
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.registry import SearchHit, attention_provider, calendar_provider, search_provider, timeline_provider

router = APIRouter(prefix="/api/home", tags=["home"])


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError("invalid date")


def _out(x: MaintenanceItem) -> dict[str, Any]:
    return {
        c: getattr(x, c)
        for c in (
            "id",
            "account_id",
            "title",
            "kind",
            "done_on",
            "cost",
            "adds_to_cost",
            "interval_months",
            "next_due",
            "contractor",
            "warranty_until",
            "notes",
        )
    }


@router.get("")
def items(_: CurrentUser, db: Db, account_id: int | None = None) -> dict[str, Any]:
    q = select(MaintenanceItem).order_by(MaintenanceItem.done_on.desc().nulls_first(), MaintenanceItem.id.desc())
    if account_id is not None:
        q = q.where(MaintenanceItem.account_id == account_id)
    rows = list(db.scalars(q))
    improvements: dict[int, Decimal] = {}
    for x in rows:
        if x.adds_to_cost and x.cost and x.account_id:
            improvements[x.account_id] = improvements.get(x.account_id, Decimal(0)) + x.cost
    t = today()
    return {
        "items": [_out(x) for x in rows],
        "due": [_out(x) for x in rows if x.next_due and x.next_due <= t + timedelta(days=60)],
        "spent_12m": sum((x.cost or 0 for x in rows if x.done_on and x.done_on >= t - timedelta(days=365)), Decimal(0)),
        "improvements_by_property": {str(k): v for k, v in improvements.items()},
    }


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    account_id: int | None = None
    kind: str = "repair"
    done_on: date | None = None
    cost: Decimal | None = Field(default=None, ge=0)
    adds_to_cost: bool = False
    interval_months: int | None = Field(default=None, ge=1, le=600)
    next_due: date | None = None
    contractor: str | None = None
    warranty_until: date | None = None
    notes: str | None = None


def _apply(db: Session, x: MaintenanceItem, body: ItemIn) -> None:
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(sorted(KINDS))}")
    if body.account_id is not None:
        a = db.get(Account, body.account_id)
        if a is None or a.kind != "property":
            raise HTTPException(422, "Link a property account")
    for k, v in body.model_dump().items():
        setattr(x, k, v)
    if x.interval_months and x.done_on and not body.next_due:
        x.next_due = add_months(x.done_on, x.interval_months)


@router.post("", status_code=201)
def add(body: ItemIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    x = MaintenanceItem(title=body.title)
    _apply(db, x, body)
    db.add(x)
    db.flush()
    audit(db, user.username, "home.add", "maintenance_item", x.id)
    return _out(x)


@router.put("/{iid}")
def update(iid: int, body: ItemIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    x = db.get(MaintenanceItem, iid)
    if x is None:
        raise HTTPException(404, "Not found")
    _apply(db, x, body)
    audit(db, user.username, "home.update", "maintenance_item", x.id)
    return _out(x)


class DoneIn(BaseModel):
    done_on: date
    cost: Decimal | None = None
    note: str | None = None


@router.post("/{iid}/done", status_code=201)
def mark_done(iid: int, body: DoneIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Log a recurring task as done: a new history entry, and the task moves to its next due date."""
    x = db.get(MaintenanceItem, iid)
    if x is None:
        raise HTTPException(404, "Not found")
    done = MaintenanceItem(
        account_id=x.account_id,
        title=x.title,
        kind="service" if x.kind == "task" else x.kind,
        done_on=body.done_on,
        cost=body.cost,
        contractor=x.contractor,
        notes=body.note,
    )
    db.add(done)
    x.next_due = add_months(body.done_on, x.interval_months) if x.interval_months else None
    db.flush()
    audit(db, user.username, "home.done", "maintenance_item", x.id)
    return _out(x)


@router.delete("/{iid}", status_code=204)
def delete(iid: int, _: CurrentUser, db: Db) -> None:
    x = db.get(MaintenanceItem, iid)
    if x:
        db.delete(x)


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    rows = db.scalars(select(MaintenanceItem).where(MaintenanceItem.title.ilike(f"%{q}%")).limit(limit))
    return [
        SearchHit(
            kind="maintenance",
            id=str(x.id),
            title=x.title,
            subtitle=f"{x.kind} {x.done_on or x.next_due or ''}",
            route="/household",
        )
        for x in rows
    ]


@timeline_provider
def _timeline(db: Session, limit: int) -> list[dict[str, Any]]:
    out = []
    for x in db.scalars(select(MaintenanceItem).order_by(MaintenanceItem.id.desc()).limit(limit)):
        if x.done_on:
            out.append(
                {
                    "date": x.done_on,
                    "kind": f"home.{x.kind}",
                    "title": x.title + (f" {x.cost} €" if x.cost else ""),
                    "ref": None,
                    "route": "/household",
                }
            )
        if x.next_due:
            out.append(
                {"date": x.next_due, "kind": "home.due", "title": f"Due: {x.title}", "ref": None, "route": "/household"}
            )
    return out


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    t = today()
    out = []
    for x in db.scalars(select(MaintenanceItem).where(MaintenanceItem.next_due <= t + timedelta(days=14))):
        assert x.next_due is not None
        out.append(
            {
                "severity": "warn" if x.next_due < t else "info",
                "title": f"Home: {x.title}",
                "detail": f"{'overdue since' if x.next_due < t else 'due'} {x.next_due:%-d.%-m.%Y}",
                "route": "/household",
                "chip": "due",
            }
        )
    for x in db.scalars(
        select(MaintenanceItem).where(
            MaintenanceItem.warranty_until >= t, MaintenanceItem.warranty_until <= t + timedelta(days=30)
        )
    ):
        out.append(
            {
                "severity": "info",
                "title": f"Warranty ends: {x.title}",
                "detail": f"{x.warranty_until:%-d.%-m.%Y}",
                "route": "/household",
                "chip": "warranty",
            }
        )
    return out


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for x in db.scalars(
        select(MaintenanceItem).where(MaintenanceItem.next_due >= start, MaintenanceItem.next_due <= end)
    ):
        out.append(
            {
                "date": x.next_due,
                "title": f"Home: {x.title}",
                "source": "home",
                "category": "home",
                "route": "/household",
            }
        )
    for x in db.scalars(
        select(MaintenanceItem).where(MaintenanceItem.warranty_until >= start, MaintenanceItem.warranty_until <= end)
    ):
        out.append(
            {
                "date": x.warranty_until,
                "title": f"Warranty ends: {x.title}",
                "source": "home",
                "category": "home",
                "route": "/household",
            }
        )
    return out
