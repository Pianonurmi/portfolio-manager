"""Aggregated calendar: own events (recurrences expanded) plus dated items that other modules contribute."""

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.calendar.models import CalendarEvent
from app.modules.calendar.recurrence import next_on_or_after, occurrences
from app.platform.registry import CALENDAR_PROVIDERS

ROUTE = "/life?tab=calendar"


def event_occurrences(e: CalendarEvent, lo: date, hi: date) -> list[dict[str, Any]]:
    """Occurrences of one own event overlapping [lo, hi]; a multi-day event is shifted whole for each repeat."""
    span = (e.end_date - e.start_date).days if e.end_date else 0
    out = []
    for d in occurrences(e.start_date, e.recurrence, lo - timedelta(days=span), hi):
        out.append(
            {
                "date": d,
                "end_date": d + timedelta(days=span) if span else None,
                "time": e.start_time.strftime("%H:%M") if e.start_time and not e.all_day else None,
                "end_time": e.end_time.strftime("%H:%M") if e.end_time and not e.all_day else None,
                "all_day": e.all_day,
                "title": e.title,
                "source": "calendar",
                "category": e.category,
                "route": ROUTE,
                "event_id": e.id,
                "recurring": e.recurrence != "none",
                "person_id": e.person_id,
                "notes": e.notes,
                "private": e.category == "health",
            }
        )
    return out


def aggregate(db: Session, lo: date, hi: date, own_only: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in db.scalars(select(CalendarEvent)):
        out += event_occurrences(e, lo, hi)
    if not own_only:
        for provider in CALENDAR_PROVIDERS:
            for item in provider(db, lo, hi):
                out.append(
                    {
                        "all_day": True,
                        "time": None,
                        "end_date": None,
                        "end_time": None,
                        "event_id": None,
                        "recurring": False,
                        "person_id": None,
                        "notes": item.get("detail"),
                        "private": False,
                        **item,
                    }
                )
    return sorted(out, key=lambda x: (x["date"], x["time"] or "", x["title"]))


def upcoming_reminders(db: Session, on: date) -> list[tuple[CalendarEvent, date]]:
    """Own events whose next occurrence is within `reminder_days_before` days of `on` (or today)."""
    out = []
    for e in db.scalars(select(CalendarEvent).where(CalendarEvent.reminder_days_before.is_not(None))):
        assert e.reminder_days_before is not None
        nxt = next_on_or_after(e.start_date, e.recurrence, on)
        if nxt is not None and (nxt - on).days <= e.reminder_days_before:
            out.append((e, nxt))
    return out
