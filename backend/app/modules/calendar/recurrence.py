"""Recurrence expansion. Monthly and yearly dates are always computed from the start date (never from the previous
occurrence), so a series on the 31st or on Feb 29 returns to its day when the month or year allows it."""

import calendar as stdlib_calendar
from collections.abc import Iterator
from datetime import date, timedelta


def add_months(d: date, months: int) -> date:
    """Same day of month `months` later; the last day of the month when that day does not exist."""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, stdlib_calendar.monthrange(y, m)[1]))


def occurrences(start: date, recurrence: str, lo: date, hi: date) -> Iterator[date]:
    """Dates of the series within [lo, hi]. A non-recurring item has just its start date."""
    if recurrence == "none":
        if lo <= start <= hi:
            yield start
        return
    if recurrence == "weekly":
        k = max(0, (lo - start).days // 7)
        while (d := start + timedelta(days=7 * k)) <= hi:
            if d >= lo:
                yield d
            k += 1
        return
    step = 1 if recurrence == "monthly" else 12 if recurrence == "yearly" else 0
    if not step:
        raise ValueError(f"unknown recurrence {recurrence!r}")
    k = max(0, ((lo.year - start.year) * 12 + lo.month - start.month) // step - 1)
    while (d := add_months(start, step * k)) <= hi:
        if d >= lo:
            yield d
        k += 1


def next_on_or_after(start: date, recurrence: str, on: date) -> date | None:
    """First occurrence on/after `on` (recurring series are unbounded)."""
    if recurrence == "none":
        return start if start >= on else None
    return next(occurrences(start, recurrence, on, on + timedelta(days=366 * 2)), None)


def rrule(start: date, recurrence: str) -> str | None:
    """RFC 5545 rule matching `occurrences`: days that a month lacks fall back to its last day."""
    if recurrence == "weekly":
        return "FREQ=WEEKLY"
    if recurrence == "monthly":
        if start.day <= 28:
            return "FREQ=MONTHLY"
        days = ",".join(str(x) for x in range(28, start.day + 1))
        return f"FREQ=MONTHLY;BYMONTHDAY={days};BYSETPOS=-1"
    if recurrence == "yearly":
        if (start.month, start.day) == (2, 29):
            return "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1"
        return "FREQ=YEARLY"
    return None
