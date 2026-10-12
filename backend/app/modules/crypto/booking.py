"""Book crypto events into the ledger in EUR, match transfers between own accounts, reconcile balances.

Finnish treatment (Vero: "Virtuaalivaluuttojen verotus"):
- every disposal is taxable: a sale for fiat, a swap to another crypto (stablecoins too), spending, fees paid in crypto;
- the sale price of a swap is the market value of what was received; that value is also its acquisition cost;
- FIFO per account, hankintameno-olettama allowed (handled by the tax engine like any other asset);
- staking/earn rewards and airdrops are capital income at market value when received.
Values come from fiat legs when the trade has one, otherwise from end-of-day EUR prices.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.crypto.assets import FIAT, crypto_instrument
from app.modules.crypto.history import nets
from app.modules.ingest.models import RawRecord, ReconciliationCheck
from app.modules.ledger.models import Posting, Transaction
from app.modules.ledger.service import LedgerError, TxnSpec, positions
from app.modules.pricing.models import Price
from app.modules.pricing.providers import yahoo
from app.modules.pricing.service import PriceBook, upsert_prices
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient

ZERO = Decimal(0)
CENT = Decimal("0.01")
CRYPTO_ACCOUNT_KINDS = {"crypto_exchange", "crypto_wallet"}


class Valuer:
    """EUR values for crypto units and fiat amounts on a date, from the price and FX tables."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.book = PriceBook(db, {i for (i,) in db.execute(select(Instrument.id).where(Instrument.type == "crypto"))})

    def fiat(self, ccy: str, amount: Decimal, on: date) -> Decimal:
        rate, _ = self.book.eur_per(ccy, on)
        return amount * rate

    def unit(self, symbol: str, on: date) -> Decimal:
        if symbol == "EUR":
            return Decimal(1)
        if symbol in FIAT:
            return self.fiat(symbol, Decimal(1), on)
        inst = crypto_instrument(self.db, symbol)
        p = self.book.price(inst.id, on)
        if p is None or p.date < on - timedelta(days=7):
            raise LedgerError(
                f"No EUR price for {symbol} on {on}. Run the price backfill (Settings → Jobs) or set a price "
                f"symbol for {symbol}, then commit again."
            )
        rate, _ = self.book.eur_per(p.currency, on)
        return p.close * rate


def ensure_prices(db: Session, symbols: set[str], start: date, client: Any | None = None) -> list[str]:
    """Fetch EUR price history for crypto assets that lack it from `start` on. Returns warnings."""
    warnings: list[str] = []
    for sym in sorted(symbols - FIAT):
        inst = crypto_instrument(db, sym)
        first = db.scalar(select(Price.date).where(Price.instrument_id == inst.id).order_by(Price.date).limit(1))
        if first is not None and first <= start:
            continue
        if not inst.price_symbol:
            warnings.append(f"{sym}: no price symbol")
            continue
        try:
            ccy, hist, _ = yahoo.history_with_splits(
                inst.price_symbol, start - timedelta(days=7), client or OutboundClient()
            )
        except Exception as exc:  # noqa: BLE001 - one missing price source must not stop the others
            warnings.append(f"{sym}: price history unavailable ({exc})")
            continue
        upsert_prices(
            db,
            [
                {"instrument_id": inst.id, "date": d, "close": c, "currency": ccy or "EUR", "source": "yahoo"}
                for d, c in hist
            ],
        )
    return warnings


