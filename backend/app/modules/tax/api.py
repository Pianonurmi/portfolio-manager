"""Tax views (TAX-01..08): yearly report per person, OST, gifts, harvesting, calculators."""

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.modules.core.models import Entity
from app.modules.portfolio.valuation import Scope
from app.modules.tax import service
from app.modules.tax.models import Gift
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.registry import undo_handler

router = APIRouter(prefix="/api/tax", tags=["tax"])


def _s(v: Any, key: str = "") -> Any:
    if isinstance(v, Decimal):  # money to cents; quantities keep their precision (crypto)
        return f"{v.normalize():f}" if key == "quantity" else str(v.quantize(service.CENT))
    if isinstance(v, dict):
        return {k: _s(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_s(x) for x in v]
    if hasattr(v, "__dataclass_fields__"):
        return {k: _s(getattr(v, k), k) for k in v.__dataclass_fields__}
    return v


def _scope(scope: str | None) -> Scope:
    sc = Scope.parse(scope or "household")
    if sc.entity_id is None:
        raise HTTPException(422, "Taxes are personal: choose a person in the context bar")
    return sc


@router.get("/year")
def year(_: CurrentUser, db: Db, scope: str | None = None, year: int | None = None) -> dict[str, Any]:
    try:
        return _s(service.year_summary(db, _scope(scope), year or today().year))  # type: ignore[no-any-return]
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/crypto")
def crypto(_: CurrentUser, db: Db, scope: str | None = None, year: int | None = None) -> dict[str, Any]:
    try:
        return _s(service.crypto_report(db, _scope(scope), year or today().year))  # type: ignore[no-any-return]
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/crypto.csv", response_class=PlainTextResponse)
def crypto_csv(_: CurrentUser, db: Db, scope: str | None = None, year: int | None = None) -> PlainTextResponse:
    """One row per disposal, in the order of OmaVero's virtual-currency disposal fields."""
    y = year or today().year
    r = service.crypto_report(db, _scope(scope), y)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(
        [
            "Virtuaalivaluutta",
            "Määrä",
            "Hankintapäivä",
            "Luovutuspäivä",
            "Luovutushinta",
            "Hankintameno",
            "Myyntikulut",
            "Menetelmä",
            "Voitto/tappio",
        ]
    )
    for s in r["sales"]:
        cost = s.cost if s.method == "actual cost" else s.deemed
        w.writerow(
            [
                s.instrument,
                s.quantity,
                s.acquired,
                s.sold,
                s.gross,
                cost,
                (s.gross - s.proceeds).quantize(service.CENT) if s.method == "actual cost" else "0.00",
                s.method,
                s.gain,
            ]
        )
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="crypto-disposals-{y}.csv"'},
    )


@router.get("/ost")
def ost(_: CurrentUser, db: Db, scope: str | None = None) -> list[dict[str, Any]]:
    return _s(service.ost_status(db, _scope(scope), today()))  # type: ignore[no-any-return]


@router.get("/harvest-plan")
def harvest_plan(_: CurrentUser, db: Db, scope: str | None = None) -> dict[str, Any]:
    """Losses worth realising before the year ends, with the tax they save now and their catches."""
    from app.modules.portfolio.financing import harvest_plan as plan

    return plan(db, _scope(scope), today())


@router.get("/harvest")
def harvest(_: CurrentUser, db: Db, scope: str | None = None) -> list[dict[str, Any]]:
    return _s(service.harvest_candidates(db, _scope(scope), today()))  # type: ignore[no-any-return]


class GiftIn(BaseModel):
    donor: str = Field(min_length=1, max_length=128)
    donor_entity_id: int | None = None
    recipient_entity_id: int
    date: date
    amount: Decimal = Field(gt=0)
    note: str | None = None


@router.get("/gifts")
def gifts(_: CurrentUser, db: Db, recipient: int | None = None) -> dict[str, Any]:
    q = select(Gift).order_by(Gift.date.desc())
    if recipient:
        q = q.where(Gift.recipient_entity_id == recipient)
    rows = [
        {
            "id": g.id,
            "donor": g.donor,
            "recipient_entity_id": g.recipient_entity_id,
            "date": g.date,
            "amount": str(g.amount),
            "note": g.note,
        }
        for g in db.scalars(q)
    ]
    windows = {}
    for (eid,) in db.execute(select(Entity.id)):
        w = service.gift_window(db, eid, today())
        if w:
            windows[str(eid)] = _s(w)
    return {"gifts": rows, "windows": windows}


@router.post("/gifts", status_code=201)
def add_gift(body: GiftIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    g = Gift(**body.model_dump())
    db.add(g)
    db.flush()
    audit(db, user.username, "gift.create", "gift", g.id, after=body.model_dump(mode="json"))
    return {
        "id": g.id,
        "undo_id": make_undo(db, "gift.delete", "Add gift", {"id": g.id}),
        "window": _s(service.gift_window(db, body.recipient_entity_id, today())),
    }


@router.delete("/gifts/{gift_id}", status_code=204)
def delete_gift(gift_id: int, user: CurrentUser, db: Db) -> None:
    g = db.get(Gift, gift_id)
    if g:
        db.delete(g)
        audit(db, user.username, "gift.delete", "gift", gift_id)


@undo_handler("gift.delete")
def _undo_gift(db: Any, inv: dict[str, Any]) -> None:
    g = db.get(Gift, int(inv["id"]))
    if g:
        db.delete(g)


@router.get("/calc/inheritance")
def calc_inheritance(_: CurrentUser, db: Db, amount: Decimal, year: int | None = None) -> dict[str, str]:
    return {"amount": str(amount), "tax": str(service.inheritance_tax(db, amount, year or today().year))}


@router.get("/calc/capital-income")
def calc_capital(_: CurrentUser, db: Db, amount: Decimal, year: int | None = None) -> dict[str, str]:
    from app.platform.rules import rules_for

    return {
        "amount": str(amount),
        "tax": str(service.capital_income_tax(amount, rules_for(db, "tax", year or today().year))),
    }
