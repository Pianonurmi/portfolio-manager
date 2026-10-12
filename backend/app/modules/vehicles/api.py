"""Vehicles: due dates (katsastus, insurance, vehicle tax), log with odometer, value estimate, cost per km."""

import datetime as dt
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetTransaction
from app.modules.core.models import Account
from app.modules.networth.models import Valuation
from app.modules.vehicles.models import LOG_KINDS, Vehicle, VehicleLog
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.registry import SearchHit, attention_provider, calendar_provider, search_provider, timeline_provider

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])
DUE_FIELDS = {"inspection_due": "Katsastus", "insurance_renewal": "Insurance renews", "tax_due": "Vehicle tax due"}


def estimate_value(v: Vehicle, on: date) -> Decimal | None:
    """Declining-balance estimate from the purchase; a guide, not a market price."""
    if not v.purchase_price or not v.purchase_date:
        return None
    years = Decimal((on - v.purchase_date).days) / Decimal("365.25")
    value: Decimal = v.purchase_price * (1 - v.depreciation_rate) ** years
    return value.quantize(Decimal(1))


def costs(db: Session, v: Vehicle, days: int = 365) -> dict[str, Any]:
    """Last 12 months: budget categories linked to the car plus logged costs; km from the odometer log."""
    start = today() - timedelta(days=days)
    budget = Decimal(0)
    if v.cost_categories:
        for t in db.scalars(
            select(BudgetTransaction).where(
                BudgetTransaction.deleted.is_(False),
                BudgetTransaction.category_id.in_(list(v.cost_categories)),
                BudgetTransaction.date >= start,
            )
        ):
            budget -= t.amount
    logs = list(db.scalars(select(VehicleLog).where(VehicleLog.vehicle_id == v.id).order_by(VehicleLog.date)))
    logged = sum((x.cost or Decimal(0) for x in logs if x.date >= start), Decimal(0))
    # logged costs are usually the same money as budget transactions: use the budget when it is linked
    total = budget if v.cost_categories else logged
    odo = [(x.date, x.odometer) for x in logs if x.odometer is not None]
    km = None
    if len(odo) >= 2:
        # from the last reading before the window (or the first one in it) to the latest reading
        before = [p for p in odo if p[0] <= start]
        first, last = (before[-1] if before else odo[0]), odo[-1]
        span = (last[0] - first[0]).days
        if span > 30 and last[1] > first[1]:
            km = int((last[1] - first[1]) * 365 / span)
    est = estimate_value(v, today())
    depreciation = (
        (estimate_value(v, start) or Decimal(0)) - (est or Decimal(0))
        if v.purchase_date and v.purchase_date < start
        else None
    )
    return {
        "running_costs_12m": total.quantize(Decimal("0.01")),
        "source": "budget categories" if v.cost_categories else "log",
        "km_per_year": km,
        "cost_per_km": (total / km).quantize(Decimal("0.01")) if km else None,
        "depreciation_12m": depreciation,
        "total_cost_per_km": ((total + (depreciation or 0)) / km).quantize(Decimal("0.01")) if km else None,
        "estimated_value": est,
        "odometer": odo[-1][1] if odo else None,
    }


def _out(db: Session, v: Vehicle) -> dict[str, Any]:
    return {
        **{
            c: getattr(v, c)
            for c in (
                "id",
                "account_id",
                "name",
                "registration",
                "model",
                "year",
                "purchase_date",
                "purchase_price",
                "depreciation_rate",
                "inspection_due",
                "insurance_renewal",
                "tax_due",
                "cost_categories",
                "sold_on",
                "notes",
            )
        },
        "costs": costs(db, v),
        "log": [
            {"id": x.id, "date": x.date, "kind": x.kind, "odometer": x.odometer, "cost": x.cost, "note": x.note}
            for x in db.scalars(
                select(VehicleLog).where(VehicleLog.vehicle_id == v.id).order_by(VehicleLog.date.desc()).limit(30)
            )
        ],
    }


