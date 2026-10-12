"""Which budget app feeds cash flow (CF-04): YNAB today, Actual Budget as the drop-in alternative."""

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.cashflow import actual, ynab
from app.platform.connections import get_connection
from app.platform.jobs import JobContext

NAMES = {"ynab": "YNAB", "actual": "Actual Budget"}


def active(db: Session) -> str | None:
    """Actual wins when connected: connecting it is the migration step."""
    a = get_connection(db, "actual")
    if a is not None and (a.config or {}).get("url"):
        return "actual"
    return "ynab" if get_connection(db, "ynab") is not None else None


def sync(db: Session, ctx: JobContext | None = None, client: Any | None = None) -> dict[str, int]:
    kind = active(db)
    if kind == "actual":
        return actual.sync(db, ctx, client)
    return ynab.sync(db, ctx, client)


def push_values(
    db: Session, values: dict[int, Decimal], client: Any | None = None, ctx: JobContext | None = None
) -> int:
    if active(db) == "actual":
        return actual.push_values(db, values, client, ctx)
    return ynab.push_values(db, values, client, ctx)
