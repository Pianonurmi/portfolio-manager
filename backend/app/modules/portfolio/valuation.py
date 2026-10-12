"""Valuation over time for a scope (household or one owner), shared by portfolio and net worth.

value(d) = Σ quantity(acct, inst, d) × EUR per unit(inst, d) × ownership share(acct, owner, d)
External flows (money or securities entering/leaving the scope) are kept separately so returns
exclude them (PORT-03).
"""

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Ownership
from app.modules.ledger.models import Posting, Transaction
from app.modules.pricing.service import PriceBook, PricePoint

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True)
class Scope:
    entity_id: int | None  # None = household (everyone, 100 % of every account)

    @property
    def token(self) -> str:
        return "household" if self.entity_id is None else f"e{self.entity_id}"

    @staticmethod
    def parse(s: str | None) -> "Scope":
        s = (s or "household").strip()
        if s in ("", "household"):
            return Scope(None)
        if s.startswith("entity:") and s[7:].isdigit():
            return Scope(int(s[7:]))
        if s.startswith("e") and s[1:].isdigit():
            return Scope(int(s[1:]))
        raise ValueError(f"bad scope {s!r}")


class Shares:
    """Dated ownership shares for the scope's owner."""

    def __init__(self, db: Session, scope: Scope, account_ids: list[int]) -> None:
        self.scope = scope
        self.rows: dict[int, list[tuple[date, date | None, Decimal]]] = defaultdict(list)
        if scope.entity_id is not None:
            for o in db.scalars(
                select(Ownership).where(Ownership.entity_id == scope.entity_id, Ownership.account_id.in_(account_ids))
            ):
                self.rows[o.account_id].append((o.valid_from, o.valid_to, o.share))

    def on(self, account_id: int, d: date) -> Decimal:
        if self.scope.entity_id is None:
            return ONE
        for start, end, share in self.rows.get(account_id, []):
            if start <= d and (end is None or d <= end):
                return share
        return ZERO

    def accounts(self) -> set[int]:
        return set(self.rows)


@dataclass
class Holding:
    account_id: int
    instrument_id: int
    quantity: Decimal
    share: Decimal
    unit_eur: Decimal
    price: PricePoint | None
    fx: Decimal
    value: Decimal  # EUR, scope share applied


