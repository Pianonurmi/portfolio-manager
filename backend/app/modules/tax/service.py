"""Finnish capital income tax estimates (TAX-01..08). Parameters come from versioned rule tables (TAX-09).

Estimates for verification and planning only; Nordnet and Vero remain the authority.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.ledger.models import Posting, Transaction
from app.modules.portfolio import lots as lotmod
from app.modules.portfolio.valuation import Scope, Shares, Valuer
from app.modules.tax.models import Gift
from app.platform.rules import rules_for

ZERO = Decimal(0)
CENT = Decimal("0.01")
NON_TAXABLE_WRAPPERS = {"ost", "kapitalisaatio", "ps"}


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def bracket_tax(amount: Decimal, brackets: list[dict[str, Any]], free_below: Decimal = ZERO) -> Decimal:
    """Gift/inheritance style tables: [{from, base, rate}] — tax = base + rate × (amount − from)."""
    if amount < free_below:
        return ZERO
    row = None
    for b in brackets:
        if amount >= D(b["from"]):
            row = b
    if row is None:
        return ZERO
    return (D(row["base"]) + D(row["rate"]) * (amount - D(row["from"]))).quantize(CENT)


def capital_income_tax(amount: Decimal, rules: dict[str, Any]) -> Decimal:
    tax = ZERO
    brackets = sorted(rules["capital_income"]["brackets"], key=lambda b: D(b["from"]))
    for i, b in enumerate(brackets):
        lo = D(b["from"])
        hi = D(brackets[i + 1]["from"]) if i + 1 < len(brackets) else None
        if amount > lo:
            part = (min(amount, hi) if hi is not None else amount) - lo
            tax += part * D(b["rate"])
    return tax.quantize(CENT)


@dataclass
class Sale:
    account: str
    instrument: str
    sold: date
    acquired: date
    quantity: Decimal
    gross: Decimal
    proceeds: Decimal
    cost: Decimal
    deemed: Decimal
    method: str
    gain: Decimal
    share: Decimal
    asset_class: str = "equity"


def sales(db: Session, scope: Scope, year: int) -> list[Sale]:
    rules = rules_for(db, "tax", year)
    dac = rules["deemed_acquisition_cost"]
    accounts = {a.id: a for a in db.scalars(select(Account))}
    taxable = [a for a, acc in accounts.items() if acc.kind not in NON_TAXABLE_WRAPPERS]
    shares = Shares(db, scope, list(accounts))
    insts = {i.id: i for i in db.scalars(select(Instrument))}
    names = {k: i.name for k, i in insts.items()}
    out: list[Sale] = []
    for d in lotmod.build(db, taxable, until=date(year, 12, 31)).disposals:
        if d.sold.year != year:
            continue
        sh = shares.on(d.account_id, d.sold)
        if sh == 0:
            continue
        years_held = (d.sold - d.acquired).days / 365.25
        pct = D(dac["held_10_years_or_more"] if years_held >= 10 else dac["held_under_10_years"])
        deemed = (d.gross * pct).quantize(CENT)
        actual_gain = d.proceeds - d.cost
        deemed_gain = d.gross - deemed
        use_deemed = deemed_gain < actual_gain  # the taxpayer may choose the better one per sale
        gain = deemed_gain if use_deemed else actual_gain
        out.append(
            Sale(
                accounts[d.account_id].name,
                names.get(d.instrument_id, "?"),
                d.sold,
                d.acquired,
                d.quantity * sh,
                (d.gross * sh).quantize(CENT),
                (d.proceeds * sh).quantize(CENT),
                (d.cost * sh).quantize(CENT),
                (deemed * sh).quantize(CENT),
                "hankintameno-olettama" if use_deemed else "actual cost",
                (gain * sh).quantize(CENT),
                sh,
                "crypto" if (i := insts.get(d.instrument_id)) is not None and i.type == "crypto" else "equity",
            )
        )
    return out


def dividends(db: Session, scope: Scope, year: int) -> dict[str, Any]:
    """Gross dividends and withholding in taxable accounts (OST dividends stay inside the wrapper).

    Foreign dividends and their withholding are also split by source country (the ISIN prefix) for the
    country-by-country foreign tax credit.
    """
    v = Valuer(db, scope)
    rows = v.income(date(year, 1, 1), date(year, 12, 31))
    kinds = {a.id: a.kind for a in db.scalars(select(Account))}
    gross = interest = rewards = fi_tax = foreign_tax = ZERO
    by_country: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"gross": ZERO, "withheld": ZERO})
    countries: dict[int, str] = {}

    def country(instrument_id: int | None) -> str:
        if instrument_id is None:
            return "FI"
        if instrument_id not in countries:
            inst = db.get(Instrument, instrument_id)
            countries[instrument_id] = ((inst.isin if inst else None) or "FI")[:2]
        return countries[instrument_id]

    for r in rows:
        if kinds.get(r["account_id"]) in NON_TAXABLE_WRAPPERS:
            continue
        e = D(r["eur"])
        if r["role"] == "income" and r["kind"] == "dividend":
            gross += e
            if (cc := country(r["instrument_id"])) != "FI":
                by_country[cc]["gross"] += e
        elif r["role"] == "income" and r["kind"] == "interest":
            interest += e
        elif r["role"] == "income" and r["kind"] == "reward":
            rewards += e
        elif r["role"] == "tax":
            if (cc := country(r["instrument_id"])) != "FI":
                foreign_tax += -e
                by_country[cc]["withheld"] += -e
            else:
                fi_tax += -e
    return {
        "gross": gross,
        "interest": interest,
        "rewards": rewards,
        "finnish_withholding": fi_tax,
        "foreign_withholding": foreign_tax,
        "foreign_by_country": dict(sorted(by_country.items())),
    }


def foreign_tax_credit(
    div: dict[str, Any], tax: Decimal, positive_income: Decimal, rules: dict[str, Any]
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Credit for foreign withholding, country by country (kaksinkertaisen verotuksen poistamisesta annettu laki).

    Finland credits at most the treaty rate on the gross dividend (the rest is reclaimed from the source country)
    and at most the Finnish tax on that country's income, which is the year's tax in proportion to the country's
    share of the positive capital income.
    """
    caps = rules["dividends"].get("foreign_credit_cap", {"default": "0.15"})
    share = D(rules["dividends"]["listed_taxable_share"])
    total = ZERO
    rows = []
    for cc, x in div["foreign_by_country"].items():
        cap = D(caps.get(cc, caps["default"]))
        creditable = min(x["withheld"], (x["gross"] * cap).quantize(CENT))
        limit = (tax * x["gross"] * share / positive_income).quantize(CENT) if positive_income > 0 else ZERO
        credit = min(creditable, limit)
        total += credit
        rows.append(
            {
                "country": cc,
                "gross": x["gross"].quantize(CENT),
                "withheld": x["withheld"].quantize(CENT),
                "treaty_rate": cap,
                "credit": credit,
                "reclaimable": (x["withheld"] - creditable).quantize(CENT),
            }
        )
    return min(total, tax), rows


