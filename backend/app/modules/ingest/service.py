"""Staging, resolution, commit and reconciliation of imports (ING-01..03)."""

import hashlib
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.crypto import booking
from app.modules.crypto import history as crypto_history
from app.modules.ingest.adapters import nordnet
from app.modules.ingest.models import ImportBatch, RawRecord, ReconciliationCheck, SourceMapping
from app.modules.ledger.models import Transaction
from app.modules.ledger.service import LedgerError, TxnSpec, record
from app.platform.config import get_settings
from app.platform.db import pairs, utcnow
from app.platform.money import ZERO, D

ADAPTERS = {"nordnet": nordnet}


class ImportError_(ValueError):
    pass


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def store_file(content: bytes, sha: str, filename: str) -> None:
    root = Path(get_settings().imports_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix[:8] or ".csv"
        target = root / f"{sha}{suffix}"
        if not target.exists():
            target.write_bytes(content)
    except OSError:
        pass  # keeping the original is best effort; the rows themselves are stored in the DB


def stage(
    db: Session, filename: str, content: bytes, source: str | None = None, default_account_id: int | None = None
) -> ImportBatch:
    sha = hashlib.sha256(content).hexdigest()
    text = nordnet.decode(content)
    if source is None:
        if nordnet.sniff(text):
            source = "nordnet"
        else:
            source = crypto_history.sniff(text)
        if source is None:
            raise ImportError_(
                "This file format is not recognised. Supported: Nordnet transaction export, Kraken ledger export, "
                "Binance transaction history, Ledger Live operations, generic crypto CSV ("
                + crypto_history.GENERIC_HELP
                + ")."
            )
    try:
        if source in crypto_history.SOURCES:
            rows, meta = crypto_history.parse(source, text)
        else:
            rows, meta = ADAPTERS[source].parse(content)
    except (nordnet.FormatError, crypto_history.FormatError) as exc:
        raise ImportError_(str(exc)) from exc
    store_file(content, sha, filename)
    return stage_rows(db, source, meta, filename, sha, rows, default_account_id)


def stage_rows(
    db: Session,
    source: str,
    meta: dict[str, Any],
    filename: str,
    sha: str,
    rows: list[Any],
    default_account_id: int | None = None,
) -> ImportBatch:
    """Stage parsed rows (from a file or an API pull) as a batch of raw records."""
    batch = ImportBatch(
        source=source,
        adapter=meta["adapter"],
        filename=filename[:255],
        file_sha256=sha,
        status="staged",
        stats={"meta": meta, "default_account_id": default_account_id},
    )
    db.add(batch)
    db.flush()
    seen: set[str] = set()
    for r in rows:
        rr = RawRecord(
            batch_id=batch.id,
            row_no=r.row_no,
            source=source,
            external_id=r.external_id,
            content_hash=r.content_hash,
            raw=r.raw,
            parsed=r.parsed,
            status="new",
            message=r.error,
        )
        if r.error:
            rr.status = "error"
        elif r.external_id and r.external_id in seen:
            rr.status, rr.message = "duplicate", "Appears twice in this file"
        if r.external_id:
            seen.add(r.external_id)
        db.add(rr)
    db.flush()
    resolve_batch(db, batch)
    return batch


def _mapping(db: Session, source: str, kind: str, key: str) -> str | None:
    m = db.scalar(
        select(SourceMapping.value).where(
            SourceMapping.source == source, SourceMapping.kind == kind, SourceMapping.key == key
        )
    )
    return m


def set_mapping(db: Session, source: str, kind: str, key: str, value: str, note: str | None = None) -> None:
    m = db.scalar(
        select(SourceMapping).where(
            SourceMapping.source == source, SourceMapping.kind == kind, SourceMapping.key == key
        )
    )
    if m is None:
        db.add(SourceMapping(source=source, kind=kind, key=key, value=value, note=note))
    else:
        m.value, m.note = value, note
    db.flush()


def instrument_key(p: dict[str, Any]) -> str:
    return p.get("isin") or f"name:{(p.get('security') or '').strip().lower()}"


def resolve_batch(db: Session, batch: ImportBatch) -> None:
    accounts = db.scalars(select(Account)).all()
    by_ref = {_digits(a.external_ref): a.id for a in accounts if _digits(a.external_ref)}
    rows = db.scalars(select(RawRecord).where(RawRecord.batch_id == batch.id)).all()
    for rr in rows:
        _resolve_row(db, batch, rr, by_ref)
    _update_stats(db, batch)


def _resolve_row(db: Session, batch: ImportBatch, rr: RawRecord, by_ref: dict[str, int]) -> None:
    if rr.status in ("committed", "ignored", "error"):
        return
    if rr.status == "duplicate" and rr.message == "Appears twice in this file":
        return
    p = rr.parsed
    src = batch.source
    if src in crypto_history.SOURCES:
        _resolve_crypto_row(db, batch, rr)
        return
    if p.get("cancelled"):
        rr.status, rr.message = "ignored", f"Cancelled by Nordnet ({p.get('cancel_date')})"
        return
    # types learned after the file was staged count too
    kind = _mapping(db, src, "txn_type", p["raw_type"]) or p.get("kind") or nordnet.TYPE_TO_KIND.get(p["raw_type"])
    p = dict(p, resolved_kind=kind)
    # account
    acct: int | None = None
    portfolio = p.get("portfolio") or ""
    mapped = _mapping(db, src, "account", portfolio) if portfolio else None
    if mapped:
        acct = int(mapped)
    elif _digits(portfolio) and _digits(portfolio) in by_ref:
        acct = by_ref[_digits(portfolio)]
    elif batch.stats.get("default_account_id"):
        acct = int(batch.stats["default_account_id"])
    rr.account_id = acct
    # instrument
    inst: int | None = None
    key = instrument_key(p)
    if p.get("isin") or p.get("security"):
        m = _mapping(db, src, "instrument", key)
        if m:
            inst = int(m)
        elif p.get("isin"):
            inst = db.scalar(select(Instrument.id).where(Instrument.isin == p["isin"]))
    rr.instrument_id = inst
    rr.parsed = p
    if kind == "ignore":
        rr.status, rr.message = "ignored", f"Type {p['raw_type']} is set to be ignored"
        return
    if not kind:
        rr.status, rr.message = "needs_review", f"Unknown transaction type “{p['raw_type']}”. Choose what it means."
        return
    if acct is None:
        rr.status, rr.message = "needs_mapping", f"Which account is Nordnet portfolio {portfolio or '(blank)'}?"
        return
    if kind in nordnet.NEEDS_INSTRUMENT and inst is None:
        rr.status, rr.message = (
            "needs_mapping",
            f"Unknown instrument {p.get('security') or ''} {p.get('isin') or ''}".strip(),
        )
        return
    if rr.external_id:
        dup = db.scalar(
            select(Transaction.id).where(
                Transaction.source == src, Transaction.external_id == rr.external_id, Transaction.voided_at.is_(None)
            )
        )
        if dup:
            rr.status, rr.message, rr.transaction_id = "duplicate", "Already in the ledger", dup
            return
    rr.status, rr.message = "new", None


def _resolve_crypto_row(db: Session, batch: ImportBatch, rr: RawRecord) -> None:
    p = rr.parsed
    if p.get("event") == "internal":
        rr.status, rr.message = "ignored", "Move between wallets of the same account (no effect)"
        return
    mapped = _mapping(db, batch.source, "account", p.get("portfolio") or "")
    acct = int(mapped) if mapped else batch.stats.get("default_account_id")
    rr.account_id = int(acct) if acct else None
    if rr.account_id is None:
        rr.status, rr.message = "needs_mapping", f"Which account are these {batch.source} rows for?"
        return
    if rr.external_id:
        dup = db.scalar(
            select(Transaction.id).where(
                Transaction.source == batch.source,
                Transaction.external_id == rr.external_id,
                Transaction.voided_at.is_(None),
            )
        )
        if dup:
            rr.status, rr.message, rr.transaction_id = "duplicate", "Already in the ledger", dup
            return
    rr.status, rr.message = "new", None


def _update_stats(db: Session, batch: ImportBatch) -> None:
    counts = pairs(
        db, select(RawRecord.status, func.count()).where(RawRecord.batch_id == batch.id).group_by(RawRecord.status)
    )
    stats = dict(batch.stats)
    stats["counts"] = counts
    batch.stats = stats


def spec_from_row(db: Session, batch: ImportBatch, rr: RawRecord) -> TxnSpec:
    p = rr.parsed
    kind = p["resolved_kind"]
    amt = D(p["amount"] or "0")
    ccy = p.get("amount_ccy") or "EUR"
    fees = D(p["fees"]) if p.get("fees") and (p.get("fees_ccy") or ccy) == ccy else ZERO
    qty = D(p["quantity"]) if p.get("quantity") else ZERO
    raw_type = p["raw_type"]
    # Normalise signs: the type decides direction, the source's sign conventions vary between exports.
    if kind in ("buy", "withdrawal", "transfer_out", "fee"):
        amt = -abs(amt)
    elif kind in ("sell", "deposit", "transfer_in"):
        amt = abs(amt)
    elif kind == "tax":
        if "PALAUTUS" in raw_type or "REFUND" in raw_type or "ÅTERBET" in raw_type:
            kind, amt = "tax_refund", abs(amt)
        else:
            amt = -abs(amt)
    if kind == "split" and (re.search(r"\b(UT|POIS|OUT|OTTO)\b", raw_type) or qty < 0):
        qty = -abs(qty)
    price = D(p["price"]) if p.get("price") else None
    fx = D(p["fx_rate"]) if p.get("fx_rate") else None
    if fx is not None and fx == 0:
        fx = None
    return TxnSpec(
        account_id=rr.account_id or 0,
        date=date.fromisoformat(p["date"]),
        settle_date=date.fromisoformat(p["settle_date"]) if p.get("settle_date") else None,
        kind=kind,
        currency=ccy,
        amount=amt,
        instrument_id=rr.instrument_id,
        quantity=abs(qty) if kind not in ("split",) else qty,
        price=price,
        price_currency=p.get("price_ccy") or None,
        fx_rate=fx,
        fees=fees,
        description=" ".join(x for x in (raw_type.title(), p.get("security")) if x),
        source=batch.source,
        external_id=rr.external_id,
        seq=int(p.get("seq") or 0),
        import_batch_id=batch.id,
        raw_record_id=rr.id,
        cost_basis=D(p["purchase_value"]) if kind == "security_in" and p.get("purchase_value") else None,
    )


def commit(db: Session, batch: ImportBatch, price_client: Any | None = None) -> dict[str, Any]:
    if batch.status == "discarded":
        raise ImportError_("This import was discarded")
    resolve_batch(db, batch)
    rows = db.scalars(select(RawRecord).where(RawRecord.batch_id == batch.id, RawRecord.status == "new")).all()
    rows = sorted(rows, key=lambda r: (r.parsed["date"], int(r.parsed.get("seq") or 0), r.row_no))
    crypto = batch.source in crypto_history.SOURCES
    warnings: list[str] = []
    if crypto and rows:
        symbols = {leg["asset"] for r in rows for leg in r.parsed.get("legs", [])}
        warnings = booking.ensure_prices(db, symbols, date.fromisoformat(rows[0].parsed["date"]), price_client)
        val = booking.Valuer(db)
    done = errors = 0
    for rr in rows:
        try:
            with db.begin_nested():
                if crypto:
                    specs = booking.specs_for(db, rr, rr.account_id or 0, val, batch.source, batch.id)
                else:
                    specs = [spec_from_row(db, batch, rr)]
                txns = [record(db, sp) for sp in specs]
            rr.status, rr.transaction_id, rr.message = "committed", txns[0].id, None
            done += 1
        except (LedgerError, ValueError, KeyError, LookupError) as exc:
            rr.status, rr.message = "error", f"Could not book this row: {exc}"
            errors += 1
    batch.status = "committed" if done else batch.status
    batch.committed_at = utcnow() if done else batch.committed_at
    _update_stats(db, batch)
    accounts = {r.account_id for r in rows if r.account_id}
    if done:
        stmt = sum(_retire_statement_bookings(db, a, batch.id) for a in accounts)
        if stmt:
            warnings.append(f"Replaced {stmt} entries booked from statements with the imported history")
    if crypto:
        replaced = sum(_retire_balance_sync(db, a) for a in accounts) if done else 0
        if replaced:
            warnings.append(f"Replaced {replaced} balance-sync entries with the imported history")
        matched = booking.match_transfers(db)
        all_rows = db.scalars(select(RawRecord).where(RawRecord.batch_id == batch.id)).all()
        reported, end = booking.batch_balances(list(all_rows))
        recon = {
            a: booking.reconcile_balances(db, a, reported, end, batch.id, f"{batch.source} on {end}")
            for a in accounts
            if reported
        }
        db.flush()
        return {
            "committed": done,
            "errors": errors,
            "reconciliation": recon,
            "transfers_matched": matched,
            "warnings": warnings,
        }
    recon = {a: reconcile(db, a, batch.id) for a in accounts}
    db.flush()
    return {"committed": done, "errors": errors, "reconciliation": recon, "warnings": warnings}


def _retire_statement_bookings(db: Session, account_id: int, batch_id: int) -> int:
    """Positions booked from a statement (opening balances, corrections) stand in for the history; once the
    history itself is imported they would count every holding twice. Undoing the import brings them back."""
    txns = db.scalars(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.voided_at.is_(None),
            Transaction.source == "statement",
        )
    ).all()
    for t in txns:
        t.voided_at, t.void_reason = utcnow(), f"replaced by import #{batch_id}"
    return len(txns)