class Valuer:
    def __init__(
        self, db: Session, scope: Scope, kinds: set[str] | None = None, account_ids: list[int] | None = None
    ) -> None:
        self.db = db
        self.scope = scope
        q = select(Account)
        if kinds is not None:
            q = q.where(Account.kind.in_(kinds))
        if account_ids is not None:
            q = q.where(Account.id.in_(account_ids))
        self.accounts = {a.id: a for a in db.scalars(q)}
        self.shares = Shares(db, scope, list(self.accounts))
        if scope.entity_id is not None:
            owned = self.shares.accounts()
            self.accounts = {k: v for k, v in self.accounts.items() if k in owned}
        ids = list(self.accounts) or [-1]
        rows = db.execute(
            select(
                Transaction.id,
                Transaction.date,
                Transaction.kind,
                Transaction.currency,
                Transaction.account_id,
                Posting.account_id,
                Posting.instrument_id,
                Posting.quantity,
                Posting.amount,
                Posting.role,
            )
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(Transaction.voided_at.is_(None), Transaction.account_id.in_(ids))
            .order_by(Transaction.date, Transaction.seq, Transaction.id)
        ).all()
        self.moves: list[tuple[date, int, int, Decimal]] = []  # (date, acct, inst, qty)
        self.ext: list[tuple[date, str, int, Decimal, str]] = []  # (date, kind, txn acct, amount, ccy)
        self.sec_moves: list[tuple[date, int, int, Decimal]] = []  # security transfers (valued at market)
        instruments: set[int] = set()
        for _tid, d, kind, ccy, tacct, pacct, inst, qty, amount, role in rows:
            if pacct is not None and inst is not None and qty != 0:
                self.moves.append((d, pacct, inst, qty))
                instruments.add(inst)
                if kind in ("security_in", "security_out"):
                    self.sec_moves.append((d, pacct, inst, qty))
            elif pacct is None and role == "external" and kind not in ("security_in", "security_out"):
                self.ext.append((d, kind, tacct, amount, ccy))
        self.book = PriceBook(db, instruments)
        self.first_date: date | None = self.moves[0][0] if self.moves else None

    # ---- point in time ----------------------------------------------------------------------

    def quantities(self, on: date) -> dict[tuple[int, int], Decimal]:
        q: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        for d, acct, inst, qty in self.moves:
            if d > on:
                break
            q[(acct, inst)] += qty
        return {k: v for k, v in q.items() if v != 0}

    def holdings(self, on: date) -> list[Holding]:
        out = []
        for (acct, inst), qty in self.quantities(on).items():
            share = self.shares.on(acct, on)
            if share == 0:
                continue
            unit, pp, fx = self.book.unit_value_eur(inst, on)
            out.append(Holding(acct, inst, qty, share, unit, pp, fx, qty * unit * share))
        return out

    # ---- series ---------------------------------------------------------------------------------

    def daily(self, start: date, end: date) -> tuple[list[date], list[Decimal], list[Decimal]]:
        """Daily (dates, values, external flows) in EUR for [start, end]. Flows are counted on their day."""
        if self.first_date is None or end < start:
            return [], [], []
        qty: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        mi = 0
        moves = self.moves
        # quantities before start
        while mi < len(moves) and moves[mi][0] < start:
            _, a, i, q = moves[mi]
            qty[(a, i)] += q
            mi += 1
        flows_by_day: dict[date, Decimal] = defaultdict(Decimal)
        for d, _kind, acct, amount, ccy in self.ext:
            if start <= d <= end:
                fx, _ = self.book.eur_per(ccy, d)
                flows_by_day[d] += -amount * fx * self.shares.on(acct, d)
        for d, acct, inst, q in self.sec_moves:
            if start <= d <= end:
                unit, _, _ = self.book.unit_value_eur(inst, d)
                flows_by_day[d] += q * unit * self.shares.on(acct, d)
        dates: list[date] = []
        values: list[Decimal] = []
        flows: list[Decimal] = []
        d = start
        while d <= end:
            while mi < len(moves) and moves[mi][0] <= d:
                _, a, i, q = moves[mi]
                qty[(a, i)] += q
                mi += 1
            v = ZERO
            for (a, i), q in qty.items():
                if q == 0:
                    continue
                s = self.shares.on(a, d)
                if s == 0:
                    continue
                unit, _, _ = self.book.unit_value_eur(i, d)
                v += q * unit * s
            dates.append(d)
            values.append(v)
            flows.append(flows_by_day.get(d, ZERO))
            d += timedelta(days=1)
        return dates, values, flows

    def income(self, start: date, end: date) -> list[dict[str, Any]]:
        """Dividend and interest postings in the period, EUR, with tax withheld."""
        ids = list(self.accounts) or [-1]
        rows = self.db.execute(
            select(
                Transaction.id,
                Transaction.date,
                Transaction.kind,
                Transaction.currency,
                Transaction.account_id,
                Posting.role,
                Posting.amount,
                Posting.instrument_id,
            )
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.voided_at.is_(None),
                Transaction.account_id.in_(ids),
                Transaction.date >= start,
                Transaction.date <= end,
                Posting.account_id.is_(None),
                or_(
                    Posting.role == "income",
                    (Posting.role == "tax") & Transaction.kind.in_(["dividend", "interest", "tax", "tax_refund"]),
                ),
            )
        ).all()
        out = []
        for tid, d, kind, ccy, acct, role, amount, inst in rows:
            fx, _ = self.book.eur_per(ccy, d)
            s = self.shares.on(acct, d)
            out.append(
                {
                    "txn": tid,
                    "date": d,
                    "kind": kind,
                    "role": role,
                    "account_id": acct,
                    "instrument_id": inst,
                    "eur": -amount * fx * s if role == "income" else -amount * fx * s,
                }
            )
        return out


def sample_index(dates: list[date], every: int) -> list[int]:
    idx = list(range(0, len(dates), every))
    if dates and idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    return idx


def index_on_or_before(dates: list[date], d: date) -> int:
    return max(0, bisect.bisect_right(dates, d) - 1)
