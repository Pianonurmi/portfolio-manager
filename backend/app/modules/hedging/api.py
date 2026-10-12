"""Hedging without selling: fits to the indices, strategy simulation and Nordnet products to do it with."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.modules.hedging import engine, service
from app.modules.hedging.models import HedgeProduct
from app.modules.portfolio.valuation import Scope
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.jobs import enqueue

router = APIRouter(prefix="/api/hedging", tags=["hedging"])


class StrategyIn(BaseModel):
    kind: str
    ratio: float = Field(default=1.0, ge=0, le=2)
    tenor: int = Field(default=3, ge=1, le=24)
    strike: float = Field(default=0.90, gt=0.3, le=1.2)
    option_fee_pct: float = Field(default=0.008, ge=0, le=0.05)  # brokerage, share of the premium
    option_fee_min: float = Field(default=15.0, ge=0, le=500)  # € per order
    clearing_pct: float = Field(default=0.0075, ge=0, le=0.05)
    mini_cost: float = Field(default=0.015, ge=0, le=0.2)
    mini_leverage: float = Field(default=5.0, ge=1.5, le=50)
    bear_factor: float = Field(default=2.0, ge=1, le=20)
    bear_cost: float | None = Field(default=None, ge=0, le=0.5)
    label: str | None = None


class SimulateIn(BaseModel):
    scope: str | None = None
    index: str | None = None  # None: the index that explains the portfolio best
    months: int = Field(default=12, ge=1, le=60)
    strategies: list[StrategyIn] | None = None  # None: the standard comparison
    index_vol: float | None = Field(default=None, gt=0, le=1.5)
    skew: float = Field(default=0.03, ge=0, le=0.3)
    rate: float | None = Field(default=None, ge=-0.02, le=0.2)
    dividend_yield: float | None = Field(default=None, ge=0, le=0.1)
    losses_usable: bool = True


def _scope(s: str | None) -> Scope:
    try:
        return Scope.parse(s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _rate(db: Any, user: Any) -> float:
    """Risk-free rate for option prices: the 3-month deposit reference, else 2 %."""
    try:
        from app.modules.cash import service as cash

        r = cash.rates(db, user).get("3m") or {}
        return float(r.get("market_rate") or r.get("rate") or 0.02)
    except Exception:  # noqa: BLE001
        return 0.02


@router.get("")
def overview(user: CurrentUser, db: Db, scope: str | None = None) -> dict[str, Any]:
    on = today()
    fits = service.fits(db, _scope(scope), on)
    tp = service.tax_position(db, _scope(scope), on)
    n = db.scalar(select(func.count()).select_from(HedgeProduct)) or 0
    return {
        "fits": fits,
        "best": fits[0]["key"] if fits else None,
        "tax": tp,
        "rate": _rate(db, user),
        "products": n,
        "products_updated": db.scalar(select(func.max(HedgeProduct.seen_at))),
        "defaults": [s.__dict__ for s in engine.default_strategies()],
    }


@router.post("/simulate")
def simulate(body: SimulateIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    from app.modules.portfolio.api import class_means

    sc, on = _scope(body.scope), today()
    fits = service.fits(db, sc, on)
    fit = next((f for f in fits if f["key"] == body.index), None) if body.index else (fits[0] if fits else None)
    if body.index and fit is None:
        raise HTTPException(422, "Not enough shared history with that index")
    mkt = service.market(db, sc, on, fit, class_means(db, user), _rate(db, user) if body.rate is None else body.rate)
    if body.index_vol is not None:
        mkt.index_vol = body.index_vol
    if body.dividend_yield is not None:
        mkt.dividend_yield = body.dividend_yield
    mkt.skew, mkt.losses_usable = body.skew, body.losses_usable
    mkt.index_key = fit["key"] if fit else None
    strategies = (
        [engine.Strategy(**s.model_dump()) for s in body.strategies] if body.strategies else engine.default_strategies()
    )
    if not any(s.kind == "none" for s in strategies):
        strategies.insert(0, engine.Strategy("none"))
    try:
        out = engine.simulate(mkt, engine.Run(months=body.months, strategies=strategies))
    except engine.HedgeError as exc:
        raise HTTPException(422, str(exc)) from exc
    key = fit["key"] if fit else None
    out["index"] = fit
    out["products"] = (
        {"short": service.suggested_products(db, key, "short"), "bear": service.suggested_products(db, key, "bear")}
        if key
        else {"short": [], "bear": []}
    )
    out["notes"] = [
        "The portfolio moves as β × the index plus what the index does not explain; a hedge only covers the first "
        "part (basis risk).",
        "A hedge's gain is taxed when it is closed, while the fall it offsets stays an unrealised, non-deductible loss "
        "in the portfolio. Selling instead realises the portfolio's gains and their tax now.",
        "Options are priced with Black–Scholes at the index's realised volatility plus the skew premium; real quotes "
        "differ. Standardised options trade in the AOT only (not in an osakesäästötili), and at Nordnet only on "
        "the Nordic exchanges: puts and collars on other indices are priced as if they existed (marked as not "
        "available). Fees: brokerage 0.40–0.80 % of the premium (at least 15 €) plus 0.75 % clearing; Nordnet "
        "Markets products cost nothing to trade over 100 €.",
        "Short mini futures pay financing (about 3 % over the reference rate, less what the short earns) and stop out "
        "if the index rises to their barrier; bear certificates lose value in choppy markets (daily reset).",
    ]
    return out


@router.post("/refresh")
def refresh(_: CurrentUser, db: Db) -> dict[str, int]:
    return {"run_id": enqueue(db, "hedge.refresh").id}
