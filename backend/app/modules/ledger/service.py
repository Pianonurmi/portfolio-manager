"""Building balanced transactions and reading positions."""

import datetime as dt
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ledger.models import Posting, Transaction
from app.platform.money import ZERO


class LedgerError(ValueError):
    pass


@dataclass
class TxnSpec:
    """Source-independent description of one transaction. All amounts are in `currency` (account currency).

    amount: signed cash effect on the account (negative = cash out), as reported by the source.
    fees: positive fee amount included in `amount`.
    tax: positive tax amount included in `amount` (withholding booked on the same row).
    """

    account_id: int
    date: dt.date
    kind: str
    currency: str
    amount: Decimal = ZERO
    instrument_id: int | None = None
    quantity: Decimal = ZERO  # absolute units for buy/sell; signed for split/security_in/out
    price: Decimal | None = None
    price_currency: str | None = None
    fx_rate: Decimal | None = None
    fees: Decimal = ZERO
    tax: Decimal = ZERO
    settle_date: dt.date | None = None
    description: str | None = None
    source: str = "manual"
    external_id: str | None = None
    seq: int = 0
    import_batch_id: int | None = None
    raw_record_id: int | None = None
    cost_basis: Decimal | None = None  # for security_in: acquisition cost carried over; for reward: market value
    # Cash leg in another currency than the transaction (e.g. a USD-settled crypto trade booked in EUR):
    # the cash posting moves `cash_quantity` units of `cash_currency` and carries `amount` in `currency`.
    cash_currency: str | None = None
    cash_quantity: Decimal | None = None
    extra: dict[str, str] = field(default_factory=dict)


def cash_instrument(db: Session, currency: str) -> Instrument:
    inst = db.scalar(select(Instrument).where(Instrument.type == "cash", Instrument.symbol == currency))
    if inst is None:
        inst = Instrument(type="cash", name=f"Cash {currency}", symbol=currency, currency=currency, asset_class="cash")
        db.add(inst)
        db.flush()
    return inst


