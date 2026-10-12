"""Duplicate instruments: find likely copies of one security and merge them into one.

Statement imports create instruments without an ISIN; a later broker history creates the same security again with
one. Every table that points at an instrument is repointed, discovered from the schema so a new table cannot be
forgotten.
"""

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import Column, Table, UniqueConstraint, and_, delete, exists, func, select, update
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ingest.models import SourceMapping
from app.modules.ledger.models import Posting, Transaction
from app.modules.pricing.providers import morningstar
from app.platform.audit import audit
from app.platform.db import Base

CONFLICT = re.compile(r"instrument #(\d+)")
FILL = ("isin", "symbol", "mic", "price_symbol", "asset_class")


class MergeError(ValueError):
    """The two instruments cannot be merged (message is user-facing)."""


def instrument_refs() -> list[Column[Any]]:
    """Every column that holds an instrument id: foreign keys to instrument.id, and plain `instrument_id` columns."""
    import app.models_all  # noqa: F401 - makes Base.metadata complete

    out = []
    for t in Base.metadata.sorted_tables:
        if t.name == "instrument":
            continue
        for c in t.columns:
            if any(fk.column.table.name == "instrument" and fk.column.name == "id" for fk in c.foreign_keys) or (
                c.name == "instrument_id"
            ):
                out.append(c)
    return out


def _unique_siblings(table: Table, col: Column[Any]) -> list[list[Column[Any]]]:
    """For each unique constraint or index containing `col`, its other columns."""
    sets = []
    for con in table.constraints:
        if isinstance(con, UniqueConstraint) and col.name in con.columns:
            sets.append([c for c in con.columns if c.name != col.name])
    for ix in table.indexes:
        if ix.unique and any(c.name == col.name for c in ix.columns):
            sets.append([c for c in ix.columns if c.name != col.name and isinstance(c, Column)])
    return sets


def _snapshot(i: Instrument) -> dict[str, Any]:
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
        "meta": dict(i.meta),
    }


def merge_instruments(db: Session, keep_id: int, drop_id: int, actor: str = "system") -> dict[str, int]:
    """Move everything from `drop_id` to `keep_id`, then delete `drop_id`. Returns rows moved per table.

    On a unique conflict (the same price date and source, the same profile source, ...) the keep side's row stays and
    the drop side's is deleted; those are counted as `<table>:dropped`. The caller commits (one transaction)."""
    if keep_id == drop_id:
        raise MergeError("Cannot merge an instrument into itself")
    keep, drop = db.get(Instrument, keep_id), db.get(Instrument, drop_id)
    if keep is None or drop is None:
        raise MergeError("Instrument not found")
    if keep.type == "cash" or drop.type == "cash":
        raise MergeError("Cash instruments cannot be merged")
    if keep.currency != drop.currency:
        raise MergeError(f"Different currencies ({keep.currency} and {drop.currency}); these are not the same line")
    if keep.isin and drop.isin and keep.isin != drop.isin:  # two ISINs are two securities, whatever the names say
        raise MergeError(f"Different ISINs ({keep.isin} and {drop.isin}); these are different securities")
    before = {"keep": _snapshot(keep), "drop": _snapshot(drop)}
    counts: dict[str, int] = {}
    for col in instrument_refs():
        t = col.table
        for others in _unique_siblings(t, col):
            k = t.alias("k")
            same = and_(k.c[col.name] == keep_id, *(k.c[c.name] == c for c in others))
            res = db.execute(delete(t).where(col == drop_id, exists(select(1).select_from(k).where(same)).correlate(t)))
            if res.rowcount:
                counts[f"{t.name}:dropped"] = counts.get(f"{t.name}:dropped", 0) + res.rowcount
        moved = db.execute(update(t).where(col == drop_id).values({col.name: keep_id})).rowcount
        if moved:
            counts[t.name] = counts.get(t.name, 0) + moved
    mapped = db.execute(
        update(SourceMapping)
        .where(SourceMapping.kind == "instrument", SourceMapping.value == str(drop_id))
        .values(value=str(keep_id))
    ).rowcount
    if mapped:
        counts[SourceMapping.__tablename__] = counts.get(SourceMapping.__tablename__, 0) + mapped

    db.expire_all()
    keep, drop = db.get(Instrument, keep_id), db.get(Instrument, drop_id)
    assert keep is not None and drop is not None
    moving_isin = drop.isin if not keep.isin else None
    if moving_isin:
        drop.isin = None  # isin is UNIQUE: free it before the keep side takes it
        db.flush()
    for f in FILL:
        if not getattr(keep, f):
            setattr(keep, f, moving_isin if f == "isin" else getattr(drop, f))
    meta = {**drop.meta, **keep.meta}
    meta.pop("isin_conflict", None)
    if str(meta.get("price_problem", "")).startswith("same ISIN"):
        meta.pop("price_problem")
    keep.meta = meta
    db.delete(drop)
    db.flush()
    audit(
        db,
        actor,
        "instrument.merge",
        "instrument",
        keep_id,
        before=before,
        after={"keep": _snapshot(keep), "dropped_id": drop_id, "moved": counts},
    )
    return counts


