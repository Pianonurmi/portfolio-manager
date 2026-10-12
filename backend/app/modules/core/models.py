"""People, legal owners, institutions, accounts, ownership and instruments (CORE-01..03, CORE-06/07)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base, IdMixin, Share, TimestampMixin

# Account kinds are data, not schema: new kinds need no migration (CORE-02).
ACCOUNT_KINDS = {
    "aot": "Arvo-osuustili",
    "ost": "Osakesäästötili",
    "bank": "Bank account",
    "savings": "Savings account",
    "credit_card": "Credit card",
    "loan": "Loan",
    "mortgage": "Mortgage",
    "crypto_exchange": "Crypto exchange",
    "crypto_wallet": "Crypto wallet",
    "kapitalisaatio": "Kapitalisaatiosopimus",
    "ps": "PS-sopimus",
    "property": "Property (osakehuoneisto)",
    "housing_loan": "Housing company loan share",
    "vehicle": "Vehicle",
    "manual_asset": "Manual asset",
    "manual_liability": "Manual liability",
}
LIABILITY_KINDS = {"credit_card", "loan", "mortgage", "housing_loan", "manual_liability"}
CASH_KINDS = {"bank", "savings"}
PROPERTY_KINDS = {"property"}
INVESTMENT_KINDS = {"aot", "ost", "crypto_exchange", "crypto_wallet", "kapitalisaatio", "ps"}
INSTRUMENT_TYPES = {"stock", "etf", "fund", "bond", "crypto", "cash", "property", "other"}


class Person(IdMixin, TimestampMixin, Base):
    __tablename__ = "person"
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="adult")  # adult | child
    birth_date: Mapped[date | None]
    # full: all finances tracked; shared: only co-owned assets (e.g. spouse today)
    tracking: Mapped[str] = mapped_column(String(16), default="full")
    is_self: Mapped[bool] = mapped_column(default=False)


class Entity(IdMixin, TimestampMixin, Base):
    """A legal owner: a person, or later a company (CORE-07)."""

    __tablename__ = "entity"
    kind: Mapped[str] = mapped_column(String(16), default="person")  # person | company
    person_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("person.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    business_id: Mapped[str | None] = mapped_column(String(16))  # Y-tunnus for companies


class Institution(IdMixin, TimestampMixin, Base):
    __tablename__ = "institution"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="bank")  # bank | broker | exchange | lender | other
    country: Mapped[str | None] = mapped_column(String(2))


class Account(IdMixin, TimestampMixin, Base):
    __tablename__ = "account"
    institution_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("institution.id"))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    opened_on: Mapped[date | None]
    closed_on: Mapped[date | None]
    external_ref: Mapped[str | None] = mapped_column(String(128))  # e.g. Nordnet account number
    notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    institution: Mapped[Institution | None] = relationship(lazy="joined")

    @property
    def is_liability(self) -> bool:
        return self.kind in LIABILITY_KINDS


class Ownership(IdMixin, TimestampMixin, Base):
    """Dated ownership share of an account by an entity (CORE-01). Shares per date sum to 1."""

    __tablename__ = "ownership"
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("entity.id", ondelete="CASCADE"), index=True)
    share: Mapped[Decimal] = mapped_column(Share)
    valid_from: Mapped[date]
    valid_to: Mapped[date | None]
    __table_args__ = (CheckConstraint("share > 0 and share <= 1", name="share_range"),)


class Instrument(IdMixin, TimestampMixin, Base):
    __tablename__ = "instrument"
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    symbol: Mapped[str | None] = mapped_column(String(32))  # exchange ticker or crypto symbol
    mic: Mapped[str | None] = mapped_column(String(8))  # exchange MIC, e.g. XHEL
    currency: Mapped[str] = mapped_column(String(8))
    price_symbol: Mapped[str | None] = mapped_column(String(64))  # provider symbol, e.g. "SAMPO.HE"
    asset_class: Mapped[str | None] = mapped_column(String(32))  # equity | fixed_income | cash | crypto | ...
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (Index("ix_instrument_name", "name"), Index("ix_instrument_symbol", "symbol"))


class InstrumentProfile(IdMixin, Base):
    """What an instrument really holds, per source. Weights are fractions (0-1); sources merge per field."""

    __tablename__ = "instrument_profile"
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(16))  # manual | morningstar | factsheet | derived
    as_of: Mapped[date | None]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("instrument_id", "source"),)