def build_postings(db: Session, s: TxnSpec) -> list[Posting]:
    """Turn a spec into postings that sum to zero. Raises LedgerError on inconsistent input."""
    cash = cash_instrument(db, s.currency).id
    P = Posting
    amt, fees, tax = s.amount, abs(s.fees), abs(s.tax)
    k = s.kind
    out: list[Posting] = []

    def cash_leg(v: Decimal) -> None:
        if v == 0:
            return
        if s.cash_currency and s.cash_currency != s.currency:
            if s.cash_quantity is None or (s.cash_quantity < 0) != (v < 0):
                raise LedgerError("cash_quantity must have the same sign as amount")
            inst_id = cash_instrument(db, s.cash_currency).id
            out.append(
                P(account_id=s.account_id, instrument_id=inst_id, quantity=s.cash_quantity, amount=v, role="asset")
            )
        else:
            out.append(P(account_id=s.account_id, instrument_id=cash, quantity=v, amount=v, role="asset"))

    def need_instrument() -> int:
        if s.instrument_id is None:
            raise LedgerError(f"{k} needs an instrument")
        return s.instrument_id

    if k in ("buy", "sell"):
        inst = need_instrument()
        q = abs(s.quantity)
        if q == 0:
            raise LedgerError(f"{k} with zero quantity")
        if k == "buy":
            # amount is negative: −(principal + fees + tax)
            principal = -amt - fees - tax
            out.append(
                P(
                    account_id=s.account_id,
                    instrument_id=inst,
                    quantity=q,
                    amount=principal,
                    role="asset",
                    price=s.price,
                    price_currency=s.price_currency,
                    fx_rate=s.fx_rate,
                )
            )
        else:
            principal = amt + fees + tax  # gross proceeds
            out.append(
                P(
                    account_id=s.account_id,
                    instrument_id=inst,
                    quantity=-q,
                    amount=-principal,
                    role="asset",
                    price=s.price,
                    price_currency=s.price_currency,
                    fx_rate=s.fx_rate,
                )
            )
        cash_leg(amt)
        if fees:
            out.append(P(amount=fees, role="fee"))
        if tax:
            out.append(P(amount=tax, role="tax"))
    elif k in ("dividend", "interest", "capital_return"):
        # amount = net cash received; gross = net + tax withheld on the same row
        gross = amt + tax + fees
        cash_leg(amt)
        out.append(
            P(amount=-gross, role="income" if k != "capital_return" else "equity", instrument_id=s.instrument_id)
        )
        if tax:
            out.append(P(amount=tax, role="tax", instrument_id=s.instrument_id))
        if fees:
            out.append(P(amount=fees, role="fee"))
    elif k in ("fee", "tax", "tax_refund"):
        cash_leg(amt)
        out.append(P(amount=-amt, role="fee" if k == "fee" else "tax", instrument_id=s.instrument_id))
    elif k in ("deposit", "withdrawal", "transfer_in", "transfer_out", "adjustment", "other"):
        cash_leg(amt)
        if amt != 0:
            out.append(P(amount=-amt, role="external"))
    elif k == "fx":
        # one row per currency leg; the other leg arrives as its own row/transaction
        cash_leg(amt)
        out.append(P(amount=-amt, role="external"))
    elif k == "reward":
        # received in kind: the market value is both income and the acquisition cost of the new units
        inst = need_instrument()
        q = abs(s.quantity)
        if q == 0:
            raise LedgerError("reward with zero quantity")
        value = s.cost_basis or ZERO
        out.append(
            P(
                account_id=s.account_id,
                instrument_id=inst,
                quantity=q,
                amount=value,
                role="asset",
                price=s.price,
                price_currency=s.price_currency,
            )
        )
        if value:
            out.append(P(amount=-value, role="income", instrument_id=inst))
    elif k in ("split", "security_in", "security_out"):
        inst = need_instrument()
        q = s.quantity if k == "split" else (abs(s.quantity) if k == "security_in" else -abs(s.quantity))
        if q == 0:
            raise LedgerError(f"{k} with zero quantity")
        value = (s.cost_basis or ZERO) if k == "security_in" else ZERO
        out.append(
            P(
                account_id=s.account_id,
                instrument_id=inst,
                quantity=q,
                amount=value,
                role="asset",
                price=s.price,
                price_currency=s.price_currency,
            )
        )
        if value:
            out.append(P(amount=-value, role="external"))
        cash_leg(amt)
        if amt:
            out.append(P(amount=-amt, role="external"))
    else:
        raise LedgerError(f"unknown transaction kind {k!r}")

    total = sum((p.amount for p in out), ZERO)
    if total != 0:
        raise LedgerError(f"postings do not balance ({total})")
    return out


def record(db: Session, s: TxnSpec) -> Transaction:
    txn = Transaction(
        account_id=s.account_id,
        date=s.date,
        settle_date=s.settle_date,
        kind=s.kind,
        description=s.description,
        currency=s.currency,
        source=s.source,
        external_id=s.external_id,
        seq=s.seq,
        import_batch_id=s.import_batch_id,
        raw_record_id=s.raw_record_id,
    )
    txn.postings = build_postings(db, s)
    db.add(txn)
    db.flush()
    return txn


@dataclass(frozen=True)
class Position:
    account_id: int
    instrument_id: int
    quantity: Decimal


def positions(db: Session, on: date | None = None, account_ids: list[int] | None = None) -> list[Position]:
    stmt = (
        select(Posting.account_id, Posting.instrument_id, func.sum(Posting.quantity))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Transaction.voided_at.is_(None), Posting.account_id.is_not(None), Posting.instrument_id.is_not(None))
        .group_by(Posting.account_id, Posting.instrument_id)
    )
    if on is not None:
        stmt = stmt.where(Transaction.date <= on)
    if account_ids is not None:
        stmt = stmt.where(Posting.account_id.in_(account_ids))
    return [Position(a, i, q) for a, i, q in db.execute(stmt).all() if q != 0 and a is not None and i is not None]


def void(db: Session, txn: Transaction, reason: str) -> None:
    from app.platform.db import utcnow

    txn.voided_at = utcnow()
    txn.void_reason = reason
