"""Market interest rates (ECB statistics): one row per series and date, values as fractions."""

import datetime as dt

from sqlalchemy import Date, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin


class MarketRate(IdMixin, Base):
    __tablename__ = "market_rate"
    series: Mapped[str] = mapped_column(String(96))  # ECB dataset/key, e.g. FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA
    label: Mapped[str] = mapped_column(String(160))
    date: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[float]  # 0.0253 = 2.53 %
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("series", "date", name="uq_market_rate_series_date"),)
