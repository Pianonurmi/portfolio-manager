"""Use confirmed holdings from a statement: compare with the ledger, book the differences, or record the value."""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.modules.ai.models import Extraction
from app.modules.core.models import Account, Instrument
from app.modules.ingest.adapters.nordnet import guess_instrument_type
from app.modules.ingest.models import ReconciliationCheck
from app.modules.ledger.models import Transaction
from app.modules.ledger.service import TxnSpec, cash_instrument, positions, record
from app.modules.networth.models import Valuation
from app.modules.pricing.service import PriceBook, upsert_prices
from app.platform.db import utcnow

ZERO = Decimal(0)
CENT = Decimal("0.01")
MODES = {"compare", "book", "value", "cash"}


def D(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v).replace(",", ".").replace(" ", ""))


def _eur(book: PriceBook, amount: Decimal | None, ccy: str | None, on: date) -> Decimal | None:
    if amount is None:
        return None
    if not ccy or ccy == "EUR":
        return amount
    try:
        rate, _ = book.eur_per(ccy, on)
    except LookupError:
        return None
    return amount * rate


def _instrument(db: Session, row: dict[str, Any]) -> Instrument:
    if row.get("instrument_id"):
        inst = db.get(Instrument, int(row["instrument_id"]))
        if inst is not None:
            return inst
    if row.get("isin"):
        inst = db.scalar(select(Instrument).where(Instrument.isin == row["isin"]))
        if inst is not None:
            return inst
    name = str(row["name"])
    inst = Instrument(
        type=guess_instrument_type(name),
        name=name,
        isin=row.get("isin") or None,
        currency=str(row.get("price_ccy") or row.get("value_ccy") or "EUR"),
        asset_class="equity",
        meta={"created_from": "statement"},
    )
    db.add(inst)
    db.flush()
    return inst