def capital_income_before_losses_carried(db: Session, scope: Scope, year: int, rules: dict[str, Any]) -> Decimal:
    """The year's capital income after its own losses, before carried-forward losses. Negative = a new loss.

    Since 2016 a capital loss is deducted from all capital income of the year (TVL 50 §), so the year's taxable
    dividends and rewards absorb it before anything is carried forward.
    """
    ss = sales(db, scope, year)
    exempt, losses_count = small_disposals(ss, rules)
    gains = sum((s.gain for s in ss if s.gain > 0), ZERO)
    losses = -sum((s.gain for s in ss if s.gain < 0), ZERO)
    div = dividends(db, scope, year)
    taxable_div = (D(div["gross"]) * D(rules["dividends"]["listed_taxable_share"])).quantize(CENT)
    rewards = D(div["rewards"]).quantize(CENT)
    return (ZERO if exempt else gains) - (losses if losses_count else ZERO) + taxable_div + rewards


def ost_status(db: Session, scope: Scope, on: date) -> list[dict[str, Any]]:
    rules = rules_for(db, "tax", on.year)
    cap = D(rules["ost"]["deposit_cap"])
    out = []
    v = Valuer(db, scope, kinds={"ost"})
    for aid, acc in v.accounts.items():
        deposits = ZERO
        for t in db.scalars(
            select(Transaction)
            .where(
                Transaction.account_id == aid,
                Transaction.voided_at.is_(None),
                Transaction.kind.in_(["deposit", "withdrawal", "transfer_in", "transfer_out"]),
            )
            .order_by(Transaction.date)
        ):
            amt = sum((p.amount for p in t.postings if p.account_id is not None), ZERO)
            if amt > 0:
                deposits += amt
            else:  # a withdrawal returns deposits in proportion (the rest is taxable gain)
                val = sum(
                    (
                        h.value / h.share
                        for h in Valuer(db, Scope(None), account_ids=[aid]).holdings(t.date - timedelta(days=1))
                    ),
                    ZERO,
                )
                ratio = (deposits / val) if val > 0 else Decimal(1)
                deposits += amt * min(ratio, Decimal(1))
        value = sum((h.value / h.share for h in v.holdings(on) if h.account_id == aid), ZERO)
        share = v.shares.on(aid, on)
        gain_share = (value - deposits) / value if value > 0 else ZERO
        out.append(
            {
                "account": acc.name,
                "value": (value * share).quantize(CENT),
                "deposits": (deposits * share).quantize(CENT),
                "gain_share": float(max(gain_share, ZERO)),
                "cap": cap,
                "cap_left": max(cap - deposits, ZERO).quantize(CENT),
                "tax_per_1000_withdrawn": (
                    Decimal(1000) * max(gain_share, ZERO) * D(rules["capital_income"]["brackets"][0]["rate"])
                ).quantize(CENT),
            }
        )
    return out


