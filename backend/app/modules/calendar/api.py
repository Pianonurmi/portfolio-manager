"""Calendar: own events plus dates other modules contribute; .ics export; reminders as attention items."""

import datetime as dt
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.calendar import ics, service
from app.modules.calendar.models import CATEGORIES, RECURRENCES, CalendarEvent
from app.modules.core.models import Person
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.registry import SearchHit, attention_provider, search_provider, timeline_provider

router = APIRouter(tags=["calendar"])
MAX_RANGE_DAYS = 800


def _out(e: CalendarEvent) -> dict[str, Any]:
    return {
        c: getattr(e, c)
        for c in (
            "id",
            "title",
            "start_date",
            "start_time",
            "end_date",
            "end_time",
            "all_day",
            "recurrence",
            "category",
            "person_id",
            "notes",
            "reminder_days_before",
        )
    }


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    start_date: date
    start_time: dt.time | None = None
    end_date: date | None = None
    end_time: dt.time | None = None
    all_day: bool = True
    recurrence: str = "none"
    category: str = "other"
    person_id: int | None = None
    notes: str | None = None
    reminder_days_before: int | None = Field(default=None, ge=0, le=365)

    @model_validator(mode="after")
    def _check(self) -> "EventIn":
        if self.recurrence not in RECURRENCES:
            raise ValueError(f"recurrence must be one of {', '.join(RECURRENCES)}")
        if self.category not in CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("The end cannot be before the start")
        if self.all_day:
            self.start_time = self.end_time = None
        elif self.start_time is None:
            raise ValueError("A timed event needs a start time")
        elif (
            self.end_time and (self.end_date or self.start_date) == self.start_date and self.end_time < self.start_time
        ):
            raise ValueError("The end cannot be before the start")
        return self


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    lo = start or today()
    hi = end or lo + timedelta(days=60)
    if hi < lo or (hi - lo).days > MAX_RANGE_DAYS:
        raise HTTPException(422, f"Choose a range of at most {MAX_RANGE_DAYS} days")
    return lo, hi


@router.get("/api/calendar")
def agenda(
    _: CurrentUser,
    db: Db,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    category: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    lo, hi = _range(from_, to)
    items = service.aggregate(db, lo, hi)
    return [i for i in items if (not category or i["category"] == category) and (not source or i["source"] == source)]


@router.get("/api/calendar.ics")
def export_ics(
    _: CurrentUser,
    db: Db,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    include: str = "own",
) -> Response:
    """Own events (recurring ones as RRULEs, so the phone repeats them); `include=all` adds other modules' dates."""
    events = list(db.scalars(select(CalendarEvent).order_by(CalendarEvent.start_date, CalendarEvent.id)))
    if from_ or to:
        lo, hi = from_ or date.min, to or date.max
        events = [
            e for e in events if e.start_date <= hi and (e.recurrence != "none" or (e.end_date or e.start_date) >= lo)
        ]
    items: list[dict[str, Any]] = []
    if include == "all":
        lo2, hi2 = _range(from_, to)
        items = [i for i in service.aggregate(db, lo2, hi2) if i["source"] != "calendar"]
    return Response(
        ics.render(events, items),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="portfolio-manager.ics"'},
    )


@router.get("/api/calendar/events")
def list_events(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [_out(e) for e in db.scalars(select(CalendarEvent).order_by(CalendarEvent.start_date, CalendarEvent.id))]


def _check_person(db: Session, pid: int | None) -> None:
    if pid is not None and db.get(Person, pid) is None:
        raise HTTPException(422, "Unknown person")


@router.post("/api/calendar/events", status_code=201)
def add(body: EventIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    _check_person(db, body.person_id)
    e = CalendarEvent(**body.model_dump())
    db.add(e)
    db.flush()
    audit(db, user.username, "calendar.add", "calendar_event", e.id)
    return _out(e)


@router.put("/api/calendar/events/{eid}")
def update(eid: int, body: EventIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    e = db.get(CalendarEvent, eid)
    if e is None:
        raise HTTPException(404, "Event not found")
    _check_person(db, body.person_id)
    for k, v in body.model_dump().items():
        setattr(e, k, v)
    audit(db, user.username, "calendar.update", "calendar_event", e.id)
    return _out(e)


@router.delete("/api/calendar/events/{eid}", status_code=204)
def delete(eid: int, user: CurrentUser, db: Db) -> None:
    e = db.get(CalendarEvent, eid)
    if e:
        db.delete(e)
        audit(db, user.username, "calendar.delete", "calendar_event", eid)


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    return [
        SearchHit(
            kind="event",
            id=str(e.id),
            title=e.title,
            subtitle=f"{e.start_date:%-d.%-m.%Y}" if e.category != "health" else None,
            route=service.ROUTE,
        )
        for e in db.scalars(select(CalendarEvent).where(CalendarEvent.title.ilike(f"%{q}%")).limit(limit))
        if e.category != "health"
    ]


@timeline_provider
def _timeline(db: Session, limit: int) -> list[dict[str, Any]]:
    """Own one-off events of the last and next 30 days; health appointments stay out of the shared timeline."""
    t = today()
    return [
        {"date": i["date"], "kind": "calendar.event", "title": i["title"], "ref": None, "route": service.ROUTE}
        for i in service.aggregate(db, t - timedelta(days=30), t + timedelta(days=30), own_only=True)
        if not i["private"]
    ][:limit]


@attention_provider
def _attention(db: Session) -> list[dict[str, Any]]:
    t = today()
    out = []
    for e, nxt in service.upcoming_reminders(db, t):
        days = (nxt - t).days
        out.append(
            {
                "severity": "info",
                "title": f"Coming up: {e.title}",
                "detail": "today"
                if days == 0
                else f"{'tomorrow' if days == 1 else f'in {days} days'} · {nxt:%-d.%-m.%Y}",
                "route": service.ROUTE,
                "chip": "event",
                "private": e.category == "health",
            }
        )
    return out
