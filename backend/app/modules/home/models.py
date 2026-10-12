"""Home maintenance log (LIFE-01): work done, recurring tasks, and renovations that add to acquisition cost."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

KINDS = {"renovation", "repair", "service", "inspection", "purchase", "task"}


class MaintenanceItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "maintenance_item"
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="repair")
    done_on: Mapped[dt.date | None]
    cost: Mapped[Decimal | None]
    # perusparannus: an improvement paid by the owner raises the acquisition cost used when the home is sold
    adds_to_cost: Mapped[bool] = mapped_column(default=False)
    interval_months: Mapped[int | None]  # recurring task, e.g. 12 for filters, 60 for a chimney inspection
    next_due: Mapped[dt.date | None]
    contractor: Mapped[str | None] = mapped_column(String(128))
    warranty_until: Mapped[dt.date | None]
    notes: Mapped[str | None] = mapped_column(Text)
