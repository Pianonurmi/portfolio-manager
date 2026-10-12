"""Price endpoints, data freshness (UX-05) and price/FX provenance."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ingest.models import ImportBatch, ReconciliationCheck
from app.modules.ledger.models import Transaction
from app.modules.ledger.service import TxnSpec, positions, record, void
from app.modules.pricing.models import CorporateAction, FxRate, Price
from app.modules.pricing.service import PriceBook, missing_splits, stale_text
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.explain import ExplainNode, explainer
from app.platform.registry import undo_handler

router = APIRouter(prefix="/api", tags=["pricing"])


@router.get("/freshness")
def freshness(_: CurrentUser, db: Db) -> dict[str, Any]:
    pos = [p for p in positions(db) if p.quantity != 0]
    book = PriceBook(db, {p.instrument_id for p in pos})
    t = today()
    stale = []
    latest: date | None = None
    for p in pos:
        if p.instrument_id in book.cash_ccy:
            continue
        pp = book.price(p.instrument_id, t)
        if pp and (latest is None or pp.date > latest):
            latest = pp.date
        if stale_text(pp, t):
            stale.append(p.instrument_id)
    last_import = db.scalar(select(func.max(ImportBatch.created_at)).where(ImportBatch.status != "discarded"))
    unrec = (
        db.scalar(select(func.count()).select_from(ReconciliationCheck).where(ReconciliationCheck.ok.is_(False))) or 0
    )
    return {
        "prices_as_of": latest,
        "stale_count": len(set(stale)),
        "stale_instruments": sorted(set(stale)),
        "last_import": last_import,
        "unreconciled": unrec,
    }


@router.get("/prices/{instrument_id}")
def price_series(instrument_id: int, _: CurrentUser, db: Db, since: date | None = None) -> list[dict[str, Any]]:
    book = PriceBook(db, {instrument_id})
    pts = book.prices.get(instrument_id, [])
    return [
        {"date": p.date, "close": str(p.close), "currency": p.currency, "source": p.source}
        for p in pts
        if since is None or p.date >= since
    ]


class PriceIn(BaseModel):
    instrument_id: int
    date: date
    close: Decimal = Field(gt=0)
    currency: str | None = None


@router.post("/prices", status_code=201)
def manual_price(body: PriceIn, user: CurrentUser, db: Db) -> dict[str, str]:
    inst = db.get(Instrument, body.instrument_id)
    if inst is None:
        raise HTTPException(404, "Instrument not found")
    existing = db.scalar(
        select(Price).where(Price.instrument_id == inst.id, Price.date == body.date, Price.source == "manual")
    )
    if existing:
        existing.close = body.close
    else:
        db.add(
            Price(
                instrument_id=inst.id,
                date=body.date,
                close=body.close,
                currency=body.currency or inst.currency,
                source="manual",
            )
        )
    audit(db, user.username, "price.manual", "instrument", inst.id, after=body.model_dump(mode="json"))
    return {"status": "ok"}


def _num(v: Decimal, places: int = 2) -> str:
    return f"{v:,.{places}f}".replace(",", " ").replace(".", ",")


@explainer("price")
def explain_price(db: Session, instrument_id: str, on: str) -> ExplainNode:
    inst = db.get(Instrument, int(instrument_id))
    if inst is None:
        raise LookupError("instrument not found")
    d = date.fromisoformat(on)
    book = PriceBook(db, {inst.id})
    p = book.price(inst.id, d)
    if p is None:
        return ExplainNode(
            label=f"No price for {inst.name}", note="Add a manual price or set a price source symbol", stale=True
        )
    stale = stale_text(p, today())
    src = {
        "yahoo": f"EOD close from Yahoo Finance ({inst.price_symbol})",
        "trade": "your own executed trade price",
        "manual": "entered manually",
        "nav": "fund NAV",
    }.get(p.source, p.source)
    return ExplainNode(
        label=f"Price of {inst.name}",
        value=f"{_num(p.close, 4)} {p.currency}",
        source="price",
        stale=bool(stale),
        note=f"{p.date:%-d.%-m.%Y} · {src}" + (f" · {stale}" if stale else ""),
    )


@explainer("fx")
def explain_fx(db: Session, currency: str, on: str) -> ExplainNode:
    d = date.fromisoformat(on)
    row = db.scalar(
        select(FxRate).where(FxRate.quote == currency, FxRate.date <= d).order_by(FxRate.date.desc()).limit(1)
    )
    if row is None:
        raise LookupError(f"no FX rate for {currency}")
    return ExplainNode(
        label=f"EUR{currency}",
        value=_num(row.rate, 4),
        source="fx",
        note=f"ECB reference rate {row.date:%-d.%-m.%Y} · 1 {currency} = {_num(1 / row.rate, 4)} €",
    )


@router.get("/corporate-actions/missing")
def list_missing_splits(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    names = {i.id: i.name for i in db.scalars(select(Instrument))}
    return [
        {
            **m,
            "instrument": names.get(m["instrument_id"]),
            "ratio": str(m["ratio"]),
            "accounts": [{"account_id": a, "quantity": str(q)} for a, q in m["accounts"]],
        }
        for m in missing_splits(db)
    ]


@router.post("/corporate-actions/{ca_id}/book")
def book_split(ca_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    ca = db.get(CorporateAction, ca_id)
    if ca is None:
        raise HTTPException(404, "Corporate action not found")
    todo = next((m for m in missing_splits(db) if m["id"] == ca.id), None)
    if todo is None:
        raise HTTPException(409, "This split is already in the ledger")
    ids = []
    for acct, qty in todo["accounts"]:
        extra = (qty * ca.ratio - qty).quantize(Decimal("0.00000001"))
        t = record(
            db,
            TxnSpec(
                account_id=acct,
                date=ca.date,
                kind="split",
                currency="EUR",
                instrument_id=ca.instrument_id,
                quantity=extra,
                description=f"Split {format(ca.ratio.normalize(), 'f')}:1 (from price provider)",
                source="corporate_action",
                external_id=f"ca:{ca.id}:{acct}",
            ),
        )
        ids.append(t.id)
    audit(db, user.username, "split.book", "corporate_action", ca.id, after={"transactions": ids})
    return {"transactions": ids, "undo_id": make_undo(db, "ledger.void_many", "Book split", {"ids": ids})}


@undo_handler("ledger.void_many")
def _void_many(db: Session, inv: dict[str, Any]) -> None:
    for tid in inv["ids"]:
        t = db.get(Transaction, int(tid))
        if t and t.voided_at is None:
            void(db, t, "undone")
