"""Turn reported balances into ledger moves so crypto is valued like any holding."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.crypto import booking, history, providers
from app.modules.crypto.assets import FIAT, crypto_instrument
from app.modules.ledger.service import TxnSpec, cash_instrument, positions, record
from app.modules.pricing.service import PriceBook
from app.platform.connections import get_connection, secret
from app.platform.db import today, utcnow
from app.platform.jobs import JobContext
from app.platform.models import Connection


def apply_balances(
    db: Session, account: Account, balances: dict[str, Decimal], source: str, on: date | None = None
) -> int:
    """Book reported-minus-ledger quantities as security moves at market value (a flow, not a gain)."""
    d = on or today()
    held = {p.instrument_id: p.quantity for p in positions(db, account_ids=[account.id])}
    wanted: dict[int, Decimal] = {}
    for sym, qty in balances.items():
        sym = sym.upper()
        if sym in FIAT:
            wanted[cash_instrument(db, sym).id] = qty
        else:
            wanted[crypto_instrument(db, sym).id] = qty
    for inst_id in held:
        wanted.setdefault(inst_id, Decimal(0))
    book = PriceBook(db, set(wanted))
    n = 0
    for inst_id, qty in wanted.items():
        diff = qty - held.get(inst_id, Decimal(0))
        if diff == 0:
            continue
        inst = db.get(Instrument, inst_id)
        assert inst is not None
        if inst.type == "cash":
            kind, amount = ("deposit" if diff > 0 else "withdrawal"), diff
            spec = TxnSpec(
                account_id=account.id,
                date=d,
                kind=kind,
                currency=inst.symbol or "EUR",
                amount=amount,
                description=f"Balance sync ({source})",
                source=source,
                external_id=f"{source}:{account.id}:{inst_id}:{d}:{held.get(inst_id, 0)}",
            )
        else:
            unit, _, _ = book.unit_value_eur(inst_id, d)
            spec = TxnSpec(
                account_id=account.id,
                date=d,
                kind="security_in" if diff > 0 else "security_out",
                currency="EUR",
                instrument_id=inst_id,
                quantity=abs(diff),
                cost_basis=(abs(diff) * unit).quantize(Decimal("0.01")) if diff > 0 else None,
                description=f"Balance sync ({source})",
                source=source,
                external_id=f"{source}:{account.id}:{inst_id}:{d}:{held.get(inst_id, 0)}",
            )
        record(db, spec)
        n += 1
    return n


def sync_all(db: Session, ctx: JobContext | None = None, clients: dict[str, Any] | None = None) -> dict[str, int]:
    """Balance mode books the difference to reported balances. History mode (switched on by importing the
    transaction history) pulls new Kraken ledger entries and only compares balances for the others."""
    stats: dict[str, int] = {}
    clients = clients or {}
    for kind in ("kraken", "binance", "ledger"):
        conn = get_connection(db, kind)
        if conn is None:
            continue
        acct_id = (conn.config or {}).get("account_id")
        account = db.get(Account, int(acct_id)) if acct_id else None
        if account is None:
            if ctx:
                ctx.warn(f"{kind}: choose the account its balances belong to")
            continue
        history = (conn.config or {}).get("mode") == "history"
        try:
            if kind == "kraken" and history:
                stats[kind] = pull_kraken_history(db, conn, account, clients.get(kind), clients.get("prices"), ctx)
            if kind == "kraken":
                bal = providers.kraken_balances(str(conn.config.get("api_key")), secret(conn) or "", clients.get(kind))
            elif kind == "binance":
                bal = providers.binance_balances(str(conn.config.get("api_key")), secret(conn) or "", clients.get(kind))
            else:
                bal = providers.address_balances(list(conn.config.get("addresses") or []), clients.get(kind))
            if history:
                r = booking.reconcile_balances(db, account.id, bal, None, None, f"{kind} now")
                stats.setdefault(kind, 0)
                if not r["ok"] and ctx:
                    ctx.warn(f"{kind}: balances differ from the imported history; see the account's reconciliation")
            else:
                stats[kind] = apply_balances(db, account, bal, kind)
            conn.status, conn.error = "ok", None
            if ctx:
                ctx.log(
                    f"{kind}: {len(bal)} assets, {stats[kind]} entries booked "
                    f"({'history' if history else 'balance'} mode)"
                )
        except Exception as exc:  # noqa: BLE001 - one failing source must not stop the others
            conn.status, conn.error = "error", str(exc)[:500]
            if ctx:
                ctx.warn(f"{kind}: {exc}")
    return stats


def pull_kraken_history(
    db: Session,
    conn: Connection,
    account: Account,
    client: Any | None = None,
    price_client: Any | None = None,
    ctx: JobContext | None = None,
) -> int:
    """New ledger entries since the last pull → staged batch (raw-first) → committed. Idempotent by refid."""
    from app.modules.ingest.service import commit, stage_rows

    since = float((conn.config or {}).get("ledger_cursor") or 0)
    entries = providers.kraken_ledgers(str(conn.config.get("api_key")), secret(conn) or "", since, client)
    if not entries:
        return 0
    rows = history.kraken_events(entries)
    batch = stage_rows(
        db,
        "kraken",
        {"adapter": "kraken-ledgers-api v1", "rows": len(rows)},
        f"Kraken API {utcnow():%Y-%m-%d %H:%M}",
        hashlib.sha256(json.dumps(entries, sort_keys=True, default=str).encode()).hexdigest(),
        rows,
        account.id,
    )
    res = commit(db, batch, price_client)
    # step back a day: entries of a refid can straddle the cursor; refids already booked are duplicates
    conn.config = {**(conn.config or {}), "ledger_cursor": max(float(e["time"]) for e in entries) - 86400}
    if ctx and res["errors"]:
        ctx.warn(f"kraken: {res['errors']} ledger entries could not be booked; open import #{batch.id}")
    return int(res["committed"])
