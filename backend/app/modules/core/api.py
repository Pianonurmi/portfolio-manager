"""CRUD for people, institutions, accounts (with dated ownership) and instruments."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.core.merge import MergeError, find_duplicates, merge_instruments
from app.modules.core.models import (
    ACCOUNT_KINDS,
    INSTRUMENT_TYPES,
    Account,
    Entity,
    Institution,
    Instrument,
    Ownership,
    Person,
)
from app.modules.core.service import OwnerShare, entity_for_person, set_owners
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today as today_local
from app.platform.explain import ExplainNode, explainer
from app.platform.registry import SearchHit, search_provider

router = APIRouter(prefix="/api", tags=["core"])


# ---- people ---------------------------------------------------------------------------------


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="adult", pattern="^(adult|child)$")
    birth_date: date | None = None
    tracking: str = Field(default="full", pattern="^(full|shared)$")
    is_self: bool = False


class PersonOut(PersonIn):
    id: int
    entity_id: int


def _person_out(db: Session, p: Person) -> PersonOut:
    ent = entity_for_person(db, p)
    return PersonOut(
        id=p.id,
        entity_id=ent.id,
        name=p.name,
        role=p.role,
        birth_date=p.birth_date,
        tracking=p.tracking,
        is_self=p.is_self,
    )


@router.get("/persons")
def list_persons(_: CurrentUser, db: Db) -> list[PersonOut]:
    return [_person_out(db, p) for p in db.scalars(select(Person).order_by(Person.is_self.desc(), Person.id))]


@router.post("/persons", status_code=201)
def create_person(body: PersonIn, user: CurrentUser, db: Db) -> PersonOut:
    p = Person(**body.model_dump())
    db.add(p)
    db.flush()
    out = _person_out(db, p)
    audit(db, user.username, "person.create", "person", p.id, after=body.model_dump(mode="json"))
    return out


@router.put("/persons/{person_id}")
def update_person(person_id: int, body: PersonIn, user: CurrentUser, db: Db) -> PersonOut:
    p = db.get(Person, person_id)
    if p is None:
        raise HTTPException(404, "Person not found")
    before = PersonIn.model_validate(p, from_attributes=True).model_dump(mode="json")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    ent = entity_for_person(db, p)
    ent.name = p.name
    audit(db, user.username, "person.update", "person", p.id, before=before, after=body.model_dump(mode="json"))
    return _person_out(db, p)


@router.get("/entities")
def list_entities(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    rows = db.execute(select(Entity, Person).outerjoin(Person, Person.id == Entity.person_id).order_by(Entity.id))
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "name": e.name,
            "person_id": e.person_id,
            "role": p.role if p else None,
            "tracking": p.tracking if p else None,
            "is_self": p.is_self if p else False,
        }
        for e, p in rows
    ]


# ---- institutions ---------------------------------------------------------------------------


class InstitutionIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = "bank"
    country: str | None = "FI"


@router.get("/institutions")
def list_institutions(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {"id": i.id, "name": i.name, "kind": i.kind, "country": i.country}
        for i in db.scalars(select(Institution).order_by(Institution.name))
    ]


@router.post("/institutions", status_code=201)
def create_institution(body: InstitutionIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if db.scalar(select(Institution).where(func.lower(Institution.name) == body.name.lower())):
        raise HTTPException(409, f"{body.name} already exists")
    i = Institution(**body.model_dump())
    db.add(i)
    db.flush()
    audit(db, user.username, "institution.create", "institution", i.id, after=body.model_dump())
    return {"id": i.id, **body.model_dump()}


# ---- accounts -------------------------------------------------------------------------------


class OwnerIn(BaseModel):
    entity_id: int
    share: Decimal = Field(gt=0, le=1)


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str
    institution_id: int | None = None
    currency: str = "EUR"
    opened_on: date | None = None
    closed_on: date | None = None
    external_ref: str | None = None
    notes: str | None = None
    owners: list[OwnerIn] = []
    owners_from: date | None = None


class AccountOut(BaseModel):
    id: int
    name: str
    kind: str
    kind_label: str
    institution_id: int | None
    institution: str | None
    currency: str
    opened_on: date | None
    closed_on: date | None
    external_ref: str | None
    notes: str | None
    owners: list[dict[str, Any]]


def _account_out(db: Session, a: Account) -> AccountOut:
    today = today_local()
    owners = db.execute(
        select(Ownership, Entity)
        .join(Entity, Entity.id == Ownership.entity_id)
        .where(
            Ownership.account_id == a.id,
            Ownership.valid_from <= today,
            or_(Ownership.valid_to.is_(None), Ownership.valid_to >= today),
        )
    ).all()
    return AccountOut(
        id=a.id,
        name=a.name,
        kind=a.kind,
        kind_label=ACCOUNT_KINDS.get(a.kind, a.kind),
        institution_id=a.institution_id,
        institution=a.institution.name if a.institution else None,
        currency=a.currency,
        opened_on=a.opened_on,
        closed_on=a.closed_on,
        external_ref=a.external_ref,
        notes=a.notes,
        owners=[
            {"entity_id": e.id, "name": e.name, "share": str(o.share), "valid_from": o.valid_from} for o, e in owners
        ],
    )


@router.get("/account-kinds")
def account_kinds(_: CurrentUser) -> dict[str, str]:
    return ACCOUNT_KINDS


@router.get("/accounts")
def list_accounts(_: CurrentUser, db: Db) -> list[AccountOut]:
    return [
        _account_out(db, a) for a in db.scalars(select(Account).order_by(Account.closed_on.is_not(None), Account.name))
    ]


@router.get("/accounts/{account_id}")
def get_account(account_id: int, _: CurrentUser, db: Db) -> AccountOut:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    return _account_out(db, a)


def _apply_owners(db: Session, a: Account, body: AccountIn) -> None:
    if body.owners:
        try:
            set_owners(
                db,
                a,
                [OwnerShare(o.entity_id, o.share) for o in body.owners],
                body.owners_from or a.opened_on or date(1900, 1, 1),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/accounts", status_code=201)
def create_account(body: AccountIn, user: CurrentUser, db: Db) -> AccountOut:
    if body.kind not in ACCOUNT_KINDS:
        raise HTTPException(422, f"Unknown account kind {body.kind}")
    a = Account(**body.model_dump(exclude={"owners", "owners_from"}))
    db.add(a)
    db.flush()
    _apply_owners(db, a, body)
    audit(db, user.username, "account.create", "account", a.id, after=body.model_dump(mode="json"))
    return _account_out(db, a)


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: AccountIn, user: CurrentUser, db: Db) -> AccountOut:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    for k, v in body.model_dump(exclude={"owners", "owners_from"}).items():
        setattr(a, k, v)
    _apply_owners(db, a, body)
    audit(db, user.username, "account.update", "account", a.id, after=body.model_dump(mode="json"))
    return _account_out(db, a)


# ---- instruments ----------------------------------------------------------------------------


class InstrumentIn(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=255)
    isin: str | None = Field(default=None, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
    symbol: str | None = None
    mic: str | None = None
    currency: str = "EUR"
    price_symbol: str | None = None
    asset_class: str | None = None


def instrument_out(i: Instrument) -> dict[str, Any]:
    return {
        "id": i.id,
        "type": i.type,
        "name": i.name,
        "isin": i.isin,
        "symbol": i.symbol,
        "mic": i.mic,
        "currency": i.currency,
        "price_symbol": i.price_symbol,
        "asset_class": i.asset_class,
    }


@router.get("/instruments")
def list_instruments(_: CurrentUser, db: Db, q: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Instrument).order_by(Instrument.name)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Instrument.name).like(like),
                func.lower(Instrument.symbol).like(like),
                Instrument.isin == q.upper(),
            )
        )
    return [instrument_out(i) for i in db.scalars(stmt.limit(200))]


@router.get("/instruments/duplicates")
def instrument_duplicates(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    """Groups of instruments that look like one security, with a suggested one to keep."""
    return find_duplicates(db)


class MergeIn(BaseModel):
    drop_id: int


@router.post("/instruments/{keep_id}/merge")
def merge_instrument(keep_id: int, body: MergeIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Fold `drop_id` into `keep_id`: postings, prices, profiles, mappings move; `drop_id` is deleted."""
    try:
        moved = merge_instruments(db, keep_id, body.drop_id, user.username)
    except MergeError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"keep_id": keep_id, "dropped_id": body.drop_id, "moved": moved}


