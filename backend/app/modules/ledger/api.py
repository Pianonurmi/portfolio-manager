"""Ledger endpoints and provenance for positions and transactions."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.ledger.models import TXN_KINDS, Posting, Transaction
from app.modules.ledger.service import LedgerError, TxnSpec, positions, record, void
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import pairs
from app.platform.explain import ExplainNode, explainer
from app.platform.registry import SearchHit, search_provider, undo_handler

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


def _fmt_q(q: Decimal) -> str:
    q = q.normalize()
    s = f"{q:,.8f}".rstrip("0").rstrip(".") if q != q.to_integral() else f"{q:,.0f}"
    return s.replace(",", " ").replace(".", ",")


def _fmt_m(v: Decimal, ccy: str) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + (" €" if ccy == "EUR" else f" {ccy}")


def txn_out(t: Transaction, names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "date": t.date,
        "kind": t.kind,
        "description": t.description,
        "currency": t.currency,
        "source": t.source,
        "external_id": t.external_id,
        "import_batch_id": t.import_batch_id,
        "voided": t.voided_at is not None,
        "postings": [
            {
                "account_id": p.account_id,
                "instrument_id": p.instrument_id,
                "instrument": names.get(p.instrument_id or 0),
                "quantity": str(p.quantity),
                "amount": str(p.amount),
                "role": p.role,
                "price": None if p.price is None else str(p.price),
                "price_currency": p.price_currency,
                "fx_rate": None if p.fx_rate is None else str(p.fx_rate),
            }
            for p in t.postings
        ],
    }


@router.get("/transactions")
def list_transactions(
    _: CurrentUser,
    db: Db,
    account_id: int | None = None,
    instrument_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    include_voided: bool = False,
) -> dict[str, Any]:
    q = select(Transaction)
    if account_id:
        q = q.where(Transaction.account_id == account_id)
    if instrument_id:
        q = q.where(Transaction.id.in_(select(Posting.transaction_id).where(Posting.instrument_id == instrument_id)))
    if not include_voided:
        q = q.where(Transaction.voided_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(Transaction.date.desc(), Transaction.seq.desc(), Transaction.id.desc())
        .limit(min(limit, 1000))
        .offset(offset)
    ).all()
    names = pairs(db, select(Instrument.id, Instrument.name))
    return {"total": total, "items": [txn_out(t, names) for t in rows]}


class ManualTxnIn(BaseModel):
    account_id: int
    date: date
    kind: str
    amount: Decimal
    currency: str = "EUR"
    instrument_id: int | None = None
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    fees: Decimal = Decimal(0)
    description: str | None = None


@router.post("/transactions", status_code=201)
def create_transaction(body: ManualTxnIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if body.kind not in TXN_KINDS:
        raise HTTPException(422, f"Unknown kind {body.kind}")
    if db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    try:
        t = record(db, TxnSpec(**body.model_dump(), source="manual"))
    except LedgerError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(db, user.username, "ledger.create", "ledger_transaction", t.id, after=body.model_dump(mode="json"))
    undo_id = make_undo(db, "ledger.void", f"Add {body.kind}", {"transaction_id": t.id})
    names = pairs(db, select(Instrument.id, Instrument.name))
    return {**txn_out(t, names), "undo_id": undo_id}


@router.post("/transactions/{txn_id}/void")
def void_transaction(txn_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    t = db.get(Transaction, txn_id)
    if t is None or t.voided_at is not None:
        raise HTTPException(404, "Transaction not found")
    void(db, t, f"voided by {user.username}")
    audit(db, user.username, "ledger.void", "ledger_transaction", t.id)
    return {"undo_id": make_undo(db, "ledger.unvoid", "Void transaction", {"transaction_id": t.id})}


@undo_handler("ledger.void")
def _undo_create(db: Session, inv: dict[str, Any]) -> None:
    t = db.get(Transaction, int(inv["transaction_id"]))
    if t and t.voided_at is None:
        void(db, t, "undone")


@undo_handler("ledger.unvoid")
def _undo_void(db: Session, inv: dict[str, Any]) -> None:
    t = db.get(Transaction, int(inv["transaction_id"]))
    if t:
        t.voided_at, t.void_reason = None, None


@router.get("/positions")
def list_positions(
    _: CurrentUser, db: Db, on: date | None = None, account_id: int | None = None
) -> list[dict[str, Any]]:
    pos = positions(db, on=on, account_ids=[account_id] if account_id else None)
    names = pairs(db, select(Instrument.id, Instrument.name))
    return [
        {
            "account_id": p.account_id,
            "instrument_id": p.instrument_id,
            "instrument": names.get(p.instrument_id),
            "quantity": str(p.quantity),
            "ref": f"pos:{p.account_id}:{p.instrument_id}",
        }
        for p in pos
    ]


# ---- provenance ----------------------------------------------------------------------------------


def raw_node(raw_id: int) -> ExplainNode:
    """Lazy link to the imported source row (explained by the ingest module)."""
    return ExplainNode(label="Source row", source="import", ref=f"raw:{raw_id}")


@explainer("pos")
def explain_position(db: Session, account_id: str, instrument_id: str, on: str | None = None) -> ExplainNode:
    a, i = db.get(Account, int(account_id)), db.get(Instrument, int(instrument_id))
    if a is None or i is None:
        raise LookupError("position not found")
    q = (
        select(Posting, Transaction)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Posting.account_id == a.id, Posting.instrument_id == i.id, Transaction.voided_at.is_(None))
        .order_by(Transaction.date, Transaction.seq, Transaction.id)
    )
    if on:
        q = q.where(Transaction.date <= date.fromisoformat(on))
    rows = db.execute(q).all()
    total = sum((p.quantity for p, _ in rows), Decimal(0))
    kids = [
        ExplainNode(
            label=f"{t.date:%-d.%-m.%Y} · {t.description or t.kind}",
            value=("+" if p.quantity > 0 else "") + _fmt_q(p.quantity),
            source="ledger",
            note=f"transaction #{t.id}" + (f" · {t.source}" if t.source != "manual" else " · manual"),
            children=[raw_node(t.raw_record_id)] if t.raw_record_id else [],
        )
        for p, t in rows[-200:]
    ]
    if len(rows) > 200:
        kids.insert(0, ExplainNode(label=f"{len(rows) - 200} earlier postings not shown", source="ledger"))
    return ExplainNode(
        label=f"{i.name} in {a.name}",
        value=_fmt_q(total),
        formula="Σ quantity of postings",
        source="ledger",
        children=kids,
    )


@explainer("txn")
def explain_txn(db: Session, txn_id: str) -> ExplainNode:
    t = db.get(Transaction, int(txn_id))
    if t is None:
        raise LookupError("transaction not found")
    names = pairs(db, select(Instrument.id, Instrument.name))
    kids = [
        ExplainNode(
            label=f"{p.role}: {names.get(p.instrument_id or 0, '')}".rstrip(": "),
            value=_fmt_m(p.amount, t.currency),
            source="ledger",
            note=(f"qty {_fmt_q(p.quantity)}" if p.quantity else None),
        )
        for p in t.postings
    ]
    if t.raw_record_id:
        kids.append(raw_node(t.raw_record_id))
    return ExplainNode(
        label=f"{t.description or t.kind} · {t.date:%-d.%-m.%Y}", formula="postings sum to 0", children=kids
    )


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    like = f"%{q.lower()}%"
    rows = db.scalars(
        select(Transaction)
        .where(func.lower(Transaction.description).like(like), Transaction.voided_at.is_(None))
        .order_by(Transaction.date.desc())
        .limit(min(limit, 8))
    ).all()
    return [
        SearchHit(
            kind="transaction",
            id=str(t.id),
            title=t.description or t.kind,
            subtitle=f"{t.date:%-d.%-m.%Y}",
            route=f"/portfolio?txn={t.id}",
            ref=f"txn:{t.id}",
        )
        for t in rows
    ]