def apply_holdings(
    db: Session, x: Extraction, rows: list[dict[str, Any]], account_id: int, mode: str, on: date
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(sorted(MODES))}")
    acct = db.get(Account, account_id)
    if acct is None:
        raise ValueError("Choose the account the statement belongs to")
    header = x.data.get("header") or {}
    book = PriceBook(db, set())
    cash_info = next(iter((header.get("cash") or {}).values()), None)
    cash_eur = _eur(book, D(cash_info["amount"]), cash_info["ccy"], on) if cash_info else None
    rows = [r for r in rows if not r.get("skip")]
    flagged = [r["name"] for r in rows if r.get("status") == "check" and not r.get("edited")]

    if mode == "value":
        parts = [_eur(book, D(r.get("value")), r.get("value_ccy"), on) for r in rows]
        if any(p is None for p in parts):
            raise ValueError("Every row needs a value (and a known currency) to record the total")
        total = (sum((p for p in parts if p is not None), ZERO) + (cash_eur or ZERO)).quantize(CENT)
        v = Valuation(
            account_id=account_id,
            date=on,
            value=total,
            method="statement",
            note=f"From {x.title}"[:500],
            document_ref=f"upload:{x.id}",
        )
        db.add(v)
        db.flush()
        return {"mode": mode, "valuation_id": v.id, "value": str(total)}

    held = {p.instrument_id: p.quantity for p in positions(db, on=on, account_ids=[account_id])}
    if mode == "compare":
        db.execute(delete(ReconciliationCheck).where(ReconciliationCheck.account_id == account_id))
        diffs = 0
        now = utcnow()
        for r in rows:
            q = D(r.get("quantity"))
            if q is None:
                continue
            inst_id = r.get("instrument_id")
            comp = held.get(int(inst_id), ZERO) if inst_id else ZERO
            places = max(0, -q.normalize().as_tuple().exponent)  # type: ignore[operator]
            ok = abs(comp - q) <= max(Decimal("0.000001"), Decimal(5) / Decimal(10) ** (places + 1))  # shown rounded
            diffs += not ok
            db.add(
                ReconciliationCheck(
                    account_id=account_id,
                    as_of=now,
                    subject=f"{r['name']} (statement {on})",
                    instrument_id=inst_id,
                    reported=str(q),
                    computed=str(comp),
                    ok=ok,
                )
            )
        if cash_info and cash_info["ccy"]:
            cid = cash_instrument(db, cash_info["ccy"]).id
            rep, comp = D(cash_info["amount"]) or ZERO, held.get(cid, ZERO)
            ok = abs(rep - comp) <= CENT
            diffs += not ok
            db.add(
                ReconciliationCheck(
                    account_id=account_id,
                    as_of=now,
                    subject=f"Cash {cash_info['ccy']} (statement {on})",
                    reported=str(rep),
                    computed=str(comp),
                    ok=ok,
                )
            )
        db.flush()
        return {"mode": mode, "differences": diffs}

    n = 0
    if mode == "cash":
        if not cash_info or not cash_info["ccy"]:
            raise ValueError("The statement shows no cash balance")
        cid = cash_instrument(db, cash_info["ccy"]).id
        diff = (D(cash_info["amount"]) or ZERO) - held.get(cid, ZERO)
        if diff:
            record(
                db,
                TxnSpec(
                    account_id=account_id,
                    date=on,
                    kind="deposit" if diff > 0 else "withdrawal",
                    currency=cash_info["ccy"],
                    amount=diff,
                    description=f"Statement {on}: cash",
                    source="statement",
                    external_id=f"stmt:{x.id}:0",
                ),
            )
        return {"mode": mode, "cash": str(D(cash_info["amount"])), "change": str(diff)}

    # book: move the ledger to the statement's quantities (opening balances, or corrections); rows still
    # flagged by the checks are never booked unless edited by hand
    rows = [r for r in rows if r["name"] not in flagged]
    skipped: list[str] = []
    price_rows: list[dict[str, object]] = []
    # Lot exports (purchase date and cost per lot) open each lot on its own date when the ledger has none yet:
    # the tax engine then sees the real acquisition dates and costs.
    lot_rows = [r for r in rows if r.get("acquired") and D(r.get("quantity")) and D(r.get("cost")) is not None]
    by_inst: dict[int, list[dict[str, Any]]] = {}
    for r in lot_rows:
        by_inst.setdefault(_instrument(db, r).id, []).append(r)
    lots_booked: set[int] = set()
    for inst_id, lots in by_inst.items():
        if held.get(inst_id, ZERO) != 0:
            continue
        for r in sorted(lots, key=lambda r: str(r["acquired"])):
            record(
                db,
                TxnSpec(
                    account_id=account_id,
                    date=date.fromisoformat(str(r["acquired"])),
                    kind="security_in",
                    currency="EUR",
                    instrument_id=inst_id,
                    quantity=D(r["quantity"]) or ZERO,
                    cost_basis=_eur(book, D(r.get("cost")), r.get("value_ccy"), on),
                    description=f"Opening lot from statement: {r['name']}",
                    source="statement",
                    external_id=f"stmt:{x.id}:{n}",
                ),
            )
            n += 1
        lots_booked.add(inst_id)
    for r in rows:
        q = D(r.get("quantity"))
        if q is None:
            skipped.append(r["name"])
            continue
        inst = _instrument(db, r)
        if inst.id in lots_booked:
            if r.get("price") is not None:
                price_rows.append(
                    {
                        "instrument_id": inst.id,
                        "date": on,
                        "close": D(r["price"]),
                        "currency": r.get("price_ccy") or inst.currency,
                        "source": "statement",
                    }
                )
            continue
        diff = q - held.get(inst.id, ZERO)
        if r.get("price") is not None:
            price_rows.append(
                {
                    "instrument_id": inst.id,
                    "date": on,
                    "close": D(r["price"]),
                    "currency": r.get("price_ccy") or inst.currency,
                    "source": "statement",
                }
            )
        if diff == 0:
            continue
        cost = _eur(book, D(r.get("cost")), r.get("value_ccy"), on)
        if cost is None:  # no purchase value on the statement: market value is the best available
            cost = _eur(book, D(r.get("value")), r.get("value_ccy"), on)
        spec = TxnSpec(
            account_id=account_id,
            date=on,
            kind="security_in" if diff > 0 else "security_out",
            currency="EUR",
            instrument_id=inst.id,
            quantity=abs(diff),
            cost_basis=(cost * diff / q).quantize(CENT) if diff > 0 and cost is not None and q else None,
            description=f"Statement {on}: {r['name']}",
            source="statement",
            external_id=f"stmt:{x.id}:{n}",
        )
        record(db, spec)
        n += 1
    if cash_info and cash_info["ccy"]:
        cid = cash_instrument(db, cash_info["ccy"]).id
        diff = (D(cash_info["amount"]) or ZERO) - held.get(cid, ZERO)
        if diff:
            record(
                db,
                TxnSpec(
                    account_id=account_id,
                    date=on,
                    kind="deposit" if diff > 0 else "withdrawal",
                    currency=cash_info["ccy"],
                    amount=diff,
                    description=f"Statement {on}: cash",
                    source="statement",
                    external_id=f"stmt:{x.id}:{n}",
                ),
            )
            n += 1
    upsert_prices(db, list({(p["instrument_id"], p["date"]): p for p in price_rows}.values()))
    db.flush()
    return {"mode": mode, "transactions": n, "skipped_without_quantity": skipped, "skipped_flagged": flagged}


def undo(db: Session, xid: int) -> None:
    x = db.get(Extraction, xid)
    if x is None:
        return
    a = x.applied or {}
    if a.get("valuation_id"):
        v = db.get(Valuation, int(a["valuation_id"]))
        if v:
            db.delete(v)
    db.execute(
        update(Transaction)
        .where(
            Transaction.source == "statement",
            Transaction.external_id.like(f"stmt:{xid}:%"),
            Transaction.voided_at.is_(None),
        )
        .values(voided_at=utcnow(), void_reason="statement undone")
    )
    x.status, x.applied, x.decided_at = "review", {}, None
