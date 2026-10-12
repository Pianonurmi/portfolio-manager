"""EOD prices and FX rates (CORE-05, ING-04/05)."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, Quantity, Rate, TimestampMixin

# Lower number wins when several sources have a price for the same day.
SOURCE_PRIORITY = {"manual": 0, "statement": 5, "yahoo": 10, "nav": 20, "trade": 90}


class Price(IdMixin, TimestampMixin, Base):
    __tablename__ = "price"
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"))
    date: Mapped[dt.date]
    close: Mapped[Decimal] = mapped_column(Quantity)
    currency: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16))
    # set on every write, also when a re-download changes a close in place (the price snapshot keys on it)
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        UniqueConstraint("instrument_id", "date", "source"),
        Index("ix_price_inst_date", "instrument_id", "date"),
    )


class Distribution(IdMixin, TimestampMixin, Base):
    """A dividend or fund distribution per unit on its ex-date, in the amount the unit's price is quoted in."""

    __tablename__ = "distribution"
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"))
    ex_date: Mapped[dt.date]
    amount: Mapped[Decimal] = mapped_column(
        Quantity
    )  # per unit, as the shares traded at the ex-date (not split-adjusted)
    currency: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16))  # yahoo | ledger
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date", "source"),)


class FxRate(IdMixin, Base):
    """ECB reference rates: `rate` units of `quote` per 1 EUR."""

    __tablename__ = "fx_rate"
    quote: Mapped[str] = mapped_column(String(8))
    date: Mapped[dt.date]
    rate: Mapped[Decimal] = mapped_column(Rate)
    source: Mapped[str] = mapped_column(String(16), default="ecb")
    __table_args__ = (UniqueConstraint("quote", "date", "source"), Index("ix_fx_quote_date", "quote", "date"))


class CorporateAction(IdMixin, TimestampMixin, Base):
    """Splits reported by price providers; compared against the ledger to catch missing split rows."""

    __tablename__ = "corporate_action"
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"))
    date: Mapped[dt.date]
    kind: Mapped[str] = mapped_column(String(16), default="split")
    ratio: Mapped[Decimal] = mapped_column(Rate)  # new shares per old share
    source: Mapped[str] = mapped_column(String(16))
    __table_args__ = (UniqueConstraint("instrument_id", "date", "kind", "source"),)
