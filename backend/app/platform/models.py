"""Cross-cutting tables: users and sessions, audit, outbound log, jobs, rules, undo, events."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base, IdMixin, TimestampMixin


class AppUser(IdMixin, TimestampMixin, Base):
    __tablename__ = "app_user"
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(default=False)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AuthSession(TimestampMixin, Base):
    __tablename__ = "auth_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(64))


class AuditLog(IdMixin, TimestampMixin, Base):
    """Who changed what (non-derived data only, NFR-07)."""

    __tablename__ = "audit_log"
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]


class OutboundLog(IdMixin, TimestampMixin, Base):
    """Every external HTTP call (NFR-02)."""

    __tablename__ = "outbound_log"
    host: Mapped[str] = mapped_column(String(255), index=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[int | None]
    bytes: Mapped[int | None]
    duration_ms: Mapped[int | None]
    blocked: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)


class JobRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "job_run"
    job: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))  # queued | running | ok | partial | failed
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule | manual
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    summary: Mapped[str | None] = mapped_column(Text)
    log: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_job_run_status_created", "status", "created_at"),)


class RuleSet(IdMixin, TimestampMixin, Base):
    """Versioned rule tables (tax parameters etc.) loaded from rules/*.yaml (ADR-09)."""

    __tablename__ = "rule_set"
    domain: Mapped[str] = mapped_column(String(32))
    year: Mapped[int]
    version: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]]
    source_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("domain", "year", "version"),)


class UndoEntry(IdMixin, TimestampMixin, Base):
    """Reversible actions (UX-06). `inverse` describes how to undo."""

    __tablename__ = "undo_entry"
    action: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255))
    inverse: Mapped[dict[str, Any]]
    expires_at: Mapped[datetime]
    undone_at: Mapped[datetime | None]


class Event(IdMixin, TimestampMixin, Base):
    """Generic timeline event shared by all modules (LIFE-02)."""

    __tablename__ = "event"
    kind: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Dashboard(IdMixin, TimestampMixin, Base):
    """User-arranged dashboards (UX-03, ADR-18)."""

    __tablename__ = "dashboard"
    name: Mapped[str] = mapped_column(String(128))
    is_home: Mapped[bool] = mapped_column(default=False)
    layout: Mapped[dict[str, Any]] = mapped_column(default=dict)
    version: Mapped[int] = mapped_column(default=1)


class SavedView(IdMixin, TimestampMixin, Base):
    __tablename__ = "saved_view"
    screen: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Connection(IdMixin, TimestampMixin, Base):
    """An external service the user connected (YNAB, Kraken, ...). Secrets are encrypted at rest (NFR-09)."""

    __tablename__ = "connection"
    kind: Mapped[str] = mapped_column(String(32), unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secret_enc: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | ok | error
    last_sync: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class WorkerStatus(Base):
    """The worker's version and heartbeat (one row), so the UI can tell a stopped or outdated worker."""

    __tablename__ = "worker_status"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime]
    heartbeat_at: Mapped[datetime]
