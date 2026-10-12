import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin


class Policy(IdMixin, TimestampMixin, Base):
    """Insurance register (INS-01)."""

    __tablename__ = "policy"
    kind: Mapped[str] = mapped_column(String(32))  # life | disability | home | travel | car | accident | other
    insurer: Mapped[str] = mapped_column(String(128))
    policy_number: Mapped[str | None] = mapped_column(String(64))
    insured_entity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="SET NULL"))
    coverage: Mapped[Decimal | None]
    premium_yearly: Mapped[Decimal | None]
    renewal_date: Mapped[dt.date | None]
    beneficiaries: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class EstateItem(IdMixin, TimestampMixin, Base):
    """'If something happens to me' entries: contacts, instructions, wishes (EST-01). Never credentials."""

    __tablename__ = "estate_item"
    section: Mapped[str] = mapped_column(String(32))  # contact | instruction | wish | location
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
