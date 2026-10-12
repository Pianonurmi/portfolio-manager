"""Own calendar events (LIFE-05). Dated items from other modules are not stored here; they come from providers."""

import datetime as dt

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

RECURRENCES = ("none", "weekly", "monthly", "yearly")
CATEGORIES = ("finance", "health", "home", "family", "other")


class CalendarEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "calendar_event"
    title: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[dt.date] = mapped_column(index=True)
    start_time: Mapped[dt.time | None]
    end_date: Mapped[dt.date | None]
    end_time: Mapped[dt.time | None]
    all_day: Mapped[bool] = mapped_column(default=True)
    recurrence: Mapped[str] = mapped_column(String(8), default="none")
    category: Mapped[str] = mapped_column(String(16), default="other")
    person_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    reminder_days_before: Mapped[int | None]
