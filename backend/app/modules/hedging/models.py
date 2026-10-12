"""Nordnet Markets products usable as hedges (short mini futures, turbo shorts, bear certificates) on main indices."""

import datetime as dt

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin


class HedgeProduct(IdMixin, Base):
    __tablename__ = "hedge_product"
    isin: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # mini | turbo | bear
    direction: Mapped[str] = mapped_column(String(8))  # short | long
    index_key: Mapped[str] = mapped_column(String(16))  # SPX | NDX | DAX | SX5E | OMXS30 | OMXH25
    leverage: Mapped[float | None]  # bear certificates: the daily factor; minis/turbos: price / (price − level)
    financing_level: Mapped[float | None]
    barrier: Mapped[float | None]
    currency: Mapped[str | None] = mapped_column(String(8))
    issuer: Mapped[str | None] = mapped_column(String(64))
    nordnet_id: Mapped[int | None] = mapped_column(BigInteger)
    seen_at: Mapped[dt.datetime]
    __table_args__ = (Index("ix_hedge_product_index", "index_key", "kind"),)
