"""Links to Paperless-ngx documents, tags and notes on any record (DOC-01/02, CORE-08)."""

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin

SUBJECTS = {"account", "instrument", "transaction", "valuation", "person", "gift", "policy", "scenario"}


class DocumentLink(IdMixin, TimestampMixin, Base):
    __tablename__ = "document_link"
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    paperless_id: Mapped[int]
    title: Mapped[str] = mapped_column(String(255))
    created_on: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "paperless_id"),
        Index("ix_doclink_subject", "subject_type", "subject_id"),
    )


class Tag(IdMixin, TimestampMixin, Base):
    __tablename__ = "tag"
    name: Mapped[str] = mapped_column(String(64), unique=True)


class TagLink(IdMixin, Base):
    __tablename__ = "tag_link"
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tag.id", ondelete="CASCADE"))
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("tag_id", "subject_type", "subject_id"),)


class Note(IdMixin, TimestampMixin, Base):
    __tablename__ = "note"
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_note_subject", "subject_type", "subject_id"),)