@router.get("")
def list_vehicles(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [_out(db, v) for v in db.scalars(select(Vehicle).order_by(Vehicle.sold_on.is_not(None), Vehicle.name))]


class VehicleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    account_id: int | None = None
    registration: str | None = Field(default=None, max_length=16)
    model: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    purchase_date: date | None = None
    purchase_price: Decimal | None = Field(default=None, ge=0)
    depreciation_rate: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    inspection_due: date | None = None
    insurance_renewal: date | None = None
    tax_due: date | None = None
    cost_categories: dict[str, str] = {}
    sold_on: date | None = None
    notes: str | None = None


def _check_account(db: Session, account_id: int | None) -> None:
    if account_id is not None:
        a = db.get(Account, account_id)
        if a is None or a.kind != "vehicle":
            raise HTTPException(422, "Link a net-worth account of type Vehicle")


@router.post("", status_code=201)
def add(body: VehicleIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    _check_account(db, body.account_id)
    v = Vehicle(**body.model_dump())
    db.add(v)
    db.flush()
    audit(db, user.username, "vehicle.add", "vehicle", v.id)
    return _out(db, v)


@router.put("/{vid}")
def update(vid: int, body: VehicleIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    v = db.get(Vehicle, vid)
    if v is None:
        raise HTTPException(404, "Vehicle not found")
    _check_account(db, body.account_id)
    for k, val in body.model_dump().items():
        setattr(v, k, val)
    audit(db, user.username, "vehicle.update", "vehicle", v.id)
    return _out(db, v)


@router.delete("/{vid}", status_code=204)
def delete(vid: int, _: CurrentUser, db: Db) -> None:
    v = db.get(Vehicle, vid)
    if v:
        db.delete(v)


class LogIn(BaseModel):
    date: dt.date
    kind: str
    odometer: int | None = Field(default=None, ge=0)
    cost: Decimal | None = Field(default=None, ge=0)
    note: str | None = None
    next_inspection: dt.date | None = None  # convenience when logging a katsastus


@router.post("/{vid}/log", status_code=201)
def add_log(vid: int, body: LogIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    v = db.get(Vehicle, vid)
    if v is None:
        raise HTTPException(404, "Vehicle not found")
    if body.kind not in LOG_KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(sorted(LOG_KINDS))}")
    db.add(VehicleLog(vehicle_id=vid, **body.model_dump(exclude={"next_inspection"})))
    if body.kind == "inspection" and body.next_inspection:
        v.inspection_due = body.next_inspection
    db.flush()
    return _out(db, v)


@router.delete("/{vid}/log/{lid}", status_code=204)
def del_log(vid: int, lid: int, _: CurrentUser, db: Db) -> None:
    x = db.get(VehicleLog, lid)
    if x and x.vehicle_id == vid:
        db.delete(x)


@router.post("/{vid}/valuation", status_code=201)
def value_now(vid: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Book today's estimate as a valuation of the linked net-worth account."""
    v = db.get(Vehicle, vid)
    if v is None or v.account_id is None:
        raise HTTPException(422, "Link the vehicle to a net-worth account first")
    est = estimate_value(v, today())
    if est is None:
        raise HTTPException(422, "Add the purchase price and date first")
    val = Valuation(
        account_id=v.account_id,
        date=today(),
        value=est,
        method="manual",
        note=f"Estimate: {v.depreciation_rate:.0%} a year from {v.purchase_price} €",
    )
    db.add(val)
    db.flush()
    audit(db, user.username, "vehicle.valuation", "valuation", val.id)
    return {"value": est, "undo_id": make_undo(db, "valuation.delete", "Vehicle estimate", {"id": val.id})}


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    rows = db.scalars(
        select(Vehicle).where((Vehicle.name.ilike(f"%{q}%")) | (Vehicle.registration.ilike(f"%{q}%"))).limit(limit)
    )
    return [
        SearchHit(kind="vehicle", id=str(v.id), title=v.name, subtitle=v.registration, route="/household") for v in rows
    ]


@timeline_provider
def _timeline(db: Session, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for v in db.scalars(select(Vehicle).where(Vehicle.sold_on.is_(None))):
        for f, label in DUE_FIELDS.items():
            d = getattr(v, f)
            if d:
                out.append(
                    {
                        "date": d,
                        "kind": f"vehicle.{f}",
                        "title": f"{v.name}: {label}",
                        "ref": None,
                        "route": "/household",
                    }
                )
    for x, name in db.execute(
        select(VehicleLog, Vehicle.name).join(Vehicle).order_by(VehicleLog.date.desc()).limit(limit)
    ).all():
        if x.kind != "fuel":
            out.append(
                {
                    "date": x.date,
                    "kind": f"vehicle.{x.kind}",
                    "title": f"{name}: {x.kind}" + (f" {x.cost} €" if x.cost else ""),
                    "ref": None,
                    "route": "/household",
                }
            )
    return out


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    out = []
    t = today()
    for v in db.scalars(select(Vehicle).where(Vehicle.sold_on.is_(None))):
        for f, label in DUE_FIELDS.items():
            d = getattr(v, f)
            if d and d <= t + timedelta(days=30):
                out.append(
                    {
                        "severity": "bad" if d < t else "warn",
                        "title": f"{label}: {v.name}",
                        "detail": f"{'overdue since' if d < t else 'due'} {d:%-d.%-m.%Y}",
                        "route": "/household",
                        "chip": "due",
                    }
                )
    return out


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for v in db.scalars(select(Vehicle).where(Vehicle.sold_on.is_(None))):
        for f, label in DUE_FIELDS.items():
            d = getattr(v, f)
            if d and start <= d <= end:
                out.append(
                    {
                        "date": d,
                        "title": f"{v.name}: {label}",
                        "source": "vehicles",
                        "category": "home",
                        "route": "/household",
                    }
                )
    return out
