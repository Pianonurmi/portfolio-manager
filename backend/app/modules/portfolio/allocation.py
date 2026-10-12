"""Allocation, targets, drift and tax-aware rebalancing suggestions (PORT-05, PORT-06)."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import INVESTMENT_KINDS, Instrument
from app.modules.portfolio import lots as lotmod
from app.modules.portfolio import profiles
from app.modules.portfolio.valuation import Scope, Valuer

ZERO = Decimal(0)
REGION_BY_ISIN = {
    "FI": "Finland",
    "SE": "Nordics",
    "NO": "Nordics",
    "DK": "Nordics",
    "US": "North America",
    "CA": "North America",
}


CLASS_LABELS = {"equity": "Equity", "bond": "Fixed income", "cash": "Cash", "other": "Other", "crypto": "Crypto"}


def _legacy_class(inst: Instrument) -> str:
    """The class of a holding that has no profile split: the single label on the instrument."""
    ac = (inst.asset_class or "").lower()
    if inst.type == "crypto" or ac == "crypto":
        return "Crypto"
    if ac in ("fixed_income", "bond") or inst.type == "bond":
        return "Fixed income"
    if ac == "mixed":
        return "Other"
    return "Equity"


def class_weights(inst: Instrument, data: dict[str, Any], rest: str = "Other") -> dict[str, Decimal]:
    """How one unit of the holding splits over asset classes (sums to 1); the unclassified remainder goes to `rest`."""
    if inst.type == "cash":
        return {"Cash": Decimal(1)}
    raw = data.get("asset_classes")
    if not raw:
        return {_legacy_class(inst): Decimal(1)}
    w: dict[str, Decimal] = defaultdict(Decimal)
    for k, x in raw.items():
        if k in CLASS_LABELS and x and x > 0:
            w[CLASS_LABELS[k]] += Decimal(str(x))
    total = sum(w.values(), ZERO)
    if total <= 0:
        return {rest: Decimal(1)}
    # rounding in the source (and its tiny not-classified part) is not a real gap
    if abs(total - 1) <= Decimal("0.001") or total > 1:
        return {k: x / total for k, x in w.items()}
    w[rest] += 1 - total
    return dict(w)


def classify(inst: Instrument, dim: str, data: dict[str, Any] | None = None) -> str:
    if inst.type == "cash":
        return "Cash" if dim != "currency" else inst.currency
    if dim == "asset_class":
        w = class_weights(inst, data or {})
        return max(w, key=lambda k: w[k])  # a mixed holding belongs to its dominant class
    if dim == "currency":
        return inst.currency
    if dim == "region":
        if inst.meta.get("region"):
            return str(inst.meta["region"])
        if inst.type in ("etf", "fund"):
            return "Global (fund)"
        return REGION_BY_ISIN.get(
            (inst.isin or "")[:2],
            "Europe" if (inst.isin or "")[:2] in ("DE", "FR", "NL", "IE", "GB", "CH") else "Other",
        )
    if dim == "instrument":
        return inst.name
    raise ValueError(f"unknown dimension {dim}")


def allocation(db: Session, scope: Scope, dim: str, on: date) -> dict[str, Decimal]:
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    insts = {i.id: i for i in db.scalars(select(Instrument))}
    holdings = v.holdings(on)
    out: dict[str, Decimal] = defaultdict(Decimal)
    if dim == "asset_class":
        eff = profiles.effective_many(db, [insts[i] for i in {h.instrument_id for h in holdings}])
        for h in holdings:
            for label, w in class_weights(insts[h.instrument_id], eff[h.instrument_id].data).items():
                out[label] += h.value * w
        return dict(out)
    for h in holdings:
        out[classify(insts[h.instrument_id], dim)] += h.value
    return dict(out)


def rebalance(
    db: Session,
    scope: Scope,
    targets: dict[str, Decimal],
    on: date,
    new_money: Decimal = ZERO,
    band: Decimal = Decimal("0.05"),
) -> dict[str, Any]:
    """Suggest trades by asset class. New money first; sells only beyond the drift band; OST before taxable accounts."""
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    insts = {i.id: i for i in db.scalars(select(Instrument))}
    holdings = v.holdings(on)
    eff = profiles.effective_many(db, [insts[i] for i in {h.instrument_id for h in holdings}])
    cur = allocation(db, scope, "asset_class", on)
    total = sum(cur.values(), ZERO) + new_money
    tsum = sum(targets.values(), ZERO) or Decimal(1)
    tgt = {k: v_ / tsum for k, v_ in targets.items()}
    rows: list[dict[str, Any]] = []
    for k in sorted(set(cur) | set(tgt)):
        w = cur.get(k, ZERO) / total if total else ZERO
        t = tgt.get(k, ZERO)
        rows.append(
            {
                "class": k,
                "value": cur.get(k, ZERO),
                "weight": w,
                "target": t,
                "drift": w - t,
                "gap": t * total - cur.get(k, ZERO),
            }
        )
    trades: list[dict[str, Any]] = []
    # 1) deploy new money into the most underweight classes
    left = new_money
    for r in sorted(rows, key=lambda r: r["gap"], reverse=True):
        if left <= 0 or r["gap"] <= 0:
            continue
        amt = min(left, r["gap"])
        trades.append(
            {"action": "buy", "class": r["class"], "amount": amt, "where": "OST first (no tax inside), else AOT"}
        )
        left -= amt
        r["gap"] -= amt
    # 2) beyond the band: sell overweight, preferring OST, then taxable lots with the smallest gain
    book = lotmod.build(db, list(v.accounts), until=on)
    for r in rows:
        if r["drift"] <= band or r["gap"] >= 0:
            continue
        need = -r["gap"]
        cands = []
        for h in holdings:
            inst = insts[h.instrument_id]
            if classify(inst, "asset_class", eff[h.instrument_id].data) != r["class"] or inst.type == "cash":
                continue
            acct = v.accounts[h.account_id]
            cost = book.cost_basis(h.account_id, h.instrument_id) * h.share
            gain_ratio = (h.value - cost) / h.value if h.value else ZERO
            cands.append((0 if acct.kind == "ost" else 1, gain_ratio, h, acct, cost))
        for _, gr, h, acct, _cost in sorted(cands, key=lambda c: (c[0], c[1])):
            if need <= 0:
                break
            amt = min(need, h.value)
            est_gain = amt * gr
            trades.append(
                {
                    "action": "sell",
                    "class": r["class"],
                    "amount": amt,
                    "instrument": insts[h.instrument_id].name,
                    "account": acct.name,
                    "taxable_gain_estimate": ZERO if acct.kind == "ost" else max(est_gain, ZERO),
                    "where": "inside OST: no tax" if acct.kind == "ost" else "taxable: gain realised",
                }
            )
            need -= amt
    return {"total": total, "rows": rows, "trades": trades, "band": band}


# ---- X-ray: what the portfolio holds once funds are looked through ------------------------------------------------

UNKNOWN = "Unknown"
NA = "Not applicable"
SIZE_LABELS = {"giant": "Giant", "large": "Large", "mid": "Mid", "small": "Small", "micro": "Micro"}
# non-equity parts of a holding keep their own name in the where-in-the-world views
SLEEVE_BUCKETS = {"Cash": "Cash", "Other": "Other", "Crypto": "Crypto", "Unknown": UNKNOWN}
# sources that say what a fund holds; for shares, crypto and cash the instrument type itself is enough
SOURCES_THAT_COVER = {"manual", "morningstar", "factsheet"}
DIMENSIONS = ("asset_class", "region", "country", "sector", "size", "style", "credit")
EPS = Decimal("0.001")


def _spread(out: dict[str, Decimal], amount: Decimal, weights: dict[str, float] | None) -> None:
    """Add `amount` over the weights of one sleeve; what they leave out (or all of it, if none) is Unknown."""
    w = {k: Decimal(str(x)) for k, x in (weights or {}).items() if x and x > 0}
    s = sum(w.values(), ZERO)
    if s <= 0:
        out[UNKNOWN] += amount
        return
    if s > 1 - EPS:  # rounding in the source, or too much: scale to the whole sleeve
        for k, x in w.items():
            out[k] += amount * x / s
        return
    for k, x in w.items():
        out[k] += amount * x
    out[UNKNOWN] += amount * (1 - s)


def _by_region(countries: dict[str, float] | None) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for c, x in (countries or {}).items():
        out[profiles.region_of(c)] += x
    return dict(out)


def holding_breakdown(inst: Instrument, data: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """Per dimension, the fractions of one unit of the holding by bucket (each dimension sums to 1)."""
    cw = class_weights(inst, data, UNKNOWN)
    eq, bd = cw.get("Equity", ZERO), cw.get("Fixed income", ZERO)
    rest = {k: x for k, x in cw.items() if k not in ("Equity", "Fixed income")}
    dims: dict[str, dict[str, Decimal]] = {d: defaultdict(Decimal) for d in DIMENSIONS}
    for k, x in cw.items():
        dims["asset_class"][k] += x

    regions = data.get("regions") or _by_region(data.get("countries"))
    if eq:
        _spread(dims["region"], eq, regions)
        _spread(dims["country"], eq, data.get("countries"))
    if bd:
        _spread(dims["region"], bd, _by_region(data.get("bond_countries")))
        _spread(dims["country"], bd, data.get("bond_countries"))
    for k, x in rest.items():
        bucket = SLEEVE_BUCKETS.get(k, UNKNOWN)
        dims["region"][bucket] += x
        dims["country"][bucket] += x

    for dim, field_ in (("sector", "sectors"), ("size", "cap_size"), ("style", "style")):
        weights = data.get(field_)
        if field_ == "cap_size" and weights:
            weights = {SIZE_LABELS.get(k, k): x for k, x in weights.items()}
        if eq:
            _spread(dims[dim], eq, weights)
        for k, x in cw.items():
            if k == "Unknown":
                dims[dim][UNKNOWN] += x
            elif k != "Equity":
                dims[dim][NA] += x
    if bd:
        _spread(dims["credit"], bd, data.get("credit"))
    for k, x in cw.items():
        if k == "Unknown":
            dims["credit"][UNKNOWN] += x
        elif k != "Fixed income":
            dims["credit"][NA] += x
    return {d: dict(b) for d, b in dims.items()}


def _ordered(buckets: dict[str, Decimal], total: Decimal, labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Largest first; Unknown, then Not applicable, always last."""

    def rank(kv: tuple[str, Decimal]) -> tuple[int, Decimal]:
        return (2 if kv[0] == NA else 1 if kv[0] == UNKNOWN else 0, -kv[1])

    return [
        {
            "key": k,
            "label": (labels or {}).get(k, k),
            "value": x,
            "weight": float(x / total) if total else 0.0,
        }
        for k, x in sorted(buckets.items(), key=rank)
        if x != 0
    ]