def _alloc(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Split total by weights; the last share takes the rounding remainder so parts sum exactly."""
    keys = list(weights)
    s = sum(weights.values(), ZERO)
    out: dict[str, Decimal] = {}
    left = total
    for i, k in enumerate(keys):
        part = left if i == len(keys) - 1 else (total * weights[k] / s if s else total / len(keys)).quantize(CENT)
        out[k] = part
        left -= part
    return out


def specs_for(db: Session, rr: RawRecord, account_id: int, val: Valuer, source: str, batch_id: int) -> list[TxnSpec]:
    p = rr.parsed
    d = date.fromisoformat(p["date"])
    seq = int(p.get("seq") or 0) * 10
    desc = p.get("description") or p.get("raw_type")
    specs: list[TxnSpec] = []

    def spec(kind: str, **kw: Any) -> None:
        n = len(specs)
        specs.append(
            TxnSpec(
                account_id=account_id,
                date=d,
                kind=kind,
                currency=kw.pop("currency", "EUR"),
                description=kw.pop("description", desc),
                source=source,
                external_id=rr.external_id if n == 0 else f"{rr.external_id}#{n + 1}",
                seq=seq + n,
                import_batch_id=batch_id,
                raw_record_id=rr.id,
                **kw,
            )
        )

    def weights(side: dict[str, Decimal]) -> dict[str, Decimal]:
        """Market values split a total over several assets; a single asset needs no price."""
        return {a: Decimal(1) for a in side} if len(side) == 1 else {a: q * val.unit(a, d) for a, q in side.items()}

    def inst(sym: str) -> int:
        return crypto_instrument(db, sym).id

    def fee_disposal(sym: str, qty: Decimal) -> None:
        """A fee paid in crypto disposes of the units; their value is the (deductible) cost, proceeds are zero."""
        value = (qty * val.unit(sym, d)).quantize(CENT)
        spec("sell", amount=ZERO, fees=value, instrument_id=inst(sym), quantity=qty)

    event = p["event"]
    legs: list[dict[str, Any]] = p["legs"]
    net = nets(p)
    fee_only = {
        x["asset"]
        for x in legs
        if x.get("is_fee") and not any(y["asset"] == x["asset"] and not y.get("is_fee") for y in legs)
    }

    if event == "trade":
        fiat = {a: v for a, v in net.items() if a in FIAT and v != 0 and a not in fee_only}
        outs = {a: -v for a, v in net.items() if a not in FIAT and v < 0 and a not in fee_only}
        ins = {a: v for a, v in net.items() if a not in FIAT and v > 0}
        fiat_fees: dict[str, Decimal] = defaultdict(Decimal)
        for x in legs:
            if x["asset"] in FIAT:
                fiat_fees[x["asset"]] += Decimal(x["fee"]) + (-Decimal(x["amount"]) if x.get("is_fee") else ZERO)
        if len(fiat) > 1:
            raise LedgerError("A trade settled in two fiat currencies; book it manually")
        if fiat and ins and outs:
            raise LedgerError("A trade with fiat on one side and crypto on both; book it manually")
        if fiat:
            ((ccy, cash),) = fiat.items()
            eur = val.fiat(ccy, cash, d).quantize(CENT)
            fee_eur = val.fiat(ccy, fiat_fees.get(ccy, ZERO), d).quantize(CENT)
            side = ins if cash < 0 else outs
            if not side:
                raise LedgerError("A trade with only fiat legs")
            w = weights(side)
            parts = _alloc(eur, w)
            fparts = _alloc(fee_eur, w)
            cparts = _alloc(cash, w)
            for a, q in side.items():
                extra = {"cash_currency": ccy, "cash_quantity": cparts[a]} if ccy != "EUR" else {}
                spec(
                    "buy" if cash < 0 else "sell",
                    amount=parts[a],
                    fees=fparts[a],
                    instrument_id=inst(a),
                    quantity=q,
                    **extra,
                )
        else:
            if not ins and not outs:
                raise LedgerError("An empty trade")
            # value = what was received; one-sided rows (card purchases, dust) are valued at market
            if ins:
                value = sum((q * val.unit(a, d) for a, q in ins.items()), ZERO).quantize(CENT)
            else:
                value = sum((q * val.unit(a, d) for a, q in outs.items()), ZERO).quantize(CENT)
            if not outs:
                spec("deposit", amount=value, description=f"{desc} (paid outside the account, valued at market)")
            else:
                for a, part in _alloc(value, weights(outs)).items():
                    spec("sell", amount=part, instrument_id=inst(a), quantity=outs[a])
            if ins:
                for a, part in _alloc(value, weights(ins)).items():
                    spec("buy", amount=-part, instrument_id=inst(a), quantity=ins[a])
            else:
                spec(
                    "withdrawal", amount=-value, description=f"{desc} (received outside the account, valued at market)"
                )
        for a in sorted(fee_only):
            q = -net.get(a, ZERO)
            if q > 0:
                if a in FIAT:
                    spec("fee", amount=-q, currency=a)
                else:
                    fee_disposal(a, q)
    elif event in ("deposit", "withdrawal"):
        for x in legs:
            a, amt, fee = x["asset"], Decimal(x["amount"]), Decimal(x["fee"])
            if a in FIAT:
                if amt:
                    spec("deposit" if amt > 0 else "withdrawal", amount=amt, currency=a)
                if fee:
                    spec("fee", amount=-fee, currency=a)
                continue
            if amt > 0:
                q = amt - fee
                if q > 0:
                    spec("security_in", instrument_id=inst(a), quantity=q)
            else:
                if amt:
                    spec("security_out", instrument_id=inst(a), quantity=-amt)
                if fee:
                    fee_disposal(a, fee)
    elif event == "reward":
        for a, q in net.items():
            if q <= 0:
                continue
            if a in FIAT:
                spec("interest", amount=q, currency=a)
            else:
                value = (q * val.unit(a, d)).quantize(CENT)
                spec("reward", instrument_id=inst(a), quantity=q, cost_basis=value)
    else:
        raise LedgerError(f"{event} events are not booked")
    if not specs:
        raise LedgerError("Nothing to book")
    return specs


# ---- transfers between own accounts ---------------------------------------------------------------


def _moves(db: Session, kind: str) -> list[tuple[Transaction, Posting]]:
    crypto = {i for (i,) in db.execute(select(Instrument.id).where(Instrument.type == "crypto"))}
    rows = []
    for t in db.scalars(
        select(Transaction)
        .where(Transaction.kind == kind, Transaction.voided_at.is_(None), Transaction.link_id.is_(None))
        .order_by(Transaction.date, Transaction.id)
    ):
        p = next((x for x in t.postings if x.account_id is not None and x.instrument_id in crypto), None)
        if p is not None:
            rows.append((t, p))
    return rows


def match_transfers(db: Session, window_days: int = 3, tolerance: Decimal = Decimal("0.02")) -> int:
    """Pair a crypto withdrawal with a deposit of the same asset into another own account: same date ±window,
    received quantity equal or up to `tolerance` lower (network fee taken by the sender's wallet)."""
    ins = _moves(db, "security_in")
    used: set[int] = set()
    n = 0
    for out_t, out_p in _moves(db, "security_out"):
        q_out = -out_p.quantity
        best = None
        for in_t, in_p in ins:
            if in_t.id in used or in_t.account_id == out_t.account_id or in_p.instrument_id != out_p.instrument_id:
                continue
            gap = abs((in_t.date - out_t.date).days)
            if gap > window_days or in_p.amount != 0:
                continue
            if not (q_out * (1 - tolerance) <= in_p.quantity <= q_out):
                continue
            score = (gap, q_out - in_p.quantity)
            if best is None or score < best[0]:
                best = (score, in_t)
        if best is not None:
            in_t = best[1]
            out_t.link_id, in_t.link_id = in_t.id, out_t.id
            used.add(in_t.id)
            n += 1
    db.flush()
    return n


def link(db: Session, out_id: int, in_id: int) -> None:
    out_t, in_t = db.get(Transaction, out_id), db.get(Transaction, in_id)
    if out_t is None or in_t is None or out_t.kind != "security_out" or in_t.kind != "security_in":
        raise LedgerError("Link a transfer out (security_out) with a transfer in (security_in)")
    for t in (out_t, in_t):
        if t.link_id:
            unlink(db, t.id)
    out_t.link_id, in_t.link_id = in_t.id, out_t.id
    db.flush()


def unlink(db: Session, txn_id: int) -> None:
    t = db.get(Transaction, txn_id)
    if t is None:
        return
    if t.link_id:
        db.execute(update(Transaction).where(Transaction.id == t.link_id).values(link_id=None))
    t.link_id = None
    db.flush()


def unlink_voided(db: Session, txn_ids: list[int]) -> None:
    if txn_ids:
        db.execute(update(Transaction).where(Transaction.link_id.in_(txn_ids)).values(link_id=None))
        db.execute(update(Transaction).where(Transaction.id.in_(txn_ids)).values(link_id=None))


def open_transfers(db: Session) -> dict[str, list[dict[str, Any]]]:
    """Unmatched crypto transfers: outs whose cost has nowhere to go, ins with unknown cost (0 €)."""
    names = {a.id: a.name for a in db.scalars(select(Account))}
    syms = {i.id: i.symbol or i.name for i in db.scalars(select(Instrument).where(Instrument.type == "crypto"))}

    def row(t: Transaction, p: Posting) -> dict[str, Any]:
        return {
            "id": t.id,
            "date": t.date,
            "account": names.get(t.account_id),
            "asset": syms.get(p.instrument_id or 0),
            "quantity": abs(p.quantity),
            "description": t.description,
        }

    return {
        "out": [row(t, p) for t, p in _moves(db, "security_out")],
        "in": [row(t, p) for t, p in _moves(db, "security_in") if p.amount == 0],
    }


# ---- reconciliation -------------------------------------------------------------------------------


def reconcile_balances(
    db: Session, account_id: int, reported: dict[str, Decimal], on: date | None, batch_id: int | None, label: str
) -> dict[str, Any]:
    """Compare reported balances per asset with the ledger; stores the result like Nordnet's checks."""
    held: dict[str, Decimal] = defaultdict(Decimal)
    syms = {
        i.id: (i.symbol or i.name)
        for i in db.scalars(select(Instrument).where(Instrument.type.in_(["crypto", "cash"])))
    }
    for pos in positions(db, on=on, account_ids=[account_id]):
        if pos.instrument_id in syms:
            held[syms[pos.instrument_id]] += pos.quantity
    db.execute(delete(ReconciliationCheck).where(ReconciliationCheck.account_id == account_id))
    ok_all = True
    now = utcnow()
    for sym in sorted(set(reported) | {k for k, v in held.items() if v}):
        rep, comp = reported.get(sym, ZERO), held.get(sym, ZERO)
        tol = CENT if sym in FIAT else Decimal("0.00000001")
        ok = abs(rep - comp) <= tol
        ok_all &= ok
        db.add(
            ReconciliationCheck(
                account_id=account_id,
                batch_id=batch_id,
                as_of=now,
                subject=f"{sym} ({label})",
                reported=str(rep),
                computed=str(comp),
                ok=ok,
            )
        )
    db.flush()
    return {"checks": len(set(reported) | set(held)), "ok": ok_all}


def batch_balances(rows: list[RawRecord]) -> tuple[dict[str, Decimal], date | None]:
    """Kraken reports the balance after each entry per wallet: take the last per wallet, sum per asset."""
    from app.modules.crypto.providers import kraken_asset

    last: dict[str, tuple[str, str]] = {}
    end: date | None = None
    for rr in sorted(rows, key=lambda r: (r.parsed.get("time") or "", r.row_no)):
        for k, v in (rr.parsed.get("balances") or {}).items():
            last[k] = (rr.parsed["time"], v)
        if rr.parsed.get("date"):
            end = max(end, date.fromisoformat(rr.parsed["date"])) if end else date.fromisoformat(rr.parsed["date"])
    out: dict[str, Decimal] = defaultdict(Decimal)
    for k, (_, v) in last.items():
        asset = kraken_asset(k.split("|")[0])
        if asset != "KFEE":
            out[asset] += Decimal(v)
    return dict(out), end
