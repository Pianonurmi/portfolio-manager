"""Import endpoints: upload → review → map → commit (ING-01..03, UX-06)."""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.ingest.adapters import nordnet
from app.modules.ingest.models import ImportBatch, RawRecord, ReconciliationCheck
from app.modules.ingest.service import (
    ImportError_,
    commit,
    create_missing_instruments,
    instrument_key,
    reconcile,
    resolve_batch,
    set_mapping,
    stage,
    undo_commit,
)
from app.modules.ledger.models import TXN_KINDS, Transaction
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import pairs
from app.platform.explain import ExplainNode, explainer
from app.platform.registry import undo_handler

router = APIRouter(prefix="/api/imports", tags=["imports"])
MAX_BYTES = 25 * 1024 * 1024


def batch_out(db: Session, b: ImportBatch) -> dict[str, Any]:
    counts = b.stats.get("counts", {})
    return {
        "id": b.id,
        "source": b.source,
        "adapter": b.adapter,
        "filename": b.filename,
        "file_sha256": b.file_sha256,
        "status": b.status,
        "created_at": b.created_at,
        "committed_at": b.committed_at,
        "counts": counts,
        "rows": sum(counts.values()),
        "meta": b.stats.get("meta", {}),
    }


def _legs_text(p: dict[str, Any]) -> str:
    parts = []
    for x in p.get("legs", []):
        net = Decimal(x["amount"]) - Decimal(x["fee"])
        parts.append(
            f"{'+' if net > 0 else '−'}{abs(net).normalize():f} {x['asset']}{' (fee)' if x.get('is_fee') else ''}"
        )
    return " · ".join(parts)


def row_out(r: RawRecord, names: dict[int, str], accts: dict[int, str]) -> dict[str, Any]:
    p = r.parsed
    if "legs" in p:  # crypto event: one row, several assets
        p = {**p, "resolved_kind": p.get("event"), "security": _legs_text(p)}
    return {
        "id": r.id,
        "row_no": r.row_no,
        "status": r.status,
        "message": r.message,
        "date": p.get("date"),
        "raw_type": p.get("raw_type"),
        "kind": p.get("resolved_kind") or p.get("kind"),
        "security": p.get("security"),
        "isin": p.get("isin"),
        "quantity": p.get("quantity"),
        "price": p.get("price"),
        "price_ccy": p.get("price_ccy"),
        "amount": p.get("amount"),
        "currency": p.get("amount_ccy"),
        "fees": p.get("fees"),
        "portfolio": p.get("portfolio"),
        "account_id": r.account_id,
        "account": accts.get(r.account_id) if r.account_id else None,
        "instrument_id": r.instrument_id,
        "instrument": names.get(r.instrument_id) if r.instrument_id else None,
        "instrument_key": instrument_key(p) if (p.get("isin") or p.get("security")) else None,
        "transaction_id": r.transaction_id,
        "raw": r.raw,
    }


@router.get("")
def list_batches(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        batch_out(db, b)
        for b in db.scalars(
            select(ImportBatch)
            .where(ImportBatch.status != "discarded")
            .order_by(ImportBatch.created_at.desc())
            .limit(100)
        )
    ]


@router.post("", status_code=201)
async def upload(
    user: CurrentUser, db: Db, file: UploadFile = File(...), account_id: int | None = Form(None)
) -> dict[str, Any]:
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "The file is larger than 25 MB")
    try:
        b = stage(db, file.filename or "upload.csv", content, default_account_id=account_id)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        user.username,
        "import.stage",
        "import_batch",
        b.id,
        after={"filename": b.filename, "rows": b.stats.get("counts")},
    )
    return batch_out(db, b)


def _batch(db: Session, batch_id: int) -> ImportBatch:
    b = db.get(ImportBatch, batch_id)
    if b is None or b.status == "discarded":
        raise HTTPException(404, "Import not found")
    return b


@router.get("/{batch_id}")
def get_batch(batch_id: int, _: CurrentUser, db: Db, status: str | None = None) -> dict[str, Any]:
    b = _batch(db, batch_id)
    q = select(RawRecord).where(RawRecord.batch_id == b.id)
    if status:
        q = q.where(RawRecord.status.in_(status.split(",")))
    rows = db.scalars(q.order_by(RawRecord.row_no)).all()
    names = pairs(db, select(Instrument.id, Instrument.name))
    accts = pairs(db, select(Account.id, Account.name))
    unknown_types = sorted(
        {str(r.parsed["raw_type"]) for r in rows if r.status == "needs_review" and r.parsed.get("raw_type")}
    )
    portfolios = sorted(
        {r.parsed.get("portfolio") or "" for r in rows if r.status == "needs_mapping" and r.account_id is None}
    )
    missing = {}
    for r in rows:
        if (
            r.status == "needs_mapping"
            and r.account_id
            and r.instrument_id is None
            and (r.parsed.get("isin") or r.parsed.get("security"))
        ):
            missing[instrument_key(r.parsed)] = {
                "key": instrument_key(r.parsed),
                "name": r.parsed.get("security"),
                "isin": r.parsed.get("isin"),
                "currency": nordnet.guess_currency(r.parsed),
            }
    return {
        **batch_out(db, b),
        "rows_detail": [row_out(r, names, accts) for r in rows],
        "unknown_types": unknown_types,
        "unmapped_portfolios": portfolios,
        "missing_instruments": list(missing.values()),
        "kinds": sorted(TXN_KINDS | {"ignore"}),
        "replaces_statements": _statement_bookings(db, {r.account_id for r in rows if r.status == "new"}),
    }