def _source_chips(eff: profiles.Effective) -> list[dict[str, Any]]:
    """Which sources feed this holding's numbers, each with the fields it supplied and its newest as-of date."""
    chips: dict[str, dict[str, Any]] = {}
    for name, src in eff.sources.items():
        c = chips.setdefault(src["source"] or "", {"source": src["source"], "as_of": None, "fields": []})
        c["fields"].append(name)
        if src["as_of"] and (c["as_of"] is None or src["as_of"] > c["as_of"]):
            c["as_of"] = src["as_of"]
    return [chips[s] for s in profiles.SOURCE_ORDER if s in chips]


def xray(db: Session, scope: Scope, on: date) -> dict[str, Any]:
    """Look-through exposures of the investment accounts: every ETF/fund is split into what it holds."""
    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    holdings = v.holdings(on)
    insts = {
        i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_({h.instrument_id for h in holdings})))
    }
    eff = profiles.effective_many(db, insts.values())
    total = sum((h.value for h in holdings), ZERO)
    dims: dict[str, dict[str, Decimal]] = {d: defaultdict(Decimal) for d in DIMENSIONS}
    rows: list[dict[str, Any]] = []
    ter_eur = ter_known = covered = bond_eur = ZERO
    dur_num = dur_den = ytm_num = ytm_den = ZERO
    quality: dict[str, Decimal] = defaultdict(Decimal)
    for h in sorted(holdings, key=lambda h: -h.value):
        inst, e = insts[h.instrument_id], eff[h.instrument_id]
        parts = holding_breakdown(inst, e.data)
        for d, buckets in parts.items():
            for k, x in buckets.items():
                dims[d][k] += h.value * x
        cw = class_weights(inst, e.data, UNKNOWN)
        src = e.source_of("asset_classes")
        is_covered = src in SOURCES_THAT_COVER or (
            src == "derived" and inst.type in ("stock", "crypto", "cash", "bond")
        )
        if is_covered:
            covered += h.value
        ter = e.data.get("ter")
        if ter is not None:
            ter_known += h.value
            ter_eur += h.value * Decimal(str(ter))
        bond = e.data.get("bond") or {}
        b = h.value * cw.get("Fixed income", ZERO)
        bond_eur += b
        if b and bond.get("modified_duration") is not None:
            dur_num, dur_den = dur_num + b * Decimal(str(bond["modified_duration"])), dur_den + b
        if b and bond.get("ytm") is not None:
            ytm_num, ytm_den = ytm_num + b * Decimal(str(bond["ytm"])), ytm_den + b
        if b and bond.get("credit_quality"):
            quality[str(bond["credit_quality"])] += b
        regions = {k: x for k, x in parts["region"].items() if k not in (UNKNOWN, NA)}
        rows.append(
            {
                "instrument_id": inst.id,
                "name": inst.name,
                "type": inst.type,
                "account_id": h.account_id,
                "account": v.accounts[h.account_id].name,
                "value": h.value,
                "weight": float(h.value / total) if total else 0.0,
                "asset_classes": {k: float(x) for k, x in sorted(cw.items(), key=lambda kv: -kv[1])},
                "top_region": max(regions, key=lambda k: regions[k]) if regions else None,
                "ter": ter,
                "ter_eur": h.value * Decimal(str(ter)) if ter is not None else None,
                "sources": _source_chips(e),
                "covered": is_covered,
            }
        )
    credit = {k: x for k, x in dims["credit"].items() if k not in (UNKNOWN, NA)}
    # Morningstar's average quality (a BBB average is not the same as an AAA largest slice); the slice only if unknown
    dominant = (
        max(quality, key=lambda k: quality[k])
        if quality
        else (max(credit, key=lambda k: credit[k]) if credit else None)
    )
    countries = profiles.COUNTRIES
    labels = {c: countries[c][1] for c in countries}
    return {
        "as_of": on.isoformat(),
        "total": total,
        "dimensions": {d: _ordered(dims[d], total, labels if d == "country" else None) for d in DIMENSIONS},
        "ter": {
            "fraction": float(ter_eur / ter_known) if ter_known else None,  # a rate, not money: never rounded to cents
            "eur_per_year": ter_eur,
            "known_share": float(ter_known / total) if total else 0.0,
        },
        "bond": {
            "value": bond_eur,
            "weight": float(bond_eur / total) if total else 0.0,
            "modified_duration": float(dur_num / dur_den) if dur_den else None,
            "ytm": float(ytm_num / ytm_den) if ytm_den else None,
            "credit_quality": dominant,
            "duration_coverage": float(dur_den / bond_eur) if bond_eur else 0.0,
        },
        "coverage": float(covered / total) if total else 0.0,
        "holdings": rows,
    }
