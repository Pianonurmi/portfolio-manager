"""Goals and habits (LIFE-04): money goals read live from the balance sheet; habits are check-in logs."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

GOAL_KINDS = {"savings", "net_worth", "portfolio", "debt_payoff", "custom"}
CADENCES = {"daily", "weekly"}


class Goal(IdMixin, TimestampMixin, Base):
    __tablename__ = "goal"
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entity.id", ondelete="SET NULL")
    )  # None = household
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="SET NULL")
    )  # debt_payoff
    target_amount: Mapped[Decimal]
    target_date: Mapped[dt.date | None]
    start_date: Mapped[dt.date]
    start_amount: Mapped[Decimal] = mapped_column(default=Decimal(0))
    reached_on: Mapped[dt.date | None]  # stamped once, when the target is first seen reached
    notes: Mapped[str | None] = mapped_column(Text)


class GoalEntry(IdMixin, TimestampMixin, Base):
    """Manual progress for savings/custom goals: an amount added (negative = taken out) on a date."""

    __tablename__ = "goal_entry"
    goal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("goal.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]
    amount: Mapped[Decimal]
    note: Mapped[str | None] = mapped_column(Text)


class Habit(IdMixin, TimestampMixin, Base):
    __tablename__ = "habit"
    title: Mapped[str] = mapped_column(String(255))
    cadence: Mapped[str] = mapped_column(String(8), default="daily")
    times_per_week: Mapped[int] = mapped_column(default=1)  # weekly cadence: check-ins that make a week count
    person_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="SET NULL"))
    active: Mapped[bool] = mapped_column(default=True)


class HabitCheckin(IdMixin, TimestampMixin, Base):
    __tablename__ = "habit_checkin"
    habit_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("habit.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]
    __table_args__ = (UniqueConstraint("habit_id", "date"),)