def _statement_bookings(db: Session, account_ids: set[int | None]) -> list[dict[str, Any]]:
    """Accounts whose positions were booked from statements: committing the history replaces those entries."""
    ids = {a for a in account_ids if a}
    if not ids:
        return []
    found = db.execute(
        select(Transaction.account_id, func.count())
        .where(Transaction.account_id.in_(ids), Transaction.source == "statement", Transaction.voided_at.is_(None))
        .group_by(Transaction.account_id)
    ).all()
    names = dict(db.execute(select(Account.id, Account.name).where(Account.id.in_(ids))).tuples().all())
    return [{"account_id": a, "account": names.get(a, str(a)), "entries": n} for a, n in found]


class MapAccountIn(BaseModel):
    portfolio: str
    account_id: int


@router.post("/{batch_id}/map-account")
def map_account(batch_id: int, body: MapAccountIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    if db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    if body.portfolio:
        set_mapping(db, b.source, "account", body.portfolio, str(body.account_id))
        acct = db.get(Account, body.account_id)
        if acct and not acct.external_ref:
            acct.external_ref = body.portfolio
    else:
        stats = dict(b.stats)
        stats["default_account_id"] = body.account_id
        b.stats = stats
    resolve_batch(db, b)
    audit(db, user.username, "import.map_account", "import_batch", b.id, after=body.model_dump())
    return batch_out(db, b)


class MapInstrumentIn(BaseModel):
    key: str
    instrument_id: int


@router.post("/{batch_id}/map-instrument")
def map_instrument(batch_id: int, body: MapInstrumentIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    if db.get(Instrument, body.instrument_id) is None:
        raise HTTPException(404, "Instrument not found")
    set_mapping(db, b.source, "instrument", body.key, str(body.instrument_id))
    resolve_batch(db, b)
    return batch_out(db, b)


@router.post("/{batch_id}/create-instruments")
def create_instruments(batch_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    n = create_missing_instruments(db, b)
    return {**batch_out(db, b), "created": n}


class MapTypeIn(BaseModel):
    raw_type: str
    kind: str


@router.post("/{batch_id}/map-type")
def map_type(batch_id: int, body: MapTypeIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    if body.kind not in TXN_KINDS | {"ignore"}:
        raise HTTPException(422, f"Unknown kind {body.kind}")
    set_mapping(db, b.source, "txn_type", body.raw_type.upper(), body.kind)
    resolve_batch(db, b)
    return batch_out(db, b)


class RowStatusIn(BaseModel):
    ignore: bool


@router.post("/{batch_id}/rows/{row_id}")
def set_row(batch_id: int, row_id: int, body: RowStatusIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    r = db.get(RawRecord, row_id)
    if r is None or r.batch_id != b.id:
        raise HTTPException(404, "Row not found")
    if r.status == "committed":
        raise HTTPException(409, "This row is already in the ledger; undo the import to change it")
    if body.ignore:
        r.status, r.message = "ignored", "Ignored by you"
    else:
        r.status, r.message = "new", None
        resolve_batch(db, b)
    return {"id": r.id, "status": r.status}


@router.post("/{batch_id}/commit")
def commit_batch(batch_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    b = _batch(db, batch_id)
    result = commit(db, b)
    undo_id = None
    if result["committed"]:
        undo_id = make_undo(db, "import.commit", f"Commit import #{b.id}", {"batch_id": b.id}, minutes=60 * 24)
    audit(
        db,
        user.username,
        "import.commit",
        "import_batch",
        b.id,
        after={k: v for k, v in result.items() if k != "reconciliation"},
    )
    return {**result, "undo_id": undo_id, "batch": batch_out(db, b)}


@undo_handler("import.commit")
def _undo_import(db: Session, inverse: dict[str, Any]) -> None:
    undo_commit(db, int(inverse["batch_id"]))


@router.delete("/{batch_id}", status_code=204)
def discard(batch_id: int, user: CurrentUser, db: Db) -> None:
    b = _batch(db, batch_id)
    committed = db.scalar(
        select(func.count()).select_from(RawRecord).where(RawRecord.batch_id == b.id, RawRecord.status == "committed")
    )
    if committed:
        raise HTTPException(409, "Rows from this import are in the ledger. Undo the import first.")
    b.status = "discarded"
    audit(db, user.username, "import.discard", "import_batch", b.id)


recon = APIRouter(prefix="/api/reconciliation", tags=["imports"])


@recon.get("")
def reconciliation(_: CurrentUser, db: Db, account_id: int | None = None) -> list[dict[str, Any]]:
    q = select(ReconciliationCheck, Account.name).join(Account, Account.id == ReconciliationCheck.account_id)
    if account_id:
        q = q.where(ReconciliationCheck.account_id == account_id)
    return [
        {
            "account_id": c.account_id,
            "account": n,
            "subject": c.subject,
            "instrument_id": c.instrument_id,
            "reported": c.reported,
            "computed": c.computed,
            "ok": c.ok,
            "as_of": c.as_of,
            "raw_record_id": c.raw_record_id,
        }
        for c, n in db.execute(q.order_by(Account.name, ReconciliationCheck.ok, ReconciliationCheck.subject)).all()
    ]


@recon.post("/{account_id}")
def rerun(account_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    return reconcile(db, account_id)


extra_routers = [recon]


@explainer("raw")
def explain_raw(db: Session, raw_id: str) -> ExplainNode:
    rr = db.get(RawRecord, int(raw_id))
    if rr is None:
        raise LookupError("source row not found")
    b = db.get(ImportBatch, rr.batch_id)
    fields = [ExplainNode(label=k, value=v) for k, v in rr.raw.items() if v and v.strip() and not k.startswith("col")]
    return ExplainNode(
        label=f"Row {rr.row_no} of {b.filename if b else '?'}",
        source="import",
        note=f"import #{rr.batch_id} · {b.adapter if b else ''} · file sha256 {b.file_sha256[:12] if b else ''}…",
        children=fields,
    )
