"""Vehicles (LIFE-01): details, due dates and a log; value and costs feed net worth and cash flow."""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

LOG_KINDS = {"fuel", "service", "repair", "inspection", "tyres", "other"}


class Vehicle(IdMixin, TimestampMixin, Base):
    __tablename__ = "vehicle"
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(128))
    registration: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(128))
    year: Mapped[int | None]
    purchase_date: Mapped[dt.date | None]
    purchase_price: Mapped[Decimal | None]
    depreciation_rate: Mapped[Decimal] = mapped_column(default=Decimal("0.15"))  # per year, declining balance
    inspection_due: Mapped[dt.date | None]  # katsastus
    insurance_renewal: Mapped[dt.date | None]
    tax_due: Mapped[dt.date | None]  # ajoneuvovero
    cost_categories: Mapped[dict[str, Any]] = mapped_column(default=dict)  # budget category id -> label
    sold_on: Mapped[dt.date | None]
    notes: Mapped[str | None] = mapped_column(Text)


class VehicleLog(IdMixin, TimestampMixin, Base):
    __tablename__ = "vehicle_log"
    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("vehicle.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]
    kind: Mapped[str] = mapped_column(String(16))
    odometer: Mapped[int | None]  # km
    cost: Mapped[Decimal | None]
    note: Mapped[str | None] = mapped_column(Text)
