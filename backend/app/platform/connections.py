"""Helpers for stored connections and their encrypted secrets."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.models import Connection
from app.platform.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/api/connections", tags=["connections"])
KINDS = {"ynab", "actual", "kraken", "binance", "ledger", "paperless", "gotify", "smtp", "llm"}


def get_connection(db: Session, kind: str) -> Connection | None:
    return db.scalar(select(Connection).where(Connection.kind == kind))


def secret(conn: Connection) -> str | None:
    return decrypt_secret(conn.secret_enc) if conn.secret_enc else None


def out(c: Connection) -> dict[str, Any]:
    return {
        "kind": c.kind,
        "config": c.config,
        "has_secret": bool(c.secret_enc),
        "status": c.status,
        "last_sync": c.last_sync,
        "error": c.error,
    }


class ConnectionIn(BaseModel):
    config: dict[str, Any] = {}
    secret: str | None = None  # omitted = keep existing


@router.get("")
def list_connections(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [out(c) for c in db.scalars(select(Connection).order_by(Connection.kind))]


@router.put("/{kind}")
def save_connection(kind: str, body: ConnectionIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown connection {kind}")
    c = get_connection(db, kind)
    if c is None:
        c = Connection(kind=kind)
        db.add(c)
    c.config = {**(c.config or {}), **body.config}
    if body.secret:
        c.secret_enc = encrypt_secret(body.secret.strip())
    c.status, c.error = "new", None
    db.flush()
    audit(
        db,
        user.username,
        "connection.save",
        "connection",
        kind,
        after={"config": c.config, "secret_changed": bool(body.secret)},
    )
    return out(c)


@router.delete("/{kind}", status_code=204)
def delete_connection(kind: str, user: CurrentUser, db: Db) -> None:
    c = get_connection(db, kind)
    if c:
        db.delete(c)
        audit(db, user.username, "connection.delete", "connection", kind)