def _retire_balance_sync(db: Session, account_id: int) -> int:
    """Balance-sync entries guessed the history; once the real history is imported they must go, and the
    connection switches to checking balances instead of booking them."""
    from app.platform.connections import KINDS
    from app.platform.models import Connection

    txns = db.scalars(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.voided_at.is_(None),
            Transaction.description.like("Balance sync (%"),
            Transaction.import_batch_id.is_(None),
        )
    ).all()
    for t in txns:
        t.voided_at, t.void_reason = utcnow(), "replaced by imported history"
    for c in db.scalars(select(Connection).where(Connection.kind.in_(KINDS))):
        if str((c.config or {}).get("account_id")) == str(account_id) and (c.config or {}).get("mode") != "history":
            c.config = {**(c.config or {}), "mode": "history"}
    return len(txns)


def undo_commit(db: Session, batch_id: int) -> None:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        return
    txns = db.scalars(
        select(Transaction).where(Transaction.import_batch_id == batch_id, Transaction.voided_at.is_(None))
    ).all()
    accounts = {t.account_id for t in txns}
    for t in txns:
        t.voided_at, t.void_reason = utcnow(), "import undone"
    booking.unlink_voided(db, [t.id for t in txns])
    for t in db.scalars(select(Transaction).where(Transaction.void_reason == f"replaced by import #{batch_id}")):
        t.voided_at, t.void_reason = None, None
    for rr in db.scalars(select(RawRecord).where(RawRecord.batch_id == batch_id, RawRecord.status == "committed")):
        rr.status, rr.transaction_id = "new", None
    batch.status, batch.committed_at = "staged", None
    db.flush()
    resolve_batch(db, batch)
    if batch.source in crypto_history.SOURCES:
        return
    for a in accounts:
        reconcile(db, a, batch_id)


