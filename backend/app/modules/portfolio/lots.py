"""FIFO tax lots per account and instrument (PORT-02, input for TAX-01).

Finnish rules: acquisition cost includes purchase fees; disposals consume the oldest lots first within
a book-entry account; sale costs reduce proceeds. Splits change quantity, not cost. Securities moved
between own accounts keep their acquisition date and cost: a linked pair (`link_id`) moves exactly the
lots that left, in either booking order; unlinked moves fall back to "first in transit" per instrument.
Rewards received in kind (staking) are acquisitions at their market value.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ledger.models import Transaction

ZERO = Decimal(0)


@dataclass
class Lot:
    account_id: int
    instrument_id: int
    acquired: date
    quantity: Decimal
    cost: Decimal  # in transaction currency of the purchase (EUR for EUR accounts)
    currency: str
    transaction_id: int

    @property
    def unit_cost(self) -> Decimal:
        return self.cost / self.quantity if self.quantity else ZERO


@dataclass
class Disposal:
    account_id: int
    instrument_id: int
    sold: date
    acquired: date
    quantity: Decimal
    proceeds: Decimal  # net of sale costs, allocated pro rata
    cost: Decimal
    currency: str
    transaction_id: int
    lot_transaction_id: int
    gross: Decimal = ZERO  # sale price before costs (basis for hankintameno-olettama)

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost


@dataclass
class LotBook:
    open: dict[tuple[int, int], list[Lot]] = field(default_factory=lambda: defaultdict(list))
    disposals: list[Disposal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lots(self, account_id: int, instrument_id: int) -> list[Lot]:
        return self.open.get((account_id, instrument_id), [])

    def cost_basis(self, account_id: int, instrument_id: int) -> Decimal:
        return sum((lot.cost for lot in self.lots(account_id, instrument_id)), ZERO)


def _consume(
    book: LotBook, key: tuple[int, int], qty: Decimal, when: date, txn: int
) -> list[tuple[Lot, Decimal, Decimal]]:
    """Remove qty FIFO; returns [(lot, qty_taken, cost_taken)]."""
    taken: list[tuple[Lot, Decimal, Decimal]] = []
    lots = book.open[key]
    left = qty
    while left > 0 and lots:
        lot = lots[0]
        q = min(left, lot.quantity)
        c = lot.cost * q / lot.quantity if lot.quantity else ZERO
        taken.append((lot, q, c))
        lot.quantity -= q
        lot.cost -= c
        left -= q
        if lot.quantity <= 0:
            lots.pop(0)
    if left > Decimal("0.00000001"):
        book.warnings.append(
            f"txn {txn} on {when}: sold {left} more units than held (account {key[0]}, instrument {key[1]})"
        )
    return taken


def build(db: Session, account_ids: list[int] | None = None, until: date | None = None) -> LotBook:
    cash = {i for (i,) in db.execute(select(Instrument.id).where(Instrument.type == "cash")).all()}
    q = (
        select(Transaction)
        .where(Transaction.voided_at.is_(None))
        .order_by(Transaction.date, Transaction.seq, Transaction.id)
    )
    if account_ids is not None:
        q = q.where(Transaction.account_id.in_(account_ids))
    if until is not None:
        q = q.where(Transaction.date <= until)
    book = LotBook()
    carried: dict[tuple[int, date], list[tuple[date, Decimal, Decimal, int]]] = defaultdict(list)  # split legs
    moved_out: dict[int, list[tuple[date, Decimal, Decimal, int]]] = defaultdict(list)  # instrument -> lots in transit
    linked_out: dict[int, list[tuple[date, Decimal, Decimal, int]]] = {}  # out txn -> its lots, awaiting the in-leg
    pending_in: dict[int, list[tuple[tuple[int, int], Decimal, date, str, int]]] = defaultdict(list)  # out txn -> ins

    def receive(
        key: tuple[int, int], transit: list[tuple[date, Decimal, Decimal, int]], need: Decimal, cur: str
    ) -> Decimal:
        while need > 0 and transit:
            acq, q_old, c, lt = transit[0]
            take = min(need, q_old)
            book.open[key].append(Lot(key[0], key[1], acq, take, c * take / q_old, cur, lt))
            if take == q_old:
                transit.pop(0)
            else:
                transit[0] = (acq, q_old - take, c - c * take / q_old, lt)
            need -= take
        book.open[key].sort(key=lambda lot: lot.acquired)
        return need

    for t in db.scalars(q):
        fees = sum((p.amount for p in t.postings if p.role in ("fee", "tax") and p.account_id is None), ZERO)
        assets = [
            p
            for p in t.postings
            if p.account_id is not None and p.instrument_id is not None and p.instrument_id not in cash
        ]
        for p in assets:
            assert p.account_id is not None and p.instrument_id is not None
            key: tuple[int, int] = (p.account_id, p.instrument_id)
            if t.kind in ("buy", "reward"):
                book.open[key].append(
                    Lot(p.account_id, p.instrument_id, t.date, p.quantity, p.amount + fees, t.currency, t.id)
                )
            elif t.kind == "sell":
                qty = -p.quantity
                proceeds = -p.amount - fees
                total_q = qty
                for lot, q_taken, c in _consume(book, key, qty, t.date, t.id):
                    book.disposals.append(
                        Disposal(
                            p.account_id,
                            p.instrument_id,
                            t.date,
                            lot.acquired,
                            q_taken,
                            proceeds * q_taken / total_q,
                            c,
                            t.currency,
                            t.id,
                            lot.transaction_id,
                            gross=-p.amount * q_taken / total_q,
                        )
                    )
            elif t.kind == "split":
                if p.quantity < 0:
                    for lot, q_taken, c in _consume(book, key, -p.quantity, t.date, t.id):
                        carried[(p.instrument_id, t.date)].append((lot.acquired, q_taken, c, lot.transaction_id))
                else:
                    legs = carried.pop((p.instrument_id, t.date), [])
                    if legs:  # out-leg + in-leg: rebuild lots with the new quantity, same dates and cost
                        old_q = sum((x[1] for x in legs), ZERO)
                        for acq, q_old, c, lt in legs:
                            book.open[key].append(
                                Lot(p.account_id, p.instrument_id, acq, p.quantity * q_old / old_q, c, t.currency, lt)
                            )
                        book.open[key].sort(key=lambda lot: lot.acquired)
                    else:  # single row: scale existing lots
                        held = sum((lot.quantity for lot in book.open[key]), ZERO)
                        if held > 0:
                            k = (held + p.quantity) / held
                            for lot in book.open[key]:
                                lot.quantity *= k
            elif t.kind == "security_out":
                legs = [
                    (lot.acquired, q_taken, c, lot.transaction_id)
                    for lot, q_taken, c in _consume(book, key, -p.quantity, t.date, t.id)
                ]
                if t.link_id is None:
                    moved_out[p.instrument_id].extend(legs)
                    continue
                for in_key, need, in_date, cur, in_txn in pending_in.pop(t.id, []):
                    left = receive(in_key, legs, need, cur)
                    if left > 0:
                        book.open[in_key].append(Lot(in_key[0], in_key[1], in_date, left, ZERO, cur, in_txn))
                linked_out[t.id] = legs
            elif t.kind == "security_in":
                if t.link_id is not None:
                    if t.link_id in linked_out:
                        left = receive(key, linked_out[t.link_id], p.quantity, t.currency)
                        if left > 0:  # more arrived than left: the rest has no known cost
                            book.open[key].append(
                                Lot(p.account_id, p.instrument_id, t.date, left, ZERO, t.currency, t.id)
                            )
                    else:  # booked before its out-leg (time zones, settlement): wait for it
                        pending_in[t.link_id].append((key, p.quantity, t.date, t.currency, t.id))
                    continue
                transit = moved_out.get(p.instrument_id) or []
                need = p.quantity
                if transit and p.amount == 0:  # own transfer: keep original dates and cost
                    need = receive(key, transit, need, t.currency)
                if need > 0:
                    book.open[key].append(Lot(p.account_id, p.instrument_id, t.date, need, p.amount, t.currency, t.id))
    for waiting in pending_in.values():  # out-leg outside the scope or the period: units with unknown cost
        for in_key, need, in_date, cur, in_txn in waiting:
            book.open[in_key].append(Lot(in_key[0], in_key[1], in_date, need, ZERO, cur, in_txn))
            book.warnings.append(f"txn {in_txn}: transfer in arrived without its out-leg; cost unknown")
    return book
