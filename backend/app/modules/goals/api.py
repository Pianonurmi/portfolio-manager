"""Goals (money targets with pace) and habits (check-ins with streaks)."""

import datetime as dt
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Entity, Person
from app.modules.goals import service
from app.modules.goals.models import CADENCES, GOAL_KINDS, Goal, GoalEntry, Habit, HabitCheckin
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.jobs import JobContext, job
from app.platform.registry import (
    SearchHit,
    attention_provider,
    calendar_provider,
    search_provider,
    timeline_provider,
)

router = APIRouter(prefix="/api/goals", tags=["goals"])
habits_router = APIRouter(prefix="/api/habits", tags=["habits"])
extra_routers = [habits_router]
CENT = Decimal("0.01")
ROUTE = "/life?tab=goals"


def _q(x: Decimal | None) -> Decimal | None:
    return None if x is None else x.quantize(CENT)


def _out(db: Session, g: Goal) -> dict[str, Any]:
    p = service.goal_progress(db, g)
    entries = list(db.scalars(select(GoalEntry).where(GoalEntry.goal_id == g.id).order_by(GoalEntry.date.desc())))
    return {
        "id": g.id,
        "title": g.title,
        "kind": g.kind,
        "entity_id": g.entity_id,
        "account_id": g.account_id,
        "target_amount": g.target_amount,
        "target_date": g.target_date,
        "start_date": g.start_date,
        "start_amount": g.start_amount,
        "reached_on": g.reached_on,
        "notes": g.notes,
        "current": _q(p.current),
        "pct": p.pct,
        "status": p.status,
        "required_monthly": _q(p.required_monthly),
        "actual_monthly": _q(p.actual_monthly),
        "projected_date": p.projected_date,
        "expected_now": _q(p.expected_now),
        "manual": g.kind in ("savings", "custom"),
        "entries": [{"id": e.id, "date": e.date, "amount": e.amount, "note": e.note} for e in entries[:50]],
    }


class GoalIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: str
    entity_id: int | None = None
    account_id: int | None = None
    target_amount: Decimal
    target_date: date | None = None
    start_date: date | None = None
    start_amount: Decimal = Decimal(0)
    notes: str | None = None


def _apply(db: Session, g: Goal, b: GoalIn) -> None:
    if b.kind not in GOAL_KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(sorted(GOAL_KINDS))}")
    if b.entity_id is not None and db.get(Entity, b.entity_id) is None:
        raise HTTPException(422, "Unknown person")
    if b.kind == "debt_payoff":
        a = db.get(Account, b.account_id) if b.account_id else None
        if a is None or not a.is_liability:
            raise HTTPException(422, "Choose the loan or credit account to pay off")
    elif b.account_id is not None:
        raise HTTPException(422, "Only a debt payoff goal is linked to an account")
    if b.target_date and b.start_date and b.target_date <= b.start_date:
        raise HTTPException(422, "The target date must be after the start date")
    g.title, g.kind, g.entity_id, g.account_id = b.title, b.kind, b.entity_id, b.account_id
    g.target_amount, g.target_date, g.notes = b.target_amount, b.target_date, b.notes
    g.start_date, g.start_amount = b.start_date or today(), b.start_amount
    if b.kind == "debt_payoff" and b.start_amount == 0:
        # default the starting debt to what is owed now, so progress is measured from today
        g.start_amount = service.current_amount(db, g, today())


@router.get("")
def list_goals(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        _out(db, g) for g in db.scalars(select(Goal).order_by(Goal.reached_on.is_not(None), Goal.target_date, Goal.id))
    ]


