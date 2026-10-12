"""ETF screener universe: one row per ISIN, refreshed weekly from Morningstar's screener."""

import datetime as dt
from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin


class EtfListing(IdMixin, Base):
    __tablename__ = "etf_listing"
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    secid: Mapped[str | None] = mapped_column(String(32))  # preferred listing
    listings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # [{exchange, ticker, currency, secid}]
    provider: Mapped[str | None] = mapped_column(String(128), index=True)
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    global_category: Mapped[str | None] = mapped_column(String(128))
    ter: Mapped[float | None]  # fractions throughout (0.002 = 0.20 %)
    fund_size_eur: Mapped[float | None]
    inception: Mapped[dt.date | None]
    domicile: Mapped[str | None] = mapped_column(String(8))
    distribution: Mapped[str | None] = mapped_column(String(8))  # acc | dist
    hedged: Mapped[bool] = mapped_column(default=False)
    base_currency: Mapped[str | None] = mapped_column(String(8))
    equity_share: Mapped[float | None]
    duration: Mapped[float | None]
    credit_quality: Mapped[int | None]
    srri: Mapped[int | None]
    star_rating: Mapped[int | None]
    sustainability: Mapped[int | None]
    benchmark: Mapped[str | None] = mapped_column(String(255))
    style_box: Mapped[str | None] = mapped_column(String(32))
    returns: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {ytd, y1, y3, y5, y10}, annualised as given
    vol_3y: Mapped[float | None]
    sharpe_3y: Mapped[float | None]
    max_dd_3y: Mapped[float | None]
    yield_12m: Mapped[float | None]
    close_price: Mapped[float | None]
    close_currency: Mapped[str | None] = mapped_column(String(8))
    # broker -> how it sells the ETF, e.g. {"nordnet": {symbol, exchange, currency, monthly_saving, owners}}
    brokers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    seen_at: Mapped[dt.datetime]
    updated_at: Mapped[dt.datetime]
    __table_args__ = (Index("ix_etf_listing_name", "name"),)
