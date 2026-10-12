"""Core domain helpers used by other modules."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Entity, Ownership, Person

ONE = Decimal(1)


def entity_for_person(db: Session, person: Person) -> Entity:
    ent = db.scalar(select(Entity).where(Entity.person_id == person.id))
    if ent is None:
        ent = Entity(kind="person", person_id=person.id, name=person.name)
        db.add(ent)
        db.flush()
    return ent


def shares_on(db: Session, account_id: int, on: date) -> dict[int, Decimal]:
    """Ownership shares {entity_id: share} valid on a date. Unowned accounts return {}."""
    rows = db.scalars(
        select(Ownership).where(
            Ownership.account_id == account_id,
            Ownership.valid_from <= on,
            or_(Ownership.valid_to.is_(None), Ownership.valid_to >= on),
        )
    ).all()
    return {r.entity_id: r.share for r in rows}


@dataclass(frozen=True)
class OwnerShare:
    entity_id: int
    share: Decimal


def set_owners(db: Session, account: Account, owners: list[OwnerShare], valid_from: date) -> None:
    """Replace ownership from `valid_from` on. Earlier periods are kept (dated ownership)."""
    total = sum((o.share for o in owners), Decimal(0))
    if owners and total != ONE:
        raise ValueError(f"ownership shares must sum to 100 %, got {total * 100} %")
    for row in db.scalars(select(Ownership).where(Ownership.account_id == account.id)).all():
        if row.valid_from >= valid_from:
            db.delete(row)
        elif row.valid_to is None or row.valid_to >= valid_from:
            row.valid_to = valid_from - timedelta(days=1)
    for o in owners:
        db.add(Ownership(account_id=account.id, entity_id=o.entity_id, share=o.share, valid_from=valid_from))
    db.flush()


def scope_entities(db: Session, scope: str) -> list[int] | None:
    """Resolve a UI scope ("household" or "entity:<id>") to entity ids; None means everyone."""
    if scope in ("", "household"):
        return None
    kind, _, ident = scope.partition(":")
    if kind == "entity" and ident.isdigit():
        return [int(ident)]
    raise ValueError(f"bad scope {scope!r}")