# ---- finding duplicates ------------------------------------------------------------------------------


def _name_key(i: Instrument) -> tuple[Any, ...]:
    if i.type in ("fund", "etf"):
        words, _ = morningstar.fund_words(i.name, i.currency)
        return ("fund", i.currency, tuple(sorted(words)))
    words, cls = morningstar.split_name(i.name)
    return ("stock", i.currency, tuple(words), cls)


def _markers(i: Instrument) -> set[str]:
    return morningstar.fund_words(i.name, i.currency)[1] if i.type in ("fund", "etf") else set()


def find_duplicates(db: Session) -> list[dict[str, Any]]:
    """Groups of instruments that are probably one security, each with a suggested one to keep."""
    insts = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.type != "cash"))}
    parent = {i: i for i in insts}
    reasons: dict[int, set[str]] = defaultdict(set)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int, why: str) -> None:
        parent[find(a)] = find(b)
        reasons[a].add(why)
        reasons[b].add(why)

    for i in insts.values():
        m = CONFLICT.search(str(i.meta.get("isin_conflict") or ""))
        if m and int(m.group(1)) in insts:
            union(i.id, int(m.group(1)), "ISIN conflict found by the price job")
    by_secid: dict[str, list[int]] = defaultdict(list)
    by_name: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for i in insts.values():
        if i.meta.get("ms_secid"):
            by_secid[str(i.meta["ms_secid"])].append(i.id)
        key = _name_key(i)
        if key[2]:
            by_name[key].append(i.id)
    for ids in by_secid.values():
        for other in ids[1:]:
            union(ids[0], other, "same Morningstar id")
    for ids in by_name.values():
        for n, a in enumerate(ids):
            for b in ids[n + 1 :]:
                ia, ib = insts[a], insts[b]
                if ia.isin and ib.isin and ia.isin != ib.isin:
                    continue  # two ISINs are two securities
                if not morningstar._markers_ok(_markers(ia), _markers(ib)) or not morningstar._markers_ok(
                    _markers(ib), _markers(ia)
                ):
                    continue
                union(a, b, "same name")
    groups: dict[int, list[int]] = defaultdict(list)
    for iid in insts:
        groups[find(iid)].append(iid)
    groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not groups:
        return []
    used = [i for g in groups.values() for i in g]
    held: dict[int, Any] = {}
    q = (
        select(Posting.instrument_id, func.count())
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Transaction.voided_at.is_(None), Posting.instrument_id.in_(used))
        .group_by(Posting.instrument_id)
    )
    postings: dict[int, int] = {}
    for inst, n in db.execute(q).all():
        postings[inst] = n
    qty = (
        select(Posting.instrument_id, func.sum(Posting.quantity))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Transaction.voided_at.is_(None), Posting.account_id.is_not(None), Posting.instrument_id.in_(used))
        .group_by(Posting.instrument_id)
    )
    for inst, total in db.execute(qty).all():
        held[inst] = total

    def row(i: Instrument) -> dict[str, Any]:
        feed = i.meta.get("price_source") or ("yahoo" if i.price_symbol else None)
        return {
            "id": i.id,
            "name": i.name,
            "isin": i.isin,
            "type": i.type,
            "currency": i.currency,
            "quantity": str(held.get(i.id, 0)),
            "postings": postings.get(i.id, 0),
            "price_source": feed,
            "created_from": i.meta.get("created_from"),
        }

    out = []
    for ids in groups.values():
        members = [insts[i] for i in ids]
        best = max(
            members,
            key=lambda i: (
                bool(i.isin),
                postings.get(i.id, 0),
                bool(i.price_symbol or i.meta.get("price_source")),
                -i.id,
            ),
        )
        out.append(
            {
                "keep_id": best.id,
                "reasons": sorted({r for i in ids for r in reasons[i]}),
                "instruments": [
                    {**row(i), "mergeable": i.currency == best.currency} for i in sorted(members, key=lambda i: i.id)
                ],
            }
        )
    return sorted(out, key=lambda g: g["instruments"][0]["name"].casefold())
