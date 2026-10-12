"""Values extracted from documents by the local LLM, staged for a human to confirm (ING-09, AI-03, DOC-03)."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

EXTRACTION_KINDS = {"statement", "loan_letter", "pension_record"}


class Extraction(IdMixin, TimestampMixin, Base):
    __tablename__ = "extraction"
    source: Mapped[str] = mapped_column(String(16), default="paperless", server_default="paperless")  # | upload
    paperless_id: Mapped[int | None]
    filename: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str | None] = mapped_column(String(64))  # original kept under imports_dir/ai/
    title: Mapped[str] = mapped_column(String(255))
    # holdings | statement | loan_letter | pension_record | factsheet | transactions | other | unknown
    kind: Mapped[str] = mapped_column(String(24))
    # queued | processing | review | accepted | rejected | error
    status: Mapped[str] = mapped_column(String(16), default="review")
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")  # header, rows, checks, notes
    progress: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")  # {"done", "total"}
    fields: Mapped[dict[str, Any]] = mapped_column(default=dict)  # name -> {value, quote, found}
    account_id: Mapped[int | None] = mapped_column(BigInteger)  # where an accepted value goes
    model: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    applied: Mapped[dict[str, Any]] = mapped_column(default=dict)  # what accepting changed
    decided_at: Mapped[datetime | None]
