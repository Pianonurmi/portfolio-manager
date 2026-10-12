"""Double-entry, multi-commodity ledger (CORE-04, ADR-05).

A transaction holds postings that sum to zero in `amount` (transaction currency). Postings with an
`account_id` change holdings (`quantity` of `instrument_id` in that account). Postings without an
account are the external side: income, fee, tax, external (money in/out of the tracked world).

Example: buy 10 NOKIA @ 4,00 with 1,00 fee
  account  NOKIA   qty +10      amount +40.00   role=asset
  account  EUR     qty −41      amount −41.00   role=asset
  —        —       qty 0        amount  +1.00   role=fee
"""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base, IdMixin, Quantity, Rate, TimestampMixin

TXN_KINDS = {
    "buy",
    "sell",
    "dividend",
    "interest",
    "fee",
    "tax",
    "tax_refund",
    "deposit",
    "withdrawal",
    "transfer_in",
    "transfer_out",
    "fx",
    "split",
    "security_in",
    "security_out",
    "capital_return",
    "reward",  # income received in kind (staking, airdrops): acquisition at market value
    "adjustment",
    "other",
}
ROLES = {"asset", "income", "fee", "tax", "external", "equity"}


class Transaction(IdMixin, TimestampMixin, Base):
    __tablename__ = "ledger_transaction"
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.id"), index=True)
    date: Mapped[dt.date] = mapped_column(index=True)  # trade date
    settle_date: Mapped[dt.date | None]
    kind: Mapped[str] = mapped_column(String(24))
    description: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), default="manual")  # nordnet | manual | ynab | ...
    external_id: Mapped[str | None] = mapped_column(String(128))
    seq: Mapped[int] = mapped_column(BigInteger, default=0)  # order within a day (source order)
    import_batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_batch.id"), index=True)
    raw_record_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("raw_record.id"))
    # The other half of a transfer between own accounts (security_out ↔ security_in): carries cost and dates
    link_id: Mapped[int | None] = mapped_column(BigInteger)
    voided_at: Mapped[dt.datetime | None]
    void_reason: Mapped[str | None] = mapped_column(String(255))
    postings: Mapped[list["Posting"]] = relationship(
        back_populates="txn", cascade="all, delete-orphan", lazy="selectin"
    )
    __table_args__ = (
        Index(
            "uq_ledger_txn_source_ext",
            "source",
            "external_id",
            unique=True,
            postgresql_where="external_id is not null and voided_at is null",
        ),
        Index("ix_ledger_txn_account_date", "account_id", "date", "seq"),
    )


class Posting(IdMixin, Base):
    __tablename__ = "ledger_posting"
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ledger_transaction.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("account.id"))
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal(0))
    amount: Mapped[Decimal]  # value in transaction currency; postings of a transaction sum to 0
    role: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal | None] = mapped_column(Quantity)  # unit price in price_currency
    price_currency: Mapped[str | None] = mapped_column(String(8))
    fx_rate: Mapped[Decimal | None] = mapped_column(Rate)  # transaction currency per 1 price currency
    txn: Mapped[Transaction] = relationship(back_populates="postings")
    __table_args__ = (Index("ix_ledger_posting_acct_inst", "account_id", "instrument_id"),)
