"""Goal progress maths and habit streaks. Pure functions first; database readers below."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.goals.models import Goal, GoalEntry, Habit, HabitCheckin
from app.modules.networth.service import Sheet
from app.modules.portfolio.valuation import Scope, Valuer
from app.platform.db import today

ZERO = Decimal(0)
MONTH_DAYS = Decimal("30.4375")
TOLERANCE = Decimal("0.02")  # share of the way: this far under the straight line still counts as on track


@dataclass
class Progress:
    current: Decimal
    pct: Decimal  # 0..100, share of the way from start to target
    status: str  # on_track | behind | reached
    required_monthly: Decimal | None  # per month from now to hit the target on its date
    actual_monthly: Decimal | None  # per month since the start
    projected_date: date | None
    expected_now: Decimal | None  # where the straight line from start to target is today


def progress(
    start_date: date,
    start_amount: Decimal,
    target_amount: Decimal,
    target_date: date | None,
    current: Decimal,
    on: date,
) -> Progress:
    """Works in both directions: a debt payoff has a target below its start, and 'progress' means falling."""
    total = target_amount - start_amount
    done = current - start_amount
    reached = total == 0 or (current >= target_amount if total > 0 else current <= target_amount)
    share = Decimal(1) if reached else max(ZERO, min(Decimal(1), done / total))
    elapsed = (on - start_date).days
    actual = (done / Decimal(elapsed) * MONTH_DAYS) if elapsed > 0 else None
    required = None
    expected = None
    if target_date is not None:
        left = (target_date - on).days
        if left > 0:
            required = (target_amount - current) / (Decimal(left) / MONTH_DAYS)
        span = (target_date - start_date).days
        if span > 0:
            expected = start_amount + total * max(ZERO, min(Decimal(1), Decimal(elapsed) / Decimal(span)))
    projected = None
    if reached:
        status = "reached"
    else:
        if actual is not None and actual != 0 and (actual > 0) == (total > 0):
            days = (target_amount - current) / (actual / MONTH_DAYS)
            projected = on + timedelta(days=int(days) + 1) if days < 365 * 200 else None
        if target_date is None:
            status = "on_track"
        elif on > target_date:
            status = "behind"
        elif expected is None:
            status = "on_track"
        else:
            planned = abs((expected - start_amount) / total)
            status = "on_track" if share >= planned - TOLERANCE else "behind"
    return Progress(
        current=current,
        pct=(share * 100).quantize(Decimal("0.1")),
        status=status,
        required_monthly=required,
        actual_monthly=actual,
        projected_date=projected,
        expected_now=expected,
    )


def current_amount(db: Session, g: Goal, on: date) -> Decimal:
    scope = Scope(g.entity_id)
    if g.kind == "net_worth":
        return sum((i.value for i in Sheet(db, scope).items(on)), ZERO)
    if g.kind == "portfolio":
        return sum((h.value for h in Valuer(db, scope).holdings(on)), ZERO)
    if g.kind == "debt_payoff":
        debt = sum((-i.value for i in Sheet(db, scope).items(on) if i.account.id == g.account_id), ZERO)
        return debt
    entries = db.scalars(select(GoalEntry.amount).where(GoalEntry.goal_id == g.id, GoalEntry.date <= on))
    return g.start_amount + sum(entries, ZERO)


def goal_progress(db: Session, g: Goal, on: date | None = None) -> Progress:
    on = on or today()
    return progress(g.start_date, g.start_amount, g.target_amount, g.target_date, current_amount(db, g, on), on)


def stamp_reached(db: Session) -> int:
    """Remember the first day each goal was seen reached (the timeline's 'goal reached' event)."""
    n = 0
    for g in db.scalars(select(Goal).where(Goal.reached_on.is_(None))):
        if goal_progress(db, g).status == "reached":
            g.reached_on = today()
            n += 1
    return n


# ---- habits ----------------------------------------------------------------------------------------------------


def _runs(days: list[date], step: int) -> list[list[date]]:
    runs: list[list[date]] = []
    for d in sorted(set(days)):
        if runs and (d - runs[-1][-1]).days == step:
            runs[-1].append(d)
        else:
            runs.append([d])
    return runs


def _week(d: date) -> date:
    return d - timedelta(days=d.weekday())  # the Monday


def habit_stats(h: Habit, checkins: list[date], on: date, created: date | None = None) -> dict[str, Any]:
    """Current/best streak and the last-30-days completion rate.

    Daily: consecutive days; today not yet done does not break a streak that was alive yesterday.
    Weekly: consecutive Monday-Sunday weeks with at least `times_per_week` check-ins; the running week does not
    break the streak until it ends."""
    days = sorted(set(checkins))
    window_start = on - timedelta(days=29)
    first = min([x for x in (created, days[0] if days else None) if x] or [on])
    start = max(window_start, first)
    span = (on - start).days + 1
    in_window = [d for d in days if start <= d <= on]
    if h.cadence == "weekly":
        need = max(1, h.times_per_week)
        per_week: dict[date, int] = {}
        for d in days:
            per_week[_week(d)] = per_week.get(_week(d), 0) + 1
        ok = [w for w, n in per_week.items() if n >= need]
        runs = _runs(ok, 7)
        this_week = _week(on)
        cur = 0
        for r in runs:
            if r[-1] in (this_week, this_week - timedelta(days=7)):
                cur = len(r)
        expected = Decimal(need) * span / 7
        rate = min(Decimal(1), Decimal(len(in_window)) / expected) if expected > 0 else ZERO
        done_this_week = per_week.get(this_week, 0)
    else:
        runs = _runs(days, 1)
        cur = 0
        for r in runs:
            if r[-1] in (on, on - timedelta(days=1)):
                cur = len(r)
        rate = Decimal(len(in_window)) / span if span else ZERO
        done_this_week = len([d for d in days if d >= _week(on)])
    return {
        "current_streak": cur,
        "best_streak": max((len(r) for r in runs), default=0),
        "rate_30d": rate.quantize(Decimal("0.001")),
        "done_today": on in days,
        "done_this_week": done_this_week,
        "last_checkin": days[-1] if days else None,
        "previous_run": len(_runs(days, 1)[-1]) if days and h.cadence == "daily" else None,
    }


def habit_checkins(db: Session, habit_id: int) -> list[date]:
    return list(db.scalars(select(HabitCheckin.date).where(HabitCheckin.habit_id == habit_id)))


def habit_warnings(h: Habit, st: dict[str, Any], on: date) -> str | None:
    """'broken' when a streak of 3+ days ended in the last week; 'at_risk' when it ends if today is missed."""
    last = st["last_checkin"]
    if last is None or not h.active:
        return None
    if h.cadence == "daily":
        run = st["previous_run"] or 0
        gap = (on - last).days
        if run >= 3 and 2 <= gap <= 7:
            return "broken"
        if run >= 3 and gap == 1:
            return "at_risk"
        return None
    left = 6 - on.weekday()  # days after today in this Mon-Sun week
    missing = max(1, h.times_per_week) - st["done_this_week"]
    if st["current_streak"] > 0 and missing > left + 1:
        return "broken"
    if st["current_streak"] > 0 and missing > left:
        return "at_risk"
    return None
