"""Dated valuations for accounts that are not valued from the ledger (CORE-06, RE-01..04)."""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

METHODS = {"manual", "statement", "index", "purchase"}


class Valuation(IdMixin, TimestampMixin, Base):
    """Value (assets) or outstanding balance (liabilities, positive number) of a whole account on a date."""

    __tablename__ = "valuation"
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="CASCADE"))
    date: Mapped[dt.date]
    value: Mapped[Decimal]
    method: Mapped[str] = mapped_column(String(16), default="manual")
    note: Mapped[str | None] = mapped_column(Text)
    document_ref: Mapped[str | None] = mapped_column(String(64))  # e.g. paperless:1842
    __table_args__ = (Index("ix_valuation_account_date", "account_id", "date"),)


class IndexSeries(TimestampMixin, Base):
    """A cached external price series, e.g. Statistics Finland's dwelling index for an area (RE-03)."""

    __tablename__ = "index_series"
    key: Mapped[str] = mapped_column(String(96), primary_key=True)  # statfin:15it:091-2:3:02
    label: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(16))  # index | eur_m2
    points: Mapped[dict[str, Any]] = mapped_column(default=dict)  # period ("2025Q3") -> value
    trades: Mapped[dict[str, Any]] = mapped_column(default=dict)  # period -> number of trades (postal series)
    fetched_at: Mapped[dt.datetime]
