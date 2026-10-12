"""Health log (LIFE-06): measurements, targets and medications. Sensitive: never sent to the AI or the digest."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

# kind -> (label, unit)
KINDS = {
    "weight": ("Weight", "kg"),
    "resting_hr": ("Resting heart rate", "bpm"),
    "blood_pressure": ("Blood pressure", "mmHg"),
    "sleep": ("Sleep", "h"),
    "steps": ("Steps", "steps"),
    "custom": ("Custom", ""),
}
DIRECTIONS = {"below", "above", "range"}


class HealthMetric(IdMixin, TimestampMixin, Base):
    __tablename__ = "health_metric"
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]
    kind: Mapped[str] = mapped_column(String(16))
    series: Mapped[str] = mapped_column(String(80), index=True)  # the kind, or "custom:<name>"
    name: Mapped[str | None] = mapped_column(String(64))  # custom only
    unit: Mapped[str | None] = mapped_column(String(16))  # custom only
    value: Mapped[Decimal]  # systolic for blood pressure
    value2: Mapped[Decimal | None]  # diastolic
    note: Mapped[str | None] = mapped_column(Text)


class HealthTarget(IdMixin, TimestampMixin, Base):
    """Band for one series of one person: below (<= high), above (>= low) or range (low..high)."""

    __tablename__ = "health_target"
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="CASCADE"), index=True)
    series: Mapped[str] = mapped_column(String(80))
    direction: Mapped[str] = mapped_column(String(8))
    low: Mapped[Decimal | None]
    high: Mapped[Decimal | None]  # systolic limit for blood pressure
    high2: Mapped[Decimal | None]  # diastolic limit
    __table_args__ = (UniqueConstraint("person_id", "series"),)


class Medication(IdMixin, TimestampMixin, Base):
    __tablename__ = "medication"
    person_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    dose: Mapped[str | None] = mapped_column(String(64))
    schedule: Mapped[str | None] = mapped_column(String(128))  # free text, e.g. "morning with food"
    active: Mapped[bool] = mapped_column(default=True)
    refill_date: Mapped[dt.date | None]
    notes: Mapped[str | None] = mapped_column(Text)
