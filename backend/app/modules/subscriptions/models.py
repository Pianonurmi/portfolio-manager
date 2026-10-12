"""Recurring payments (LIFE-01: subscriptions), confirmed by the user or detected from budget transactions."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

PERIODS = {"weekly": 7, "monthly": 30, "quarterly": 91, "yearly": 365}


class Subscription(IdMixin, TimestampMixin, Base):
    __tablename__ = "subscription"
    name: Mapped[str] = mapped_column(String(128))
    payee: Mapped[str | None] = mapped_column(String(255), index=True)  # normalised budget payee it matches
    amount: Mapped[Decimal]  # per period, positive
    period: Mapped[str] = mapped_column(String(16), default="monthly")
    next_due: Mapped[dt.date | None]
    status: Mapped[str] = mapped_column(String(16), default="active")  # candidate | active | ignored | cancelled
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | detected
    category: Mapped[str | None] = mapped_column(String(64))  # streaming, software, insurance, gym, …
    last_seen: Mapped[dt.date | None]
    previous_amount: Mapped[Decimal | None]  # set when a price change is seen
    notes: Mapped[str | None] = mapped_column(Text)
