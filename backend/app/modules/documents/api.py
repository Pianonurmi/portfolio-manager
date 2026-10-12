"""Paperless-ngx search and links (documents stay in Paperless), tags and notes."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.documents.models import SUBJECTS, DocumentLink, Note, Tag, TagLink
from app.platform.auth import CurrentUser, Db
from app.platform.connections import get_connection, secret
from app.platform.outbound import OutboundClient
from app.platform.registry import SearchHit, search_provider

router = APIRouter(prefix="/api/documents", tags=["documents"])


def paperless(db: Session) -> tuple[str, str]:
    c = get_connection(db, "paperless")
    if c is None or not c.secret_enc or not (c.config or {}).get("url"):
        raise HTTPException(404, "Connect Paperless-ngx in Settings → Connections (URL and API token)")
    return str(c.config["url"]).rstrip("/"), secret(c) or ""


def _check(subject_type: str) -> None:
    if subject_type not in SUBJECTS:
        raise HTTPException(422, f"Unknown subject {subject_type}")


@router.get("/search")
def search(_: CurrentUser, db: Db, q: str, client: Any = None) -> list[dict[str, Any]]:
    url, token = paperless(db)
    r = (client or OutboundClient()).get(
        f"{url}/api/documents/",
        params={"query": q, "page_size": 20},
        headers={"Authorization": f"Token {token}", "Accept": "application/json; version=5"},
    )
    if r.status_code >= 400:
        raise HTTPException(502, f"Paperless answered {r.status_code}")
    return [
        {
            "id": d["id"],
            "title": d["title"],
            "created": d.get("created_date") or d.get("created"),
            "url": f"{url}/documents/{d['id']}/details",
        }
        for d in r.json().get("results", [])
    ]


class LinkIn(BaseModel):
    subject_type: str
    subject_id: int
    paperless_id: int
    title: str = Field(max_length=255)
    created_on: str | None = None


@router.get("/links")
def links(_: CurrentUser, db: Db, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
    c = get_connection(db, "paperless")
    base = str((c.config or {}).get("url", "")).rstrip("/") if c else ""
    return [
        {
            "id": d.id,
            "paperless_id": d.paperless_id,
            "title": d.title,
            "created_on": d.created_on,
            "url": f"{base}/documents/{d.paperless_id}/details" if base else None,
        }
        for d in db.scalars(
            select(DocumentLink).where(DocumentLink.subject_type == subject_type, DocumentLink.subject_id == subject_id)
        )
    ]


@router.post("/links", status_code=201)
def add_link(body: LinkIn, _: CurrentUser, db: Db) -> dict[str, int]:
    _check(body.subject_type)
    existing = db.scalar(
        select(DocumentLink).where(
            DocumentLink.subject_type == body.subject_type,
            DocumentLink.subject_id == body.subject_id,
            DocumentLink.paperless_id == body.paperless_id,
        )
    )
    if existing:
        return {"id": existing.id}
    d = DocumentLink(**body.model_dump())
    db.add(d)
    db.flush()
    return {"id": d.id}


@router.delete("/links/{link_id}", status_code=204)
def del_link(link_id: int, _: CurrentUser, db: Db) -> None:
    d = db.get(DocumentLink, link_id)
    if d:
        db.delete(d)


class TagIn(BaseModel):
    subject_type: str
    subject_id: int
    name: str = Field(min_length=1, max_length=64)


@router.get("/tags")
def tags(_: CurrentUser, db: Db, subject_type: str | None = None, subject_id: int | None = None) -> list[str]:
    q = select(Tag.name)
    if subject_type:
        q = q.join(TagLink, TagLink.tag_id == Tag.id).where(
            TagLink.subject_type == subject_type, TagLink.subject_id == subject_id
        )
    return [n for (n,) in db.execute(q.order_by(Tag.name))]


@router.post("/tags", status_code=201)
def add_tag(body: TagIn, _: CurrentUser, db: Db) -> dict[str, str]:
    _check(body.subject_type)
    name = body.name.strip().lower()
    t = db.scalar(select(Tag).where(func.lower(Tag.name) == name)) or Tag(name=name)
    db.add(t)
    db.flush()
    if not db.scalar(
        select(TagLink).where(
            TagLink.tag_id == t.id, TagLink.subject_type == body.subject_type, TagLink.subject_id == body.subject_id
        )
    ):
        db.add(TagLink(tag_id=t.id, subject_type=body.subject_type, subject_id=body.subject_id))
    return {"name": name}


@router.delete("/tags", status_code=204)
def del_tag(subject_type: str, subject_id: int, name: str, _: CurrentUser, db: Db) -> None:
    t = db.scalar(select(Tag).where(Tag.name == name.lower()))
    if t:
        for tl in db.scalars(
            select(TagLink).where(
                TagLink.tag_id == t.id, TagLink.subject_type == subject_type, TagLink.subject_id == subject_id
            )
        ):
            db.delete(tl)


class NoteIn(BaseModel):
    subject_type: str
    subject_id: int
    text: str = Field(min_length=1, max_length=10000)


@router.get("/notes")
def notes(_: CurrentUser, db: Db, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
    return [
        {"id": n.id, "text": n.text, "created_at": n.created_at}
        for n in db.scalars(
            select(Note)
            .where(Note.subject_type == subject_type, Note.subject_id == subject_id)
            .order_by(Note.created_at.desc())
        )
    ]


@router.post("/notes", status_code=201)
def add_note(body: NoteIn, _: CurrentUser, db: Db) -> dict[str, int]:
    _check(body.subject_type)
    n = Note(**body.model_dump())
    db.add(n)
    db.flush()
    return {"id": n.id}


@router.delete("/notes/{note_id}", status_code=204)
def del_note(note_id: int, _: CurrentUser, db: Db) -> None:
    n = db.get(Note, note_id)
    if n:
        db.delete(n)


@search_provider
def _search(db: Session, q: str, limit: int) -> list[SearchHit]:
    like = f"%{q.lower()}%"
    hits = [
        SearchHit(
            kind="document",
            id=str(d.id),
            title=d.title,
            subtitle=f"{d.subject_type} · Paperless #{d.paperless_id}",
            route=f"/net-worth?doc={d.paperless_id}",
        )
        for d in db.scalars(select(DocumentLink).where(func.lower(DocumentLink.title).like(like)).limit(limit))
    ]
    hits += [
        SearchHit(kind="note", id=str(n.id), title=n.text[:80], subtitle=n.subject_type, route="/net-worth")
        for n in db.scalars(select(Note).where(func.lower(Note.text).like(like)).limit(limit))
    ]
    return hits
