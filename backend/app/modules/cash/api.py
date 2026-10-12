"""Cash: deposit rates (market references and the household's own offers) and the interest ladder planner."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.modules.cash import ladder, service
from app.modules.cash.models import MarketRate
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.jobs import enqueue
from app.platform.rules import rules_for

router = APIRouter(prefix="/api/cash", tags=["cash"])
MAX_RATE = 0.25


class OfferIn(BaseModel):
    rate: float | None = None  # fraction; null removes the override
    bank: str | None = Field(default=None, max_length=80)


class RatesIn(BaseModel):
    products: dict[str, OfferIn]


class RungIn(BaseModel):
    tenor: int
    share: float | None = None


class LadderIn(BaseModel):
    amount: float
    liquid: float = 0
    rungs: list[RungIn] | None = None
    horizon: int = 24
    rollover: Literal["longest", "same", "savings"] = "longest"
    rates: dict[str, float] | None = None


def _rates_view(db: Db, user: CurrentUser) -> dict[str, Any]:
    prods = service.rates(db, user)
    series = [
        {"key": k, "label": v["label"], "date": v["date"].isoformat(), "value": v["value"]}
        for k, v in service.latest(db).items()
    ]
    updated: datetime | None = db.scalar(select(func.max(MarketRate.fetched_at)))
    return {"products": prods, "series": series, "updated_at": updated.isoformat() if updated else None}


@router.get("/rates")
def get_rates(user: CurrentUser, db: Db) -> dict[str, Any]:
    return _rates_view(db, user)


@router.put("/rates")
def put_rates(body: RatesIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    saved = {k: v for k, v in ((user.settings or {}).get("deposit_rates") or {}).items() if k in service.PRODUCTS}
    for product, offer in body.products.items():
        if product not in service.PRODUCTS:
            raise HTTPException(422, f"Unknown product {product!r}")
        if offer.rate is None:
            saved.pop(product, None)
            continue
        if not 0 <= offer.rate < MAX_RATE:
            raise HTTPException(422, f"Rate for {product} must be between 0 % and {MAX_RATE * 100:.0f} %")
        saved[product] = {"rate": offer.rate, "bank": (offer.bank or "").strip() or None}
    user.settings = {**(user.settings or {}), "deposit_rates": saved}
    return _rates_view(db, user)


@router.post("/rates/refresh")
def refresh_rates(db: Db, user: CurrentUser) -> dict[str, int]:
    return {"run_id": enqueue(db, "rates.refresh").id}


def _tax_rate(db: Db, year: int) -> float:
    try:
        return float(rules_for(db, "tax", year)["capital_income"]["brackets"][0]["rate"])
    except (LookupError, KeyError, IndexError):
        return 0.30


@router.post("/ladder")
def post_ladder(body: LadderIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    day = today()
    rates = {p: v["rate"] for p, v in service.rates(db, user).items()}
    rates.update(body.rates or {})
    try:
        return ladder.build_ladder(
            body.amount,
            body.liquid,
            [r.model_dump() for r in body.rungs] if body.rungs is not None else None,
            body.horizon,
            body.rollover,
            rates,
            _tax_rate(db, day.year),
            day,
        )
    except ladder.LadderError as exc:
        raise HTTPException(422, str(exc)) from exc
