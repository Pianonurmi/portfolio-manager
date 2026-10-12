"""Nordnet securities credit: collateral value (lainoitusarvo), Superluotto tier, interest and margin-call distance.

Terms come from the dated rule table `rules/nordnet_credit-<year>.yaml` (public Nordnet pages). Collateral rates per
instrument are Nordnet's published minimums (instrument meta "nordnet", refreshed weekly); a well diversified
portfolio gets the diversification bonus from the table. The bonus is applied as the same number of percentage points
to every pledged holding, capped at the maximum rate: Nordnet states the table per portfolio, not per holding.
Only the account at Nordnet (a book-entry account, AOT) is pledged; an osakesäästötili cannot carry credit.
"""

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.portfolio.valuation import Scope, Valuer
from app.platform.rules import rules_for

TIERS = ("kulta", "hopea", "base")  # cheapest first
TIER_LABEL = {"kulta": "Kulta", "hopea": "Hopea", "base": "Base rate"}


class LeverageError(ValueError):
    pass


@dataclass(frozen=True)
class Pledge:
    key: str
    name: str
    value: float  # EUR market value
    kind: str  # stock | etf | fund | other
    pawn: float  # Nordnet's published collateral rate (0..1)
    listed: bool = True  # False: Nordnet does not list it, so it has no collateral value


@dataclass(frozen=True)
class Terms:
    rates: dict[str, float]  # tier -> yearly rate on the day
    tiers: dict[str, dict[str, float]]
    columns: list[float]
    table: dict[float, list[float]]
    max_rate: float
    fund_counts_as: int
    bonus_below: float
    source: str

    @staticmethod
    def from_rules(doc: dict[str, Any], on: date) -> "Terms":
        rates = {}
        for tier, steps in doc["rates"].items():
            valid = [s for s in steps if date.fromisoformat(str(s["from"])) <= on] or steps[:1]
            rates[tier] = float(max(valid, key=lambda s: str(s["from"]))["rate"])
        c = doc["collateral"]
        return Terms(
            rates=rates,
            tiers={k: {kk: float(vv) for kk, vv in v.items()} for k, v in doc["tiers"].items()},
            columns=[float(x) for x in c["bonus_columns"]],
            table={float(k): [float(x) for x in v] for k, v in c["bonus_table"].items()},
            max_rate=float(c["max_rate"]),
            fund_counts_as=int(c.get("fund_counts_as", 1)),
            bonus_below=float(c.get("bonus_needs_largest_below", 0.5)),
            source=str(doc.get("source", "")),
        )


def terms_for(db: Session, on: date) -> Terms:
    return Terms.from_rules(rules_for(db, "nordnet_credit", on.year), on)


# ---- collateral --------------------------------------------------------------------------------------------------


def _eligible(pledges: list[Pledge]) -> list[Pledge]:
    return [p for p in pledges if p.pawn > 0 and p.value > 0]


def largest_share(pledges: list[Pledge], terms: Terms) -> float:
    """The largest holding's share of the pledged market value; a fund counts as several holdings."""
    el = _eligible(pledges)
    mv = sum(p.value for p in el)
    if mv <= 0:
        return 0.0
    return max((p.value / (terms.fund_counts_as if p.kind == "fund" else 1)) / mv for p in el)


def bonus_rate(avg: float, largest: float, terms: Terms) -> float:
    """The portfolio-level rate from the diversification table (never below the average itself)."""
    if largest > terms.bonus_below or avg <= 0:
        return avg
    rows = [r for r in sorted(terms.table) if r <= avg + 1e-9]
    if not rows:
        return avg
    col = next((i for i, c in enumerate(terms.columns) if largest <= c + 1e-9), len(terms.columns) - 1)
    return max(avg, min(terms.table[rows[-1]][col], terms.max_rate))


def collateral(pledges: list[Pledge], terms: Terms) -> dict[str, Any]:
    """Collateral value with the diversification bonus, per holding and in total."""
    el = _eligible(pledges)
    mv = sum(p.value for p in el)
    avg = sum(p.value * p.pawn for p in el) / mv if mv > 0 else 0.0
    largest = largest_share(pledges, terms)
    port = bonus_rate(avg, largest, terms)
    uplift = port - avg
    rows = []
    total = 0.0
    for p in pledges:
        rate = min(p.pawn + uplift, terms.max_rate) if p.pawn > 0 else 0.0
        cv = max(p.value, 0.0) * rate
        total += cv
        rows.append(
            {
                "key": p.key,
                "name": p.name,
                "kind": p.kind,
                "value": p.value,
                "pawn": p.pawn,
                "rate": rate,
                "collateral": cv,
                "listed": p.listed,
            }
        )
    return {
        "market_value": sum(max(p.value, 0.0) for p in pledges),
        "pledged_value": mv,
        "average_rate": avg,
        "largest_share": largest,
        "bonus": uplift,
        "collateral_value": total,
        "effective_rate": total / mv if mv > 0 else 0.0,
        "holdings": sorted(rows, key=lambda r: -r["value"]),
    }


def _check(what: str, value: float, limit: float) -> dict[str, Any]:
    return {"what": what, "value": value, "limit": limit, "ok": value <= limit + 1e-12}


