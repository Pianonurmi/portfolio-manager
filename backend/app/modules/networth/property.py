"""Property tools: price-index revaluation from Statistics Finland (RE-03) and housing costs from YNAB (RE-07).

Index estimate: value now = base value × index(latest quarter) / index(base quarter). The base is the purchase
(price and date in the property details) or, failing that, the latest non-index valuation. Postal-code €/m² is
noisy, so it is smoothed with a trade-weighted average of the last four quarters before taking the ratio.

Housing costs: YNAB categories mapped to a property (hoitovastike, rahoitusvastike, upkeep, …). Rahoitusvastike
repays the housing company loan, so it is shown but kept out of running costs and yield.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetCategory, BudgetTransaction
from app.modules.cashflow.service import month_key
from app.modules.core.models import Account
from app.modules.networth.models import IndexSeries, Valuation
from app.modules.pricing.providers import statfin
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today, utcnow
from app.platform.jobs import JobContext, job

router = APIRouter(prefix="/api/property", tags=["property"])
ZERO = Decimal(0)
COST_TYPES = {
    "vastike": "Hoitovastike",
    "rahoitusvastike": "Rahoitusvastike",
    "upkeep": "Upkeep and repairs",
    "utilities": "Water, electricity, heating",
    "insurance": "Home insurance",
    "property_tax": "Kiinteistövero",
    "other": "Other housing costs",
}
NOT_RUNNING = {"rahoitusvastike"}


def quarter(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_end(period: str) -> date:
    y, q = int(period[:4]), int(period[-1])
    nxt = date(y + (q == 4), 1 if q == 4 else q * 3 + 1, 1)
    return nxt - timedelta(days=1)


def _account(db: Session, account_id: int) -> Account:
    a = db.get(Account, account_id)
    if a is None or a.kind != "property":
        raise HTTPException(404, "Property not found")
    return a


# ---- index ----------------------------------------------------------------------------------------


def series_key(meta: dict[str, Any]) -> str | None:
    if meta.get("index_source") == "postal" and meta.get("postal_code"):
        return f"statfin:{statfin.POSTAL_TABLE}:{meta['postal_code']}:{meta.get('postal_house_type') or '2'}"
    if meta.get("index_source") == "region" and meta.get("index_region"):
        ht = str(meta.get("index_house_type") or "3")
        rooms = str(meta.get("index_rooms") or "00") if ht == "3" else "00"
        return f"statfin:{statfin.REGION_TABLE}:{meta['index_region']}:{ht}:{rooms}"
    return None


def refresh_series(db: Session, meta: dict[str, Any], client: Any | None = None) -> IndexSeries:
    if meta.get("index_source") == "postal":
        data = statfin.postal_prices(str(meta["postal_code"]), str(meta.get("postal_house_type") or "2"), client)
    elif meta.get("index_source") == "region":
        data = statfin.region_index(
            str(meta["index_region"]),
            str(meta.get("index_house_type") or "3"),
            str(meta.get("index_rooms") or "00"),
            client,
        )
    else:
        raise HTTPException(422, "Choose an index source in the property details first")
    s = db.get(IndexSeries, data["key"]) or IndexSeries(key=data["key"])
    s.label, s.unit = data["label"], data["unit"]
    s.points = {p: str(v) for p, v in data["points"]}
    s.trades = dict(data.get("trades") or {})
    s.fetched_at = utcnow()
    db.add(s)
    db.flush()
    return s


def smoothed(s: IndexSeries) -> dict[str, Decimal]:
    """Index series as is; €/m² as a trade-weighted average of the last four quarters."""
    pts = sorted((p, Decimal(v)) for p, v in s.points.items())
    if s.unit == "index":
        return dict(pts)
    out: dict[str, Decimal] = {}
    for i, (p, _) in enumerate(pts):
        window = pts[max(0, i - 3) : i + 1]
        w = [(v, Decimal(s.trades.get(q, 1) or 1)) for q, v in window]
        total = sum((x for _, x in w), ZERO)
        out[p] = (sum((v * x for v, x in w), ZERO) / total).quantize(Decimal("0.01"))
    return out


def _base(db: Session, a: Account) -> tuple[date, Decimal, str] | None:
    m = a.meta or {}
    if m.get("purchase_price") and m.get("purchase_date"):
        return date.fromisoformat(str(m["purchase_date"])), Decimal(str(m["purchase_price"])), "purchase"
    v = db.scalar(
        select(Valuation)
        .where(Valuation.account_id == a.id, Valuation.method != "index")
        .order_by(Valuation.date.desc())
        .limit(1)
    )
    return (v.date, v.value, f"{v.method} valuation") if v else None


def estimate(db: Session, a: Account, s: IndexSeries) -> dict[str, Any]:
    idx = smoothed(s)
    if not idx:
        raise HTTPException(422, "The series has no data for this area")
    base = _base(db, a)
    latest = max(idx)
    out: dict[str, Any] = {"latest_period": latest, "latest_value": idx[latest], "base": None, "value": None}
    area = (a.meta or {}).get("area_m2")
    if s.unit == "eur_m2" and area:
        out["market_m2_value"] = (idx[latest] * Decimal(str(area).replace(",", "."))).quantize(Decimal(1))
    if base is None:
        out["missing"] = "a purchase price and date, or a valuation to start from"
        return out
    bdate, bvalue, bsource = base
    candidates = [p for p in idx if p <= quarter(bdate)]
    if not candidates:
        out["missing"] = f"index data for {quarter(bdate)} (the series starts {min(idx)})"
        return out
    bp = max(candidates)
    value = (bvalue * idx[latest] / idx[bp]).quantize(Decimal(1))
    out.update(
        base={"date": bdate, "value": bvalue, "source": bsource, "period": bp, "index": idx[bp]},
        value=value,
        change=float(idx[latest] / idx[bp] - 1),
    )
    return out


def add_index_valuation(db: Session, a: Account, s: IndexSeries) -> Valuation | None:
    e = estimate(db, a, s)
    if e["value"] is None:
        return None
    on = min(quarter_end(e["latest_period"]), today())
    v = db.scalar(
        select(Valuation).where(Valuation.account_id == a.id, Valuation.method == "index", Valuation.date == on)
    )
    note = (
        f"{s.label}: {e['latest_period']} {e['latest_value']} / {e['base']['period']} {e['base']['index']}"
        f" × {e['base']['value']} ({e['base']['source']})"
    )
    if v is None:
        v = Valuation(account_id=a.id, date=on, value=e["value"], method="index", note=note)
        db.add(v)
    else:
        v.value, v.note = e["value"], note
    db.flush()
    return v


@lru_cache(maxsize=1)
def _regions_cached(day: date) -> list[dict[str, str]]:
    return statfin.regions()


@router.get("/index/regions")
def regions(_: CurrentUser) -> list[dict[str, str]]:
    try:
        return _regions_cached(today())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Statistics Finland is not reachable: {exc}") from exc


@router.get("/{account_id}/index")
def index(account_id: int, _: CurrentUser, db: Db, refresh: bool = False) -> dict[str, Any]:
    a = _account(db, account_id)
    key = series_key(a.meta or {})
    if key is None:
        return {"configured": False}
    s = db.get(IndexSeries, key)
    if s is None or refresh:
        try:
            s = refresh_series(db, a.meta or {})
        except statfin.StatFinError as exc:
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - network problems are shown, not raised as 500
            raise HTTPException(502, f"Statistics Finland is not reachable: {exc}") from exc
    pts = sorted(smoothed(s).items())
    return {
        "configured": True,
        "key": s.key,
        "label": s.label,
        "unit": s.unit,
        "fetched_at": s.fetched_at,
        "points": [{"period": p, "value": v, "trades": s.trades.get(p)} for p, v in pts[-40:]],
        "estimate": estimate(db, a, s),
    }


@router.post("/{account_id}/index/valuation", status_code=201)
def index_valuation(account_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    a = _account(db, account_id)
    key = series_key(a.meta or {})
    s = db.get(IndexSeries, key) if key else None
    if s is None:
        raise HTTPException(422, "Load the index first")
    v = add_index_valuation(db, a, s)
    if v is None:
        raise HTTPException(422, "Add a purchase price and date (or a valuation) to start the index from")
    audit(db, user.username, "valuation.index", "valuation", v.id, after={"series": s.key, "value": str(v.value)})
    return {
        "id": v.id,
        "date": v.date,
        "value": v.value,
        "undo_id": make_undo(db, "valuation.delete", "Index estimate", {"id": v.id}),
    }


@job("property.index", "Refresh housing price indices (Statistics Finland)", cron="15 7 * * 1")
def refresh_indices(ctx: JobContext) -> None:
    n = 0
    for a in ctx.db.scalars(select(Account).where(Account.kind == "property", Account.closed_on.is_(None))):
        m = a.meta or {}
        if series_key(m) is None:
            continue
        try:
            s = refresh_series(ctx.db, m)
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"{a.name}: {exc}")
            continue
        if m.get("index_auto"):
            v = add_index_valuation(ctx.db, a, s)
            if v is not None:
                n += 1
                ctx.log(f"{a.name}: {v.value} € ({s.label})")
    ctx.summary = f"{n} index valuations updated"


# ---- housing costs ---------------------------------------------------------------------------------


def costs(db: Session, a: Account, months: int = 24, until: date | None = None) -> dict[str, Any]:
    m = a.meta or {}
    mapping: dict[str, str] = dict(m.get("cost_categories") or {})
    rent_cat = m.get("rent_category")
    end = until or today()
    keys: list[str] = []
    y, mo = end.year, end.month
    for _ in range(months):
        keys.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    keys.reverse()
    start = date(int(keys[0][:4]), int(keys[0][5:]), 1)
    cats = {c.id: c for c in db.scalars(select(BudgetCategory))}
    by_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    rent: dict[str, Decimal] = defaultdict(Decimal)
    ids = list(mapping) + ([rent_cat] if rent_cat else [])
    if ids:
        for t in db.scalars(
            select(BudgetTransaction).where(
                BudgetTransaction.deleted.is_(False),
                BudgetTransaction.category_id.in_(ids),
                BudgetTransaction.date >= start,
                BudgetTransaction.date <= end,
            )
        ):
            k = month_key(t.date)
            if t.category_id == rent_cat:
                rent[k] += t.amount
            elif t.category_id in mapping:
                by_month[k][mapping[t.category_id]] += -t.amount
    last12 = keys[-13:-1] if len(keys) > 12 else keys[:-1] or keys  # full months only
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for k in last12:
        for typ, v in by_month[k].items():
            totals[typ] += v
    running = sum((v for t, v in totals.items() if t not in NOT_RUNNING), ZERO)
    rent12 = sum((rent[k] for k in last12), ZERO)
    value = db.scalar(
        select(Valuation.value).where(Valuation.account_id == a.id).order_by(Valuation.date.desc()).limit(1)
    )
    area = Decimal(str(m["area_m2"]).replace(",", ".")) if m.get("area_m2") else None
    n = Decimal(max(1, len(last12)))
    return {
        "mapping": [
            {
                "category_id": cid,
                "category": f"{cats[cid].group_name} · {cats[cid].name}" if cid in cats else cid,
                "type": typ,
                "type_label": COST_TYPES.get(typ, typ),
            }
            for cid, typ in mapping.items()
        ],
        "rent_category": rent_cat,
        "types": COST_TYPES,
        "months": [
            {
                "month": k,
                "total": sum(by_month[k].values(), ZERO),
                "by_type": dict(by_month[k]),
                "rent": rent.get(k, ZERO),
            }
            for k in keys
        ],
        "last12": {
            "by_type": dict(totals),
            "running": running,
            "total": sum(totals.values(), ZERO),
            "monthly_running": (running / n).quantize(Decimal("0.01")),
            "per_m2_month": (running / n / area).quantize(Decimal("0.01")) if area else None,
            "rent": rent12,
            "net_yield": float((rent12 - running) / value) if rent_cat and value else None,
        },
    }


@router.get("/{account_id}/costs")
def property_costs(account_id: int, _: CurrentUser, db: Db, months: int = 24) -> dict[str, Any]:
    return costs(db, _account(db, account_id), max(3, min(months, 120)))


class CostMappingIn(BaseModel):
    cost_categories: dict[str, str]
    rent_category: str | None = None


@router.put("/{account_id}/costs")
def map_costs(account_id: int, body: CostMappingIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    a = _account(db, account_id)
    bad = [t for t in body.cost_categories.values() if t not in COST_TYPES]
    if bad:
        raise HTTPException(422, f"Unknown cost type {bad[0]}")
    before = dict(a.meta or {})
    a.meta = {**before, "cost_categories": body.cost_categories, "rent_category": body.rent_category}
    audit(db, user.username, "property.costs", "account", a.id, after=body.model_dump())
    return costs(db, a)
