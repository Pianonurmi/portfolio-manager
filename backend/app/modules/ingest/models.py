"""Raw-first ingestion (ADR-06): every imported row is kept with a content hash and provenance."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

ROW_STATUSES = {"new", "duplicate", "needs_mapping", "needs_review", "ignored", "committed", "error"}


class ImportBatch(IdMixin, TimestampMixin, Base):
    __tablename__ = "import_batch"
    source: Mapped[str] = mapped_column(String(32))  # nordnet | ...
    adapter: Mapped[str] = mapped_column(String(64))  # e.g. nordnet-csv v1 (fi)
    filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="staged")  # staged | committed | discarded
    committed_at: Mapped[datetime | None]
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RawRecord(IdMixin, Base):
    __tablename__ = "raw_record"
    batch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("import_batch.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int]  # 1-based data row in the file (header excluded)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict[str, Any]]
    parsed: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("account.id"))
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (Index("ix_raw_record_source_ext", "source", "external_id"),)


class SourceMapping(IdMixin, TimestampMixin, Base):
    """Remembered user decisions per source: instrument keys, portfolios and transaction types."""

    __tablename__ = "source_mapping"
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # instrument | account | txn_type
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(String(64))  # instrument id | account id | ledger kind
    note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("source", "kind", "key"),)


class ReconciliationCheck(IdMixin, TimestampMixin, Base):
    __tablename__ = "reconciliation_check"
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("account.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_batch.id", ondelete="SET NULL"))
    as_of: Mapped[datetime]
    subject: Mapped[str] = mapped_column(String(128))  # instrument name or "cash EUR"
    instrument_id: Mapped[int | None] = mapped_column(BigInteger)
    reported: Mapped[str] = mapped_column(String(40))
    computed: Mapped[str] = mapped_column(String(40))
    ok: Mapped[bool]
    raw_record_id: Mapped[int | None] = mapped_column(BigInteger)