def tier(pledges: list[Pledge], loan: float, collateral_value: float, terms: Terms) -> dict[str, Any]:
    """The cheapest Superluotto tier whose three conditions hold, with each condition's actual value."""
    el = _eligible(pledges)
    mv = sum(p.value for p in el)
    stock = max((p.value / mv for p in el if p.kind != "fund"), default=0.0) if mv else 0.0
    fund = max((p.value / mv for p in el if p.kind == "fund"), default=0.0) if mv else 0.0
    use = loan / collateral_value if collateral_value > 0 else (0.0 if loan <= 0 else float("inf"))
    checks = {}
    chosen = "base"
    for t in ("kulta", "hopea"):
        c = terms.tiers[t]
        checks[t] = [
            _check("largest share or ETF", stock, c["max_stock_or_etf"]),
            _check("largest fund", fund, c["max_fund"]),
            _check("credit used of collateral value", use, c["max_loan_to_collateral"]),
        ]
        if chosen == "base" and all(x["ok"] for x in checks[t]):
            chosen = t
    return {"tier": chosen, "label": TIER_LABEL[chosen], "rate": terms.rates[chosen], "checks": checks}


def margin_call_drop(pledges: list[Pledge], loan: float, terms: Terms) -> dict[str, float | None]:
    """The fall that would push the loan above the collateral value: all holdings together, and the largest alone."""
    if loan <= 0:
        return {"all": None, "largest": None}
    cv = collateral(pledges, terms)["collateral_value"]
    if cv <= loan:
        return {"all": 0.0, "largest": 0.0}
    together = 1 - loan / cv  # a uniform fall keeps the weights, so the bonus stays
    el = sorted(_eligible(pledges), key=lambda p: -p.value)
    alone: float | None = None
    if el:
        top = el[0]

        def after(d: float) -> float:
            moved = [replace(p, value=p.value * (1 - d)) if p.key == top.key else p for p in pledges]
            return float(collateral(moved, terms)["collateral_value"])

        if after(1.0) <= loan:
            lo, hi = 0.0, 1.0
            for _ in range(50):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if after(mid) > loan else (lo, mid)
            alone = hi
    return {"all": together, "largest": alone}


def interest(loan: float, rate: float, deductible: float) -> dict[str, float]:
    """Yearly interest and what it costs after the capital-income deduction (or deficit credit)."""
    gross = max(loan, 0.0) * rate
    return {"gross": gross, "after_tax": gross * (1 - deductible), "rate_after_tax": rate * (1 - deductible)}


def what_if(pledges: list[Pledge], loan: float, extra: float, terms: Terms, into: str | None = None) -> list[Pledge]:
    """Borrow `extra` and buy: into one holding (`into`), else spread over the pledged holdings by value."""
    if extra == 0:
        return list(pledges)
    if into is not None:
        if not any(p.key == into for p in pledges):
            raise LeverageError("Unknown holding to buy")
        return [replace(p, value=p.value + extra) if p.key == into else p for p in pledges]
    el = _eligible(pledges)
    mv = sum(p.value for p in el)
    if mv <= 0:
        raise LeverageError("Nothing in the account has a collateral value")
    return [replace(p, value=p.value + extra * p.value / mv) if p in el else p for p in pledges]


def report(pledges: list[Pledge], loan: float, terms: Terms, deductible: float) -> dict[str, Any]:
    col = collateral(pledges, terms)
    t = tier(pledges, loan, col["collateral_value"], terms)
    mv = col["market_value"]
    return {
        **col,
        "loan": loan,
        "equity": mv - loan,
        "leverage": mv / (mv - loan) if mv - loan > 0 else None,
        "loan_to_collateral": loan / col["collateral_value"] if col["collateral_value"] > 0 else None,
        "headroom": max(col["collateral_value"] - loan, 0.0),
        "tier": t,
        "interest": interest(loan, t["rate"], deductible),
        "margin_call_drop": margin_call_drop(pledges, loan, terms),
        "shocks": [
            {
                "fall": d,
                "loan_to_collateral": loan / (col["collateral_value"] * (1 - d)) if col["collateral_value"] else None,
                "margin_call": loan > col["collateral_value"] * (1 - d),
            }
            for d in (0.1, 0.2, 0.3, 0.4, 0.5)
        ],
    }


# ---- from the ledger ---------------------------------------------------------------------------------------------


def pledged_account(db: Session, account_id: int | None = None) -> Account | None:
    """The book-entry account at Nordnet (or the one asked for); an OST cannot carry credit."""
    if account_id is not None:
        acc = db.get(Account, account_id)
        if acc is None or acc.kind != "aot":
            raise LeverageError("Credit is pledged against a book-entry account (AOT)")
        return acc
    for acc in db.scalars(select(Account).where(Account.kind == "aot").order_by(Account.id)):
        if acc.institution and "nordnet" in acc.institution.name.lower():
            return acc
    return None


def pledges_for(db: Session, acc: Account, on: date) -> tuple[list[Pledge], float]:
    """The account's holdings as pledges, and today's loan (a negative cash balance)."""
    v = Valuer(db, Scope(None), account_ids=[acc.id])
    holdings = v.holdings(on)
    insts = {
        i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_({h.instrument_id for h in holdings})))
    }
    out: list[Pledge] = []
    cash = 0.0
    for h in holdings:
        inst = insts[h.instrument_id]
        if inst.type == "cash":
            cash += float(h.value)
            continue
        nn = (inst.meta or {}).get("nordnet") or {}
        manual = (inst.meta or {}).get("collateral_manual")
        pawn = float(manual) if manual is not None else float(nn.get("pawn") or 0.0)
        kind = (
            "fund"
            if inst.type == "fund"
            else "etf"
            if inst.type == "etf"
            else "stock"
            if inst.type == "stock"
            else "other"
        )
        out.append(
            Pledge(str(inst.id), inst.name, float(h.value), kind, pawn, listed=bool(nn) and not nn.get("unlisted"))
        )
    return out, max(-cash, 0.0)
