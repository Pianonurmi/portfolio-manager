from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.modules.crypto import booking
from app.modules.ledger.service import LedgerError
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.jobs import enqueue

router = APIRouter(prefix="/api/crypto", tags=["crypto"])


@router.post("/sync", status_code=202)
def sync(_: CurrentUser, db: Db) -> dict[str, int]:
    return {"run_id": enqueue(db, "crypto.sync").id}


@router.get("/transfers")
def transfers(_: CurrentUser, db: Db) -> dict[str, list[dict[str, Any]]]:
    """Crypto moves without a counterpart: their cost basis cannot follow them (ING-11)."""
    return booking.open_transfers(db)


@router.post("/transfers/match")
def match(user: CurrentUser, db: Db) -> dict[str, int]:
    n = booking.match_transfers(db)
    audit(db, user.username, "crypto.match_transfers", "ledger", None, after={"matched": n})
    return {"matched": n}


class LinkIn(BaseModel):
    out_id: int
    in_id: int


@router.post("/transfers/link")
def link(body: LinkIn, user: CurrentUser, db: Db) -> dict[str, bool]:
    try:
        booking.link(db, body.out_id, body.in_id)
    except LedgerError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(db, user.username, "crypto.link", "ledger_transaction", body.out_id, after=body.model_dump())
    return {"ok": True}


@router.delete("/transfers/link/{txn_id}", status_code=204)
def unlink(txn_id: int, user: CurrentUser, db: Db) -> None:
    booking.unlink(db, txn_id)
    audit(db, user.username, "crypto.unlink", "ledger_transaction", txn_id)
