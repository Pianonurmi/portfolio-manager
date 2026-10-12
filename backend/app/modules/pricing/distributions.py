"""Dividends and fund distributions per unit: Yahoo's ex-date records, else derived from the user's own bookings.

Both feed the total-return series of the risk report and the income forecast. Accumulating funds simply have none.
"""

import bisect
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ledger.models import Posting, Transaction
from app.modules.pricing.models import Distribution
from app.modules.pricing.service import PriceBook

ZERO = Decimal(0)
UNIT_PLACES = Decimal("0.00000001")


@dataclass(frozen=True)
class Dist:
    """One distribution: `amount` per unit in `currency` on `date` (ex-date for Yahoo, booking date for the ledger)."""

    date: date
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class DistSeries:
    source: str  # yahoo | ledger
    rows: list[Dist]


def yahoo_distributions(db: Session, instrument_ids: Iterable[int]) -> dict[int, list[Dist]]:
    out: dict[int, list[Dist]] = defaultdict(list)
    ids = list(instrument_ids)
    if not ids:
        return {}
    q = (
        select(Distribution.instrument_id, Distribution.ex_date, Distribution.amount, Distribution.currency)
        .where(Distribution.instrument_id.in_(ids), Distribution.source == "yahoo")
        .order_by(Distribution.ex_date)
    )
    for inst, d, amount, ccy in db.execute(q).all():
        out[inst].append(Dist(d, amount, ccy))
    return dict(out)


def ledger_distributions(db: Session, instrument_ids: Iterable[int]) -> dict[int, list[tuple[date, Decimal]]]:
    """Per-unit dividends (EUR) from the user's own `dividend` bookings: gross amount ÷ units held on that day.

    The booking date stands in for the ex-date. Bookings of one instrument on one day (several accounts) are pooled;
    a booking with no units held is skipped, since a per-unit amount cannot be told.
    """
    ids = list(instrument_ids)
    if not ids:
        return {}
    gross: dict[tuple[int, date], tuple[Decimal, set[int]]] = {}
    divs = db.execute(
        select(Transaction.date, Transaction.currency, Transaction.account_id, Posting.instrument_id, Posting.amount)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Transaction.voided_at.is_(None),
            Transaction.kind == "dividend",
            Posting.role == "income",
            Posting.account_id.is_(None),
            Posting.instrument_id.in_(ids),
        )
    ).all()
    if not divs:
        return {}
    book = PriceBook(db, set())
    for d, ccy, acct, inst, amount in divs:
        try:
            fx, _ = book.eur_per(ccy, d)
        except LookupError:
            continue
        eur, accts = gross.get((inst, d), (ZERO, set()))
        gross[(inst, d)] = (eur - amount * fx, accts | {acct})
    # units held per (account, instrument) over time, from the ledger's own postings
    moves: dict[tuple[int, int], list[tuple[date, Decimal]]] = defaultdict(list)
    q = (
        select(Posting.account_id, Posting.instrument_id, Transaction.date, Posting.quantity)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Transaction.voided_at.is_(None),
            Posting.account_id.is_not(None),
            Posting.instrument_id.in_(ids),
            Posting.quantity != 0,
        )
        .order_by(Transaction.date)
    )
    for a, i, d, qty in db.execute(q).all():
        moves[(a, i)].append((d, qty))
    cum: dict[tuple[int, int], tuple[list[date], list[Decimal]]] = {}
    for key, ms in moves.items():
        total = ZERO
        running = []
        for _, qty in ms:
            total += qty
            running.append(total)
        cum[key] = ([d for d, _ in ms], running)

    def held(acct: int, inst: int, on: date) -> Decimal:
        if (acct, inst) not in cum:
            return ZERO
        ds, running = cum[(acct, inst)]
        k = bisect.bisect_right(ds, on) - 1
        return running[k] if k >= 0 else ZERO

    out: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
    for (inst, d), (eur, accts) in sorted(gross.items(), key=lambda kv: kv[0][1]):
        units = sum((max(held(a, inst, d), ZERO) for a in accts), ZERO)
        if units > 0 and eur > 0:
            out[inst].append((d, (eur / units).quantize(UNIT_PLACES)))
    return dict(out)


def ledger_dividend_instruments(db: Session, instrument_ids: Iterable[int]) -> set[int]:
    """Instruments that have any dividend booking, whether or not a per-unit amount could be derived."""
    ids = list(instrument_ids)
    if not ids:
        return set()
    q = (
        select(Posting.instrument_id)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Transaction.voided_at.is_(None),
            Transaction.kind == "dividend",
            Posting.role == "income",
            Posting.instrument_id.in_(ids),
        )
        .distinct()
    )
    return {i for (i,) in db.execute(q).all() if i is not None}


def distribution_series(db: Session, instrument_ids: Iterable[int]) -> dict[int, DistSeries]:
    """Per instrument that has any: Yahoo's distributions, else the ledger-derived ones (in EUR)."""
    ids = list(dict.fromkeys(instrument_ids))
    out = {i: DistSeries("yahoo", rows) for i, rows in yahoo_distributions(db, ids).items() if rows}
    rest = [i for i in ids if i not in out]
    for i, rows in ledger_distributions(db, rest).items():
        if rows:
            out[i] = DistSeries("ledger", [Dist(d, a, "EUR") for d, a in rows])
    return out


def trailing_per_unit_eur(series: DistSeries, book: PriceBook, end: date, days: int = 365) -> Decimal:
    """Σ distributions per unit in (end − days, end], in EUR at each payment's own date."""
    total = ZERO
    for x in series.rows:
        if end - timedelta(days=days) < x.date <= end:
            try:
                fx, _ = book.eur_per(x.currency, x.date)
            except LookupError:
                continue
            total += x.amount * fx
    return total
