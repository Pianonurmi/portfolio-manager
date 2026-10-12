"""ETF screener: browse the universe, mark candidates the planner, optimiser and risk views can use."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import Select, Text, func, or_, select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ledger.service import positions
from app.modules.portfolio import profiles
from app.modules.pricing.models import Price
from app.modules.pricing.providers import morningstar
from app.modules.screener.models import EtfListing
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.jobs import enqueue

router = APIRouter(prefix="/api/etfs", tags=["etfs"])
SORTS = {"name", "ter", "fund_size_eur", "y1", "y3", "y5", "vol_3y", "sharpe_3y", "max_dd_3y", "inception", "owners"}
BROKERS = {"nordnet", "nordnet_monthly"}
ASCENDING = {"name", "ter", "vol_3y"}  # sensible default direction when `desc` is not given
M = 1_000_000


class CandidateIn(BaseModel):
    listing_secid: str | None = None


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def _sort_column(sort: str) -> Any:
    if sort in ("y1", "y3", "y5"):
        return EtfListing.returns[sort].as_float()
    if sort == "owners":
        return EtfListing.brokers["nordnet"]["owners"].as_float()
    return getattr(EtfListing, sort)


def _filtered(
    q: str | None,
    category: str | None,
    provider: str | None,
    max_ter: float | None,
    min_size: float | None,
    equity_min: float | None,
    equity_max: float | None,
    distribution: str | None,
    hedged: bool | None,
    domicile: str | None,
    min_years: float | None,
    broker: str | None = None,
) -> Select[Any]:
    """Percent inputs (max_ter, equity_*) are percentages, min_size is M EUR."""
    stmt = select(EtfListing)
    if q and q.strip():
        pat = _like(q.strip())
        tickers = EtfListing.listings.cast(Text).ilike('%"ticker": "%' + pat[1:-1] + '%"%', escape="\\")
        stmt = stmt.where(
            or_(EtfListing.name.ilike(pat, escape="\\"), EtfListing.isin.ilike(pat, escape="\\"), tickers)
        )
    if category:
        stmt = stmt.where(EtfListing.category == category)
    if provider:
        stmt = stmt.where(EtfListing.provider == provider)
    if max_ter is not None:
        stmt = stmt.where(EtfListing.ter <= max_ter / 100 + 1e-9)
    if min_size is not None:
        stmt = stmt.where(EtfListing.fund_size_eur >= min_size * M)
    if equity_min is not None:
        stmt = stmt.where(EtfListing.equity_share >= equity_min / 100 - 1e-9)
    if equity_max is not None:
        stmt = stmt.where(EtfListing.equity_share <= equity_max / 100 + 1e-9)
    if distribution in ("acc", "dist"):
        stmt = stmt.where(EtfListing.distribution == distribution)
    if hedged is not None:
        stmt = stmt.where(EtfListing.hedged.is_(hedged))
    if domicile:
        stmt = stmt.where(EtfListing.domicile == domicile.upper())
    if broker in BROKERS:
        stmt = stmt.where(EtfListing.brokers.has_key("nordnet"))
        if broker == "nordnet_monthly":
            stmt = stmt.where(EtfListing.brokers["nordnet"]["monthly_saving"].as_boolean().is_(True))
    if min_years:
        stmt = stmt.where(EtfListing.inception <= today() - timedelta(days=int(min_years * 365.25)))
    return stmt


def _out(e: EtfListing) -> dict[str, Any]:
    return {
        c.name: getattr(e, c.name)
        for c in EtfListing.__table__.columns
        if c.name not in ("id", "seen_at", "updated_at")
    }


def _flags(db: Session, isins: list[str]) -> tuple[dict[str, Instrument], set[int]]:
    """Instruments of these ISINs, and the ids currently held."""
    insts = {i.isin: i for i in db.scalars(select(Instrument).where(Instrument.isin.in_(isins))) if i.isin}
    held = {p.instrument_id for p in positions(db)} & {i.id for i in insts.values()} if insts else set()
    return insts, held


def _row(e: EtfListing, insts: dict[str, Instrument], held: set[int]) -> dict[str, Any]:
    inst = insts.get(e.isin)
    return {
        **_out(e),
        "instrument_id": inst.id if inst else None,
        "candidate_instrument_id": inst.id if inst and inst.meta.get("candidate") else None,
        "held": bool(inst and inst.id in held),
    }


def _counts(db: Session, col: Any, limit: int) -> list[dict[str, Any]]:
    n = func.count()
    rows = db.execute(select(col, n).where(col.is_not(None)).group_by(col).order_by(n.desc(), col).limit(limit)).all()
    return [{"name": name, "count": c} for name, c in rows]


@router.get("")
def list_etfs(
    user: CurrentUser,
    db: Db,
    q: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    max_ter: float | None = None,
    min_size: float | None = None,
    equity_min: float | None = None,
    equity_max: float | None = None,
    distribution: str | None = None,
    hedged: bool | None = None,
    domicile: str | None = None,
    min_years: float | None = None,
    broker: str | None = None,
    sort: str = "fund_size_eur",
    desc: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if broker and broker not in BROKERS:
        raise HTTPException(422, f"broker must be one of {', '.join(sorted(BROKERS))}")
    if sort not in SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(sorted(SORTS))}")
    limit = max(1, min(limit, 200))
    stmt = _filtered(
        q,
        category,
        provider,
        max_ter,
        min_size,
        equity_min,
        equity_max,
        distribution,
        hedged,
        domicile,
        min_years,
        broker,
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    col = _sort_column(sort)
    descending = (sort not in ASCENDING) if desc is None else desc
    order = col.desc().nulls_last() if descending else col.asc().nulls_last()
    page = list(db.scalars(stmt.order_by(order, EtfListing.id).limit(limit).offset(max(offset, 0))))
    insts, held = _flags(db, [e.isin for e in page])
    return {
        "total": total,
        "rows": [_row(e, insts, held) for e in page],
        "updated_at": db.scalar(select(func.max(EtfListing.updated_at))),
        "categories": _counts(db, EtfListing.category, 60),
        "providers": _counts(db, EtfListing.provider, 100),
        "nordnet_total": db.scalar(select(func.count()).where(EtfListing.brokers.has_key("nordnet"))) or 0,
    }


@router.post("/refresh")
def refresh(user: CurrentUser, db: Db) -> dict[str, int]:
    return {"run_id": enqueue(db, "etfs.universe").id}


@router.get("/candidates")
def candidates(user: CurrentUser, db: Db) -> list[dict[str, Any]]:
    insts = list(
        db.scalars(
            select(Instrument).where(Instrument.meta["candidate"].as_boolean().is_(True)).order_by(Instrument.name)
        )
    )
    eff = profiles.effective_many(db, insts)
    rows = db.execute(select(Price.instrument_id, func.max(Price.date)).group_by(Price.instrument_id)).all()
    last = {i: d for i, d in rows}
    out = []
    for i in insts:
        e = eff[i.id]
        mstar = next((r for r in e.rows if r.source == "morningstar"), None)
        out.append(
            {
                "id": i.id,
                "name": i.name,
                "isin": i.isin,
                "currency": i.currency,
                "ter": e.data.get("ter"),
                "asset_classes": e.data.get("asset_classes"),
                "last_price": last.get(i.id),
                "price_problem": i.meta.get("price_problem") or i.meta.get("price_error"),
                "profile_source": "morningstar" if mstar else None,
                "profile_as_of": mstar.as_of if mstar else None,
            }
        )
    return out


@router.get("/{isin}")
def get_etf(isin: str, user: CurrentUser, db: Db) -> dict[str, Any]:
    e = db.scalar(select(EtfListing).where(EtfListing.isin == isin.upper()))
    if e is None:
        raise HTTPException(404, "ETF not found")
    insts, held = _flags(db, [e.isin])
    inst = insts.get(e.isin)
    profile = None
    if inst:
        data, sources = profiles.effective(db, inst)
        profile = {"data": data, "sources": sources}
    return {**_row(e, insts, held), "updated_at": e.updated_at, "profile": profile}


@router.post("/{isin}/candidate")
def add_candidate(isin: str, body: CandidateIn, user: CurrentUser, db: Db) -> dict[str, int]:
    e = db.scalar(select(EtfListing).where(EtfListing.isin == isin.upper()))
    if e is None:
        raise HTTPException(404, "ETF not found")
    listings = e.listings or []
    if body.listing_secid:
        chosen = next((x for x in listings if x.get("secid") == body.listing_secid), None)
        if chosen is None:
            raise HTTPException(422, "unknown listing")
    else:
        chosen = next((x for x in listings if x.get("secid") == e.secid), listings[0] if listings else None)
    secid = (chosen or {}).get("secid") or e.secid
    meta = {"candidate": True, "ms_secid": secid, "ms_type": morningstar.ETF, "ms_universe": morningstar.ETF_UNIVERSE}
    inst = db.scalar(select(Instrument).where(Instrument.isin == e.isin))
    if inst is None:
        inst = Instrument(
            type="etf",
            name=e.name,
            isin=e.isin,
            currency=(chosen or {}).get("currency") or e.close_currency or "EUR",
            symbol=(chosen or {}).get("ticker"),
            mic=(chosen or {}).get("exchange"),
            meta=meta,
        )
        db.add(inst)
        db.flush()
    else:
        # keep what is known (secid chosen earlier, price source); only fill the Morningstar ids that are missing
        inst.meta = {**meta, **{k: v for k, v in inst.meta.items() if v is not None}, "candidate": True}
    audit(db, user.username, "etf.candidate.add", "instrument", inst.id)
    return {"instrument_id": inst.id, "run_id": enqueue(db, "etfs.prepare").id}


@router.delete("/{isin}/candidate", status_code=204)
def remove_candidate(isin: str, user: CurrentUser, db: Db) -> None:
    inst = db.scalar(select(Instrument).where(Instrument.isin == isin.upper()))
    if inst is None or not inst.meta.get("candidate"):
        raise HTTPException(404, "not a candidate")
    inst.meta = {k: v for k, v in inst.meta.items() if k != "candidate"}
    audit(db, user.username, "etf.candidate.remove", "instrument", inst.id)
