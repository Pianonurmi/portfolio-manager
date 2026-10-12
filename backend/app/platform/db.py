"""Database engine, session handling and the declarative base."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, DateTime, Engine, MetaData, Numeric, Select, create_engine, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.platform.config import get_settings

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Money and quantities are exact decimals (NFR-04, ADR-12).
Amount = Numeric(18, 4)
Quantity = Numeric(20, 8)
Rate = Numeric(20, 10)
Share = Numeric(7, 6)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, Decimal: Amount, datetime: DateTime(timezone=True)}


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


def utcnow() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    """Today's date in the household's timezone."""
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_factory() -> sessionmaker[Session]:
    engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reset_engine() -> None:
    """Used by tests to point at another database."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def pairs(db: Session, stmt: Select[Any]) -> dict[Any, Any]:
    """Two-column query as a dict, e.g. {instrument id: name}."""
    return {k: v for k, v in db.execute(stmt).tuples()}