def small_disposals(ss: list[Sale], rules: dict[str, Any]) -> tuple[bool, bool]:
    """(gains tax-free, losses deductible). Gains are free when the year's sale prices total at most the limit;
    losses are then deductible only if the acquisition costs exceed the loss limit (TVL 48 § 6 mom, 50 §)."""
    ci = rules["capital_income"]
    proceeds = sum((s.gross for s in ss), ZERO)
    costs = sum((s.cost for s in ss), ZERO)
    exempt = bool(ss) and proceeds <= D(ci["small_disposals_exempt_total_proceeds"])
    limit = D(ci.get("small_disposals_loss_limit_total_cost", ci["small_disposals_exempt_total_proceeds"]))
    losses_count = not (proceeds <= limit and costs <= limit)
    return exempt, losses_count


def year_summary(db: Session, scope: Scope, year: int) -> dict[str, Any]:
    rules = rules_for(db, "tax", year)
    ss = sales(db, scope, year)
    total_gross = sum((s.gross for s in ss), ZERO)
    gains = sum((s.gain for s in ss if s.gain > 0), ZERO)
    losses = -sum((s.gain for s in ss if s.gain < 0), ZERO)
    exempt, losses_count = small_disposals(ss, rules)
    net = (ZERO if exempt else gains) - (losses if losses_count else ZERO)
    # Loss carry-forward: each year's loss is used against the next five years' capital income, oldest first.
    # Replay twice the window so losses older than the window have taken their share of the gains in between.
    years = int(rules["capital_income"]["loss_carry_forward_years"])
    pool: list[tuple[int, Decimal]] = []
    for y in range(year - 2 * years, year):
        try:
            g = capital_income_before_losses_carried(db, scope, y, rules_for(db, "tax", y))
        except LookupError:
            continue
        pool = [(py, amt) for py, amt in pool if py >= y - years and amt > 0]
        if g < 0:
            pool.append((y, -g))
        elif g > 0:
            for i, (py, amt) in enumerate(pool):  # older losses used first
                use = min(amt, g)
                pool[i] = (py, amt - use)
                g -= use
    pool = [(py, amt) for py, amt in pool if py >= year - years]
    carry_in = sum((a for _, a in pool), ZERO)
    div = dividends(db, scope, year)
    taxable_div = (div["gross"] * D(rules["dividends"]["listed_taxable_share"])).quantize(CENT)
    rewards = div["rewards"].quantize(CENT)  # crypto staking/earn: fully taxable capital income
    # Losses are deductible from all capital income (gains, dividends, rewards) of the year, then carried forward.
    before = net + taxable_div + rewards
    new_loss = max(-before, ZERO)
    before = max(before, ZERO)
    used = min(carry_in, before)
    capital_income = before - used
    carry_out = carry_in - used + new_loss
    taxable_gains = max(net, ZERO)
    tax = capital_income_tax(capital_income, rules)
    positive_income = max(net, ZERO) + taxable_div + rewards
    credit, credit_rows = foreign_tax_credit(div, tax, positive_income, rules)
    prepaid = div["finnish_withholding"]
    return {
        "year": year,
        "rules_version": rules["_version"],
        "sales": ss,
        "sale_proceeds": total_gross,
        "small_disposals_exempt": exempt,
        "losses_deductible": losses_count,
        "gains": gains,
        "losses": losses,
        "loss_carry_in": carry_in,
        "loss_used": used,
        "loss_carry_out": carry_out,
        "taxable_gains": taxable_gains,
        "dividends": div,
        "taxable_dividends": taxable_div,
        "crypto_rewards": rewards,
        "capital_income": capital_income,
        "tax": tax,
        "foreign_tax_credit": credit,
        "foreign_tax_by_country": credit_rows,
        "foreign_tax_reclaimable": sum((r["reclaimable"] for r in credit_rows), ZERO),
        "prepaid": prepaid,
        "estimate_due": (tax - credit - prepaid).quantize(CENT),
    }


