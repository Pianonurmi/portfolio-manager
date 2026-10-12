"""Health log: metrics per person, targets, medications. Sensitive data: attention items are flagged `private` so
they are left out of pushed notifications and the weekly digest, and nothing here reaches the AI context."""

import datetime as dt
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Person
from app.modules.health import service
from app.modules.health.models import DIRECTIONS, KINDS, HealthMetric, HealthTarget, Medication
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.registry import attention_provider, calendar_provider

router = APIRouter(prefix="/api/health", tags=["health"])
ROUTE = "/life?tab=health"


def _person(db: Session, pid: int) -> Person:
    p = db.get(Person, pid)
    if p is None:
        raise HTTPException(422, "Unknown person")
    return p


def _label(kind: str, name: str | None, unit: str | None) -> tuple[str, str]:
    if kind == "custom":
        return name or "Custom", unit or ""
    return KINDS[kind]


def _target_out(t: HealthTarget | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {"id": t.id, "direction": t.direction, "low": t.low, "high": t.high, "high2": t.high2}


@router.get("/overview")
def overview(_: CurrentUser, db: Db, person_id: int) -> dict[str, Any]:
    """Per series: latest reading, 30/90-day averages, status against the target and a year of points."""
    _person(db, person_id)
    on = today()
    rows = list(db.scalars(select(HealthMetric).where(HealthMetric.person_id == person_id)))
    targets = {t.series: t for t in db.scalars(select(HealthTarget).where(HealthTarget.person_id == person_id))}
    by: dict[str, list[HealthMetric]] = {}
    for r in rows:
        by.setdefault(r.series, []).append(r)
    series = []
    for key in sorted(set(by) | set(targets), key=lambda k: (list(KINDS).index(k.split(":")[0]), k)):
        rs = by.get(key, [])
        kind = "custom" if key.startswith("custom:") else key
        name = next((r.name for r in rs if r.name), key[7:] if kind == "custom" else None)
        unit = next((r.unit for r in rs if r.unit), None)
        label, u = _label(kind, name, unit)
        series.append(
            {
                "series": key,
                "kind": kind,
                "label": label,
                "unit": u,
                "target": _target_out(targets.get(key)),
                **service.series_summary(rs, targets.get(key), on),
            }
        )
    return {"person_id": person_id, "series": series}


class MetricIn(BaseModel):
    person_id: int
    date: dt.date | None = None
    kind: str
    name: str | None = Field(default=None, max_length=64)
    unit: str | None = Field(default=None, max_length=16)
    value: Decimal
    value2: Decimal | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "MetricIn":
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if self.kind == "custom" and not self.name:
            raise ValueError("A custom measurement needs a name")
        if self.kind == "blood_pressure" and self.value2 is None:
            raise ValueError("Blood pressure needs systolic and diastolic")
        if self.kind != "blood_pressure":
            self.value2 = None
        if self.value < 0:
            raise ValueError("The value cannot be negative")
        return self


@router.post("/metrics", status_code=201)
def add_metric(body: MetricIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    _person(db, body.person_id)
    m = HealthMetric(
        **body.model_dump(exclude={"date"}), date=body.date or today(), series=service.series_key(body.kind, body.name)
    )
    if body.kind != "custom":
        m.name = m.unit = None
    db.add(m)
    db.flush()
    audit(db, user.username, "health.metric", "health_metric", m.id)
    return {"id": m.id, "series": m.series, "date": m.date}


@router.get("/metrics")
def list_metrics(
    _: CurrentUser, db: Db, person_id: int, series: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    q = select(HealthMetric).where(HealthMetric.person_id == person_id)
    if series:
        q = q.where(HealthMetric.series == series)
    return [
        {"id": m.id, "date": m.date, "series": m.series, "value": m.value, "value2": m.value2, "note": m.note}
        for m in db.scalars(q.order_by(HealthMetric.date.desc(), HealthMetric.id.desc()).limit(limit))
    ]


@router.delete("/metrics/{mid}", status_code=204)
def del_metric(mid: int, _: CurrentUser, db: Db) -> None:
    m = db.get(HealthMetric, mid)
    if m:
        db.delete(m)


class TargetIn(BaseModel):
    person_id: int
    kind: str
    name: str | None = None
    direction: str
    low: Decimal | None = None
    high: Decimal | None = None
    high2: Decimal | None = None

    @model_validator(mode="after")
    def _check(self) -> "TargetIn":
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if self.direction not in DIRECTIONS:
            raise ValueError("direction must be below, above or range")
        need = {"below": self.high, "above": self.low}.get(self.direction)
        if self.direction == "range" and (self.low is None or self.high is None or self.low > self.high):
            raise ValueError("A range needs low and high, low first")
        if self.direction != "range" and need is None:
            raise ValueError("Set the limit: high for below, low for above")
        return self


@router.put("/targets")
def set_target(body: TargetIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    _person(db, body.person_id)
    key = service.series_key(body.kind, body.name)
    t = db.scalar(select(HealthTarget).where(HealthTarget.person_id == body.person_id, HealthTarget.series == key))
    if t is None:
        t = HealthTarget(person_id=body.person_id, series=key, direction=body.direction)
        db.add(t)
    t.direction, t.low, t.high, t.high2 = body.direction, body.low, body.high, body.high2
    db.flush()
    return _target_out(t) or {}


@router.delete("/targets/{tid}", status_code=204)
def del_target(tid: int, _: CurrentUser, db: Db) -> None:
    t = db.get(HealthTarget, tid)
    if t:
        db.delete(t)


class MedIn(BaseModel):
    person_id: int
    name: str = Field(min_length=1, max_length=128)
    dose: str | None = Field(default=None, max_length=64)
    schedule: str | None = Field(default=None, max_length=128)
    active: bool = True
    refill_date: date | None = None
    notes: str | None = None


def _med_out(m: Medication) -> dict[str, Any]:
    return {c: getattr(m, c) for c in ("id", "person_id", "name", "dose", "schedule", "active", "refill_date", "notes")}


@router.get("/medications")
def list_meds(_: CurrentUser, db: Db, person_id: int | None = None) -> list[dict[str, Any]]:
    q = select(Medication).order_by(
        Medication.active.desc(), Medication.refill_date.is_(None), Medication.refill_date, Medication.name
    )
    if person_id is not None:
        q = q.where(Medication.person_id == person_id)
    return [_med_out(m) for m in db.scalars(q)]


@router.post("/medications", status_code=201)
def add_med(body: MedIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    _person(db, body.person_id)
    m = Medication(**body.model_dump())
    db.add(m)
    db.flush()
    audit(db, user.username, "health.medication", "medication", m.id)
    return _med_out(m)


@router.put("/medications/{mid}")
def update_med(mid: int, body: MedIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    m = db.get(Medication, mid)
    if m is None:
        raise HTTPException(404, "Medication not found")
    _person(db, body.person_id)
    for k, v in body.model_dump().items():
        setattr(m, k, v)
    return _med_out(m)


@router.delete("/medications/{mid}", status_code=204)
def del_med(mid: int, _: CurrentUser, db: Db) -> None:
    m = db.get(Medication, mid)
    if m:
        db.delete(m)


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    t = today()
    out = []
    for m, who in db.execute(
        select(Medication, Person.name)
        .join(Person, Person.id == Medication.person_id)
        .where(Medication.active.is_(True), Medication.refill_date <= t + timedelta(days=7))
    ):
        assert m.refill_date is not None
        out.append(
            {
                "severity": "warn" if m.refill_date < t else "info",
                "title": f"Refill: {m.name} ({who})",
                "detail": f"{'overdue since' if m.refill_date < t else 'due'} {m.refill_date:%-d.%-m.%Y}",
                "route": ROUTE,
                "chip": "refill",
                "private": True,
            }
        )
    return out


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {
            "date": m.refill_date,
            "title": f"Refill: {m.name}",
            "source": "health",
            "category": "health",
            "route": ROUTE,
            "private": True,
        }
        for m in db.scalars(
            select(Medication).where(
                Medication.active.is_(True), Medication.refill_date >= start, Medication.refill_date <= end
            )
        )
    ]