@router.post("", status_code=201)
def add(body: GoalIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    g = Goal()
    _apply(db, g, body)
    db.add(g)
    db.flush()
    service.stamp_reached(db)
    audit(db, user.username, "goal.add", "goal", g.id)
    return _out(db, g)


@router.put("/{gid}")
def update(gid: int, body: GoalIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    g = db.get(Goal, gid)
    if g is None:
        raise HTTPException(404, "Goal not found")
    _apply(db, g, body)
    g.reached_on = None  # re-evaluated: the target may have moved
    db.flush()
    service.stamp_reached(db)
    audit(db, user.username, "goal.update", "goal", g.id)
    return _out(db, g)


@router.delete("/{gid}", status_code=204)
def delete(gid: int, user: CurrentUser, db: Db) -> None:
    g = db.get(Goal, gid)
    if g:
        db.delete(g)
        audit(db, user.username, "goal.delete", "goal", gid)


class EntryIn(BaseModel):
    date: dt.date | None = None
    amount: Decimal
    note: str | None = None


@router.post("/{gid}/entries", status_code=201)
def add_entry(gid: int, body: EntryIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    g = db.get(Goal, gid)
    if g is None:
        raise HTTPException(404, "Goal not found")
    if g.kind not in ("savings", "custom"):
        raise HTTPException(422, "Progress for this goal is read from your accounts")
    db.add(GoalEntry(goal_id=gid, date=body.date or today(), amount=body.amount, note=body.note))
    db.flush()
    service.stamp_reached(db)
    return _out(db, g)


@router.delete("/{gid}/entries/{eid}", status_code=204)
def del_entry(gid: int, eid: int, _: CurrentUser, db: Db) -> None:
    e = db.get(GoalEntry, eid)
    if e and e.goal_id == gid:
        db.delete(e)


@job("goals.refresh", "Mark reached goals", cron="15 3 * * *")
def refresh(ctx: JobContext) -> None:
    ctx.summary = f"{service.stamp_reached(ctx.db)} goals reached"


# ---- habits -------------------------------------------------------------------------------------------------


class HabitIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    cadence: str = "daily"
    times_per_week: int = Field(default=1, ge=1, le=7)
    person_id: int | None = None
    active: bool = True


def _habit_out(db: Session, h: Habit) -> dict[str, Any]:
    on = today()
    days = service.habit_checkins(db, h.id)
    st = service.habit_stats(h, days, on, h.created_at.date())
    return {
        "id": h.id,
        "title": h.title,
        "cadence": h.cadence,
        "times_per_week": h.times_per_week,
        "person_id": h.person_id,
        "active": h.active,
        **{k: st[k] for k in ("current_streak", "best_streak", "rate_30d", "done_today", "done_this_week")},
        "warning": service.habit_warnings(h, st, on),
        "recent": sorted((d for d in days if d > on - timedelta(days=28)), reverse=True),
    }


def _check_habit(db: Session, b: HabitIn) -> None:
    if b.cadence not in CADENCES:
        raise HTTPException(422, "cadence must be daily or weekly")
    if b.person_id is not None and db.get(Person, b.person_id) is None:
        raise HTTPException(422, "Unknown person")


@habits_router.get("")
def list_habits(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [_habit_out(db, h) for h in db.scalars(select(Habit).order_by(Habit.active.desc(), Habit.title))]


@habits_router.post("", status_code=201)
def add_habit(body: HabitIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    _check_habit(db, body)
    h = Habit(**body.model_dump())
    db.add(h)
    db.flush()
    audit(db, user.username, "habit.add", "habit", h.id)
    return _habit_out(db, h)


@habits_router.put("/{hid}")
def update_habit(hid: int, body: HabitIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    h = db.get(Habit, hid)
    if h is None:
        raise HTTPException(404, "Habit not found")
    _check_habit(db, body)
    for k, v in body.model_dump().items():
        setattr(h, k, v)
    return _habit_out(db, h)


@habits_router.delete("/{hid}", status_code=204)
def delete_habit(hid: int, _: CurrentUser, db: Db) -> None:
    h = db.get(Habit, hid)
    if h:
        db.delete(h)


class CheckinIn(BaseModel):
    date: dt.date | None = None


@habits_router.post("/{hid}/checkins", status_code=201)
def checkin(hid: int, body: CheckinIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    h = db.get(Habit, hid)
    if h is None:
        raise HTTPException(404, "Habit not found")
    d = body.date or today()
    if d > today():
        raise HTTPException(422, "Check-ins cannot be in the future")
    if d not in service.habit_checkins(db, hid):  # idempotent
        db.add(HabitCheckin(habit_id=hid, date=d))
        db.flush()
    return _habit_out(db, h)


@habits_router.delete("/{hid}/checkins/{d}")
def uncheck(hid: int, d: date, _: CurrentUser, db: Db) -> dict[str, Any]:
    h = db.get(Habit, hid)
    if h is None:
        raise HTTPException(404, "Habit not found")
    for c in db.scalars(select(HabitCheckin).where(HabitCheckin.habit_id == hid, HabitCheckin.date == d)):
        db.delete(c)
    db.flush()
    return _habit_out(db, h)


# ---- registrations ------------------------------------------------------------------------------------------


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    hits = [
        SearchHit(kind="goal", id=str(g.id), title=g.title, subtitle=f"{g.kind} goal", route=ROUTE)
        for g in db.scalars(select(Goal).where(Goal.title.ilike(f"%{q}%")).limit(limit))
    ]
    hits += [
        SearchHit(kind="habit", id=str(h.id), title=h.title, subtitle=f"{h.cadence} habit", route=ROUTE)
        for h in db.scalars(select(Habit).where(Habit.title.ilike(f"%{q}%")).limit(limit))
    ]
    return hits[:limit]


@timeline_provider
def _timeline(db: Session, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in db.scalars(select(Goal).order_by(Goal.id.desc()).limit(limit)):
        out.append(
            {
                "date": g.created_at.date(),
                "kind": "goal.created",
                "title": f"Goal set: {g.title}",
                "ref": None,
                "route": ROUTE,
            }
        )
        if g.reached_on:
            out.append(
                {
                    "date": g.reached_on,
                    "kind": "goal.reached",
                    "title": f"Goal reached: {g.title}",
                    "ref": None,
                    "route": ROUTE,
                }
            )
    return out


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    on = today()
    for g in db.scalars(select(Goal).where(Goal.reached_on.is_(None))):
        p = service.goal_progress(db, g, on)
        if p.status == "behind":
            late = g.target_date is not None and g.target_date < on
            out.append(
                {
                    "severity": "warn",
                    "title": f"Goal behind: {g.title}",
                    "detail": f"{p.pct} % done"
                    + (" · target date has passed" if late else " · below the straight line to the target"),
                    "route": ROUTE,
                    "chip": "goal",
                }
            )
    for h in db.scalars(select(Habit).where(Habit.active.is_(True))):
        st = service.habit_stats(h, service.habit_checkins(db, h.id), on, h.created_at.date())
        w = service.habit_warnings(h, st, on)
        if w:
            out.append(
                {
                    "severity": "info",
                    "title": f"Streak {'broken' if w == 'broken' else 'at risk'}: {h.title}",
                    "detail": f"best {st['best_streak']} " + ("weeks" if h.cadence == "weekly" else "days"),
                    "route": ROUTE,
                    "chip": "habit",
                }
            )
    return out


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {
            "date": g.target_date,
            "title": f"Goal target: {g.title}",
            "source": "goals",
            "category": "finance",
            "route": ROUTE,
        }
        for g in db.scalars(
            select(Goal).where(Goal.target_date >= start, Goal.target_date <= end, Goal.reached_on.is_(None))
        )
    ]
