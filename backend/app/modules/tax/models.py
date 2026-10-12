import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin


class Gift(IdMixin, TimestampMixin, Base):
    """Gift register for the 3-year gift-tax window (TAX-07, KIDS-03)."""

    __tablename__ = "gift"
    donor: Mapped[str] = mapped_column(String(128))  # free text: "Alex", "Grandparents (mother's side)"
    donor_entity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="SET NULL"))
    recipient_entity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"))
    date: Mapped[dt.date]
    amount: Mapped[Decimal]
    note: Mapped[str | None] = mapped_column(Text)