@router.post("/instruments", status_code=201)
def create_instrument(body: InstrumentIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if body.type not in INSTRUMENT_TYPES:
        raise HTTPException(422, f"Unknown instrument type {body.type}")
    if body.isin and db.scalar(select(Instrument).where(Instrument.isin == body.isin)):
        raise HTTPException(409, f"An instrument with ISIN {body.isin} exists")
    i = Instrument(**body.model_dump())
    db.add(i)
    db.flush()
    audit(db, user.username, "instrument.create", "instrument", i.id, after=body.model_dump())
    return instrument_out(i)


@router.put("/instruments/{instrument_id}")
def update_instrument(instrument_id: int, body: InstrumentIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    i = db.get(Instrument, instrument_id)
    if i is None:
        raise HTTPException(404, "Instrument not found")
    for k, v in body.model_dump().items():
        setattr(i, k, v)
    audit(db, user.username, "instrument.update", "instrument", i.id, after=body.model_dump())
    return instrument_out(i)


# ---- profiles -------------------------------------------------------------------------------


def _profile_out(db: Session, inst: Instrument) -> dict[str, Any]:
    from app.modules.portfolio import profiles

    e = profiles.effective_many(db, [inst])[inst.id]
    return {
        "instrument_id": inst.id,
        "name": inst.name,
        "data": e.data,
        "sources": e.sources,
        "stored": [{"source": r.source, "as_of": r.as_of, "fetched_at": r.fetched_at, "data": r.data} for r in e.rows],
    }


@router.get("/instruments/{instrument_id}/profile")
def get_profile(instrument_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Effective profile (per-field winner and its source) plus every stored source row."""
    i = db.get(Instrument, instrument_id)
    if i is None:
        raise HTTPException(404, "Instrument not found")
    return _profile_out(db, i)


class ProfileIn(BaseModel):
    data: dict[str, Any]


@router.put("/instruments/{instrument_id}/profile")
def put_profile(instrument_id: int, body: ProfileIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Hand overrides, field by field: they beat every fetched source. A null or empty value removes the field."""
    from app.modules.portfolio import profiles

    i = db.get(Instrument, instrument_id)
    if i is None:
        raise HTTPException(404, "Instrument not found")
    try:
        patch = profiles.clean_manual(body.data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    before = next((r.data for r in profiles.effective_many(db, [i])[i.id].rows if r.source == "manual"), {})
    after = profiles.set_manual(db, i, patch)
    audit(db, user.username, "instrument.profile", "instrument", i.id, before=before, after=after)
    return _profile_out(db, i)


# ---- search & explain -------------------------------------------------------------------------


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    like = f"%{q.lower()}%"
    hits = [
        SearchHit(
            kind="account",
            id=str(a.id),
            title=a.name,
            subtitle=ACCOUNT_KINDS.get(a.kind, a.kind),
            route=f"/accounts/{a.id}",
            ref=f"acct:{a.id}",
        )
        for a in db.scalars(select(Account).where(func.lower(Account.name).like(like)).limit(limit))
    ]
    hits += [
        SearchHit(
            kind="instrument",
            id=str(i.id),
            title=i.name,
            subtitle=" · ".join(x for x in (i.symbol, i.isin) if x),
            route=f"/instruments/{i.id}",
        )
        for i in db.scalars(
            select(Instrument)
            .where(
                or_(
                    func.lower(Instrument.name).like(like),
                    func.lower(Instrument.symbol).like(like),
                    func.lower(Instrument.isin).like(like),
                )
            )
            .limit(limit)
        )
    ]
    hits += [
        SearchHit(kind="person", id=str(p.id), title=p.name, subtitle="Person", route="/settings/people")
        for p in db.scalars(select(Person).where(func.lower(Person.name).like(like)).limit(limit))
    ]
    return hits


@explainer("owners")
def _explain_owners(db: Session, account_id: str) -> ExplainNode:
    a = db.get(Account, int(account_id))
    if a is None:
        raise LookupError("account not found")
    out = _account_out(db, a)
    return ExplainNode(
        label=f"Ownership of {a.name}",
        formula="dated ownership rows valid today",
        children=[
            ExplainNode(
                label=o["name"],
                value=f"{Decimal(o['share']) * 100:.0f} %",
                source="manual",
                note=f"since {o['valid_from']:%d.%m.%Y}",
            )
            for o in out.owners
        ],
    )