def crypto_report(db: Session, scope: Scope, year: int) -> dict[str, Any]:
    """Crypto disposals and rewards of a year, laid out like OmaVero's virtual-currency form (9A)."""
    from app.modules.crypto.booking import open_transfers

    ss = [s for s in sales(db, scope, year) if s.asset_class == "crypto"]
    v = Valuer(db, scope)
    insts = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.type == "crypto"))}
    rewards = [
        {"date": r["date"], "asset": insts[r["instrument_id"]].symbol, "eur": D(r["eur"]).quantize(CENT)}
        for r in v.income(date(year, 1, 1), date(year, 12, 31))
        if r["kind"] == "reward" and r["role"] == "income" and r["instrument_id"] in insts
    ]
    by_asset: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for s in ss:
        a = by_asset[s.instrument]
        a["proceeds"] += s.gross
        a["cost"] += s.cost if s.method == "actual cost" else s.deemed
        a["fees"] += s.gross - s.proceeds
        a["gain"] += s.gain
    open_ = open_transfers(db)
    return {
        "year": year,
        "sales": ss,
        "by_asset": [{"asset": k, **{f: x.quantize(CENT) for f, x in a.items()}} for k, a in sorted(by_asset.items())],
        "proceeds": sum((s.gross for s in ss), ZERO),
        "gains": sum((s.gain for s in ss if s.gain > 0), ZERO),
        "losses": -sum((s.gain for s in ss if s.gain < 0), ZERO),
        "rewards": rewards,
        "rewards_total": sum((r["eur"] for r in rewards), ZERO),
        "unmatched_out": [t for t in open_["out"] if t["date"].year <= year],
        "unknown_cost_in": [t for t in open_["in"] if t["date"].year <= year],
        "warnings": lotmod.build(db, list(v.accounts), until=date(year, 12, 31)).warnings[:20],
    }


def harvest_candidates(db: Session, scope: Scope, on: date) -> list[dict[str, Any]]:
    """Open taxable lots with unrealised losses (TAX-06)."""
    v = Valuer(db, scope)
    kinds = {a.id: a.kind for a in db.scalars(select(Account))}
    book = lotmod.build(db, list(v.accounts), until=on)
    names = {i.id: i.name for i in db.scalars(select(Instrument))}
    out = []
    for (aid, iid), ls in book.open.items():
        if kinds.get(aid) in NON_TAXABLE_WRAPPERS or iid in v.book.cash_ccy:
            continue
        try:
            unit, _, _ = v.book.unit_value_eur(iid, on)
        except LookupError:  # no price or FX rate for it (e.g. an old NOK holding): it cannot be valued
            continue
        share = v.shares.on(aid, on)
        for lot in ls:
            loss = (lot.quantity * unit - lot.cost) * share
            if loss < -1:
                out.append(
                    {
                        "instrument": names.get(iid),
                        "account": v.accounts[aid].name,
                        "acquired": lot.acquired,
                        "quantity": lot.quantity * share,
                        "value": (lot.quantity * unit * share).quantize(CENT),
                        "unrealised": loss.quantize(CENT),
                    }
                )
    return sorted(out, key=lambda x: x["unrealised"])


def gift_window(db: Session, recipient: int, on: date) -> list[dict[str, Any]]:
    rules = rules_for(db, "tax", on.year)
    g = rules["gift_tax"]
    start = on.replace(year=on.year - int(g["window_years"]))
    by: dict[str, Decimal] = defaultdict(Decimal)
    first: dict[str, date] = {}
    for x in db.scalars(
        select(Gift)
        .where(Gift.recipient_entity_id == recipient, Gift.date > start, Gift.date <= on)
        .order_by(Gift.date)
    ):
        by[x.donor] += x.amount
        first.setdefault(x.donor, x.date)
    limit = D(g["tax_free_below"])
    return [
        {
            "donor": d,
            "total": amt,
            "limit": limit,
            "left": max(limit - amt, ZERO),
            "tax_if_now": bracket_tax(amt, g["class_1_brackets"], limit),
            "window_frees": first[d].replace(year=first[d].year + int(g["window_years"])),
        }
        for d, amt in by.items()
    ]


def inheritance_tax(db: Session, amount: Decimal, year: int) -> Decimal:
    r = rules_for(db, "tax", year)["inheritance_tax"]
    return bracket_tax(amount, r["class_1_brackets"], D(r["tax_free_below"]))


def dividend_txn_ids(db: Session) -> list[int]:  # used by tests
    return [t for (t,) in db.execute(select(Posting.transaction_id).where(Posting.role == "income"))]