def reconcile(db: Session, account_id: int, batch_id: int | None = None) -> dict[str, Any]:
    """Walk the account's ledger in source order and compare running totals with the source's own
    'total quantity' and 'balance' columns. Stores the final state per instrument and cash currency."""
    txns = db.scalars(
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.voided_at.is_(None))
        .order_by(Transaction.date, Transaction.seq, Transaction.id)
    ).all()
    raw_ids = [t.raw_record_id for t in txns if t.raw_record_id]
    raws = {r.id: r for r in db.scalars(select(RawRecord).where(RawRecord.id.in_(raw_ids)))} if raw_ids else {}
    names = pairs(db, select(Instrument.id, Instrument.name))
    cash_ccy = pairs(db, select(Instrument.id, Instrument.symbol).where(Instrument.type == "cash"))
    cash_ids = set(cash_ccy)
    qty: dict[int, Decimal] = defaultdict(Decimal)
    cash: dict[str, Decimal] = defaultdict(Decimal)
    last: dict[str, tuple[int | None, str, Decimal, Decimal, int]] = {}
    row_mismatches = 0
    for t in txns:
        for p in t.postings:
            if p.account_id == account_id and p.instrument_id is not None:
                if p.instrument_id in cash_ids:
                    cash[cash_ccy[p.instrument_id] or "?"] += p.quantity
                else:
                    qty[p.instrument_id] += p.quantity
        rr = raws.get(t.raw_record_id) if t.raw_record_id else None
        if rr is None:
            continue
        parsed = rr.parsed
        inst = next(
            (
                x.instrument_id
                for x in t.postings
                if x.instrument_id and x.instrument_id not in cash_ids and x.account_id == account_id
            ),
            None,
        )
        if inst is not None and parsed.get("total_quantity") is not None:
            rep, comp = D(parsed["total_quantity"]), qty[inst]
            row_mismatches += rep != comp
            last[f"i{inst}"] = (inst, names.get(inst, "?"), rep, comp, rr.id)
        if parsed.get("balance") is not None:
            rep, comp = D(parsed["balance"]), cash[t.currency]
            row_mismatches += abs(rep - comp) > Decimal("0.01")
            last[f"c{t.currency}"] = (None, f"Cash {t.currency}", rep, comp, rr.id)
    db.execute(delete(ReconciliationCheck).where(ReconciliationCheck.account_id == account_id))
    now = utcnow()
    ok_all = True
    for inst, subject, rep, comp, rid in last.values():
        tol = Decimal("0.01") if inst is None else Decimal("0.00000001")
        ok = abs(rep - comp) <= tol
        ok_all &= ok
        db.add(
            ReconciliationCheck(
                account_id=account_id,
                batch_id=batch_id,
                as_of=now,
                subject=subject,
                instrument_id=inst,
                reported=str(rep),
                computed=str(comp),
                ok=ok,
                raw_record_id=rid,
            )
        )
    db.flush()
    return {"checks": len(last), "ok": ok_all, "row_mismatches": row_mismatches}


def create_missing_instruments(db: Session, batch: ImportBatch) -> int:
    rows = db.scalars(
        select(RawRecord).where(
            RawRecord.batch_id == batch.id, RawRecord.status == "needs_mapping", RawRecord.instrument_id.is_(None)
        )
    ).all()
    created = 0
    for rr in rows:
        p = rr.parsed
        if not (p.get("isin") or p.get("security")):
            continue
        key = instrument_key(p)
        if _mapping(db, batch.source, "instrument", key):
            continue
        inst = db.scalar(select(Instrument).where(Instrument.isin == p["isin"])) if p.get("isin") else None
        if inst is None:
            name = (p.get("security") or p.get("isin") or "Unknown").strip()
            inst = Instrument(
                type=nordnet.guess_instrument_type(name),
                name=name,
                isin=p.get("isin"),
                currency=nordnet.guess_currency(p),
                asset_class="equity",
                meta={"created_from": f"import {batch.id}"},
            )
            db.add(inst)
            db.flush()
            created += 1
        set_mapping(db, batch.source, "instrument", key, str(inst.id))
    resolve_batch(db, batch)
    return created
