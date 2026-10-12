"""Budget data mirrored from YNAB or Actual (CF-01, CF-04). Amounts are exact decimals in euros."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, TimestampMixin


class BudgetAccount(TimestampMixin, Base):
    __tablename__ = "budget_account"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # YNAB account id
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))
    on_budget: Mapped[bool]
    closed: Mapped[bool]
    balance: Mapped[Decimal]
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="SET NULL")
    )  # pull balance into
    push_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="SET NULL")
    )  # push value from
    push_precision: Mapped[int] = mapped_column(default=100)  # round pushed values to this many euros


class BudgetCategory(TimestampMixin, Base):
    __tablename__ = "budget_category"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_name: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    hidden: Mapped[bool] = mapped_column(default=False)
    deleted: Mapped[bool] = mapped_column(default=False)
    is_income: Mapped[bool] = mapped_column(default=False, server_default="false")  # Actual: income categories


class BudgetTransaction(TimestampMixin, Base):
    __tablename__ = "budget_transaction"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[dt.date] = mapped_column(index=True)
    amount: Mapped[Decimal]
    account_id: Mapped[str] = mapped_column(String(64))
    category_id: Mapped[str | None] = mapped_column(String(64))
    payee: Mapped[str | None] = mapped_column(String(255))
    memo: Mapped[str | None] = mapped_column(Text)
    transfer_account_id: Mapped[str | None] = mapped_column(String(64))
    parent_id: Mapped[str | None] = mapped_column(String(64))  # subtransaction of a split
    deleted: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (Index("ix_budget_txn_cat_date", "category_id", "date"),)
