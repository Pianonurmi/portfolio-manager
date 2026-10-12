"""Balance sheet and net-worth history (NW-01..04)."""

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import CASH_KINDS, INVESTMENT_KINDS, LIABILITY_KINDS, PROPERTY_KINDS, Account
from app.modules.ledger.models import Transaction
from app.modules.networth.loans import estimate_balance
from app.modules.networth.models import Valuation
from app.modules.portfolio.valuation import Scope, Shares, Valuer

ZERO = Decimal(0)

GROUPS = ["investments", "cash", "property", "other", "liabilities"]
GROUP_LABEL = {
    "investments": "Investments",
    "cash": "Cash and savings",
    "property": "Property",
    "other": "Other assets",
    "liabilities": "Liabilities",
}


def group_of(kind: str) -> str:
    if kind in LIABILITY_KINDS:
        return "liabilities"
    if kind in INVESTMENT_KINDS:
        return "investments"
    if kind in CASH_KINDS:
        return "cash"
    if kind in PROPERTY_KINDS:
        return "property"
    return "other"


@dataclass
class Item:
    account: Account
    group: str
    value: Decimal  # signed: liabilities negative; scope share applied
    gross: Decimal  # 100 % value, positive
    share: Decimal
    method: str  # ledger | manual | statement | index | purchase | estimate | none
    as_of: date | None
    estimated: bool


class Sheet:
    """Values every account in scope on any date."""

    def __init__(self, db: Session, scope: Scope) -> None:
        self.db = db
        self.scope = scope
        accounts = {a.id: a for a in db.scalars(select(Account))}
        ledger_ids = {
            a for (a,) in db.execute(select(Transaction.account_id).where(Transaction.voided_at.is_(None)).distinct())
        }
        self.shares = Shares(db, scope, list(accounts))
        if scope.entity_id is not None:
            accounts = {k: v for k, v in accounts.items() if k in self.shares.accounts()}
        self.accounts = accounts
        self.ledger_ids = {a for a in ledger_ids if a in accounts}
        self.valuer = Valuer(db, scope, account_ids=list(self.ledger_ids) or [-1])
        vals: dict[int, list[Valuation]] = defaultdict(list)
        for v in db.scalars(
            select(Valuation).where(Valuation.account_id.in_(list(accounts) or [-1])).order_by(Valuation.date)
        ):
            vals[v.account_id].append(v)
        self.vals = dict(vals)
        self._vdates = {k: [v.date for v in vs] for k, vs in self.vals.items()}

    def manual_value(self, account_id: int, on: date) -> tuple[Decimal, str, date | None, bool]:
        a = self.accounts[account_id]
        ds = self._vdates.get(account_id)
        if not ds:
            return ZERO, "none", None, False
        i = bisect.bisect_right(ds, on) - 1
        if i < 0:
            return ZERO, "none", None, False
        v = self.vals[account_id][i]
        if a.kind in ("mortgage", "loan", "housing_loan") and (on - v.date).days > 31:
            est = estimate_balance(v.value, v.date, on, a.meta or {})
            if est is not None:
                return est, "estimate", v.date, True
        return v.value, v.method, v.date, v.method in ("index", "manual") and a.kind in PROPERTY_KINDS

    def items(self, on: date) -> list[Item]:
        out: list[Item] = []
        ledger_vals: dict[int, Decimal] = defaultdict(Decimal)
        gross_vals: dict[int, Decimal] = defaultdict(Decimal)
        for h in self.valuer.holdings(on):
            ledger_vals[h.account_id] += h.value
            gross_vals[h.account_id] += h.value / h.share if h.share else ZERO
        for aid, a in self.accounts.items():
            if a.opened_on and a.opened_on > on:
                continue
            if a.closed_on and a.closed_on < on:
                continue
            share = self.shares.on(aid, on)
            if share == 0:
                continue
            g = group_of(a.kind)
            as_of: date | None
            if aid in self.ledger_ids:
                val, gross, method, as_of, est = (
                    ledger_vals.get(aid, ZERO),
                    gross_vals.get(aid, ZERO),
                    "ledger",
                    on,
                    False,
                )
            else:
                gross, method, as_of, est = self.manual_value(aid, on)
                val = gross * share
            sign = -1 if g == "liabilities" else 1
            out.append(Item(a, g, val * sign, gross, share, method, as_of, est))
        return out

    def series(self, start: date, end: date, step: int) -> list[dict[str, object]]:
        ldates, lvalues, _ = self.valuer.daily(start, end) if self.ledger_ids else ([], [], [])
        by_date = dict(zip(ldates, lvalues, strict=False))
        # ledger part by group
        out = []
        d = start
        dates = []
        while d <= end:
            dates.append(d)
            d += timedelta(days=step)
        if dates[-1] != end:
            dates.append(end)
        for d in dates:
            items = self.items(d)
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for it in items:
                totals[it.group] += it.value
            assets = sum((v for k, v in totals.items() if k != "liabilities"), ZERO)
            liab = -totals["liabilities"]
            out.append(
                {
                    "date": d,
                    "assets": assets,
                    "liabilities": liab,
                    "net_worth": assets - liab,
                    "investments": totals["investments"],
                    "property": totals["property"],
                    "cash": totals["cash"],
                    "ledger": by_date.get(d, ZERO),
                }
            )
        return out

    def change(self, start: date, end: date) -> dict[str, Decimal]:
        """Decompose the net-worth change into contributions, market returns, debt paydown, revaluation."""
        a, b = self.items(start), self.items(end)
        va = {i.account.id: i for i in a}
        vb = {i.account.id: i for i in b}
        contributions = market = paydown = reval = ZERO
        _, _, flows = self.valuer.daily(start, end) if self.ledger_ids else ([], [], [])
        ledger_flows = sum(flows[1:], ZERO) if flows else ZERO
        for aid in set(va) | set(vb):
            x, y = va.get(aid), vb.get(aid)
            dv = (y.value if y else ZERO) - (x.value if x else ZERO)
            grp = (y or x).group  # type: ignore[union-attr]
            if aid in self.ledger_ids:
                market += dv
            elif grp == "liabilities":
                paydown += dv
            else:
                reval += dv
        contributions = ledger_flows
        market -= ledger_flows
        total = contributions + market + paydown + reval
        return {
            "contributions": contributions,
            "market": market,
            "debt_paydown": paydown,
            "revaluation": reval,
            "total": total,
        }
