"""iCalendar (RFC 5545) output for importing into a phone calendar. Times are floating local time."""

from datetime import date, datetime, timedelta
from hashlib import sha1
from typing import Any

from app.modules.calendar.models import CalendarEvent
from app.modules.calendar.recurrence import rrule
from app.platform.db import utcnow

PRODID = "-//Portfolio Manager//Calendar//EN"
DOMAIN = "portfolio-manager.local"


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _d(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fold(line: str) -> str:
    """Lines over 75 octets are folded with CRLF + space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    cur = ""
    for ch in line:
        if len((cur + ch).encode()) > (75 if not parts else 74):
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    return "\r\n ".join(parts)


def _alarm(days: int, title: str) -> list[str]:
    return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(title)}", f"TRIGGER:-P{days}D", "END:VALARM"]


def event_lines(e: CalendarEvent, stamp: str) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:event-{e.id}@{DOMAIN}", f"DTSTAMP:{stamp}", f"SUMMARY:{esc(e.title)}"]
    if e.all_day or e.start_time is None:
        last = e.end_date or e.start_date
        lines += [f"DTSTART;VALUE=DATE:{_d(e.start_date)}", f"DTEND;VALUE=DATE:{_d(last + timedelta(days=1))}"]
    else:
        lines.append(f"DTSTART:{_d(e.start_date)}T{e.start_time:%H%M%S}")
        end_d = e.end_date or e.start_date
        if e.end_time is not None:
            lines.append(f"DTEND:{_d(end_d)}T{e.end_time:%H%M%S}")
        elif e.end_date:
            lines.append(f"DTEND:{_d(end_d)}T{e.start_time:%H%M%S}")
    rule = rrule(e.start_date, e.recurrence)
    if rule:
        lines.append(f"RRULE:{rule}")
    if e.notes:
        lines.append(f"DESCRIPTION:{esc(e.notes)}")
    lines.append(f"CATEGORIES:{esc(e.category.upper())}")
    if e.reminder_days_before is not None:
        lines += _alarm(e.reminder_days_before, e.title)
    lines.append("END:VEVENT")
    return lines


def item_lines(item: dict[str, Any], stamp: str) -> list[str]:
    """An aggregated item from another module: a single all-day VEVENT."""
    key = f"{item['source']}|{item['date']}|{item['title']}"
    uid = sha1(key.encode()).hexdigest()[:20]  # noqa: S324  identifier, not security
    d: date = item["date"]
    return [
        "BEGIN:VEVENT",
        f"UID:{item['source']}-{uid}@{DOMAIN}",
        f"DTSTAMP:{stamp}",
        f"SUMMARY:{esc(item['title'])}",
        f"DTSTART;VALUE=DATE:{_d(d)}",
        f"DTEND;VALUE=DATE:{_d(d + timedelta(days=1))}",
        f"CATEGORIES:{esc(str(item['category']).upper())}",
        "END:VEVENT",
    ]


def render(events: list[CalendarEvent], items: list[dict[str, Any]], now: datetime | None = None) -> str:
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Portfolio Manager",
    ]
    for e in events:
        lines += event_lines(e, stamp)
    for it in items:
        lines += item_lines(it, stamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
