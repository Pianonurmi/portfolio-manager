"""Local LLM (AI-01..04): OpenAI-compatible endpoint on the LAN only; answers cite figures from the API; never acts."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai import attachments, extract, factsheet, holdings_apply, llm, statements
from app.modules.ai import jobs as ai_jobs
from app.modules.ai.models import EXTRACTION_KINDS, Extraction
from app.modules.core.models import Instrument
from app.modules.crypto import history as crypto_history
from app.modules.documents.api import paperless
from app.modules.ingest.adapters import nordnet
from app.modules.networth.service import GROUP_LABEL, Sheet
from app.modules.portfolio import profiles
from app.modules.portfolio.performance import compute
from app.modules.portfolio.valuation import Scope, Valuer
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today, utcnow
from app.platform.jobs import enqueue
from app.platform.outbound import OutboundClient
from app.platform.registry import undo_handler

router = APIRouter(prefix="/api/ai", tags=["ai"])
SYSTEM = (
    "You are a careful personal-finance assistant for one Finnish household. Answer ONLY from the JSON data provided. "
    "Quote the exact figures you use with their units. If the data does not contain the answer, say so. "
    "Do not give buy/sell instructions or tax advice beyond explaining the numbers. Answer in the user's language."
)


def facts(db: Db) -> dict[str, Any]:
    """Finance only. Health, calendar and habit data are deliberately never part of what the model sees."""
    items = Sheet(db, Scope(None)).items(today())
    groups: dict[str, float] = {}
    for i in items:
        groups[GROUP_LABEL[i.group]] = groups.get(GROUP_LABEL[i.group], 0.0) + float(i.value)
    v = Valuer(db, Scope(None))
    perf = {}
    if v.first_date:
        d, vals, fl = v.daily(v.first_date, today())
        for p in ("YTD", "1Y", "3Y", "Max"):
            r = compute(p, d, vals, fl)
            if r:
                perf[p] = {"twr": r.twr, "xirr": r.xirr, "gain_eur": float(r.gain)}
    holdings = sorted(
        (
            {
                "account": v.accounts[h.account_id].name,
                "instrument_id": h.instrument_id,
                "value_eur": round(float(h.value), 2),
            }
            for h in v.holdings(today())
        ),
        key=lambda x: -float(str(x["value_eur"])),
    )[:25]
    return {
        "as_of": str(today()),
        "net_worth_by_group_eur": groups,
        "net_worth_eur": sum(groups.values()),
        "performance": perf,
        "holdings_top": holdings,
        "accounts": [{"name": i.account.name, "value_eur": round(float(i.value), 2)} for i in items],
    }


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


@router.post("/ask")
def ask(body: AskIn, _: CurrentUser, db: Db, client: Any = None) -> dict[str, Any]:
    data = facts(db)
    answer, model = llm.chat(
        db,
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"DATA:\n{json.dumps(data, default=str)}\n\nQUESTION: {body.question}"},
        ],
        client=client,
    )
    return {"answer": answer, "data_used": data, "model": model}


# ---- plain-language explanations (AI-04) -----------------------------------------------------------

EXPLAIN = (
    "Explain these retirement simulation results to the household in plain language, in at most 150 words. "
    "Use only the numbers given; mention the success probability, what drives it and one or two levers from the "
    "inputs. Do not recommend specific investments. Answer in {lang}."
)


class ExplainIn(BaseModel):
    params: dict[str, Any]
    result: dict[str, Any]
    language: str = Field(default="English", max_length=20)


@router.post("/explain-plan")
def explain_plan(body: ExplainIn, _: CurrentUser, db: Db, client: Any = None) -> dict[str, Any]:
    keep = ("success", "earliest_retirement", "median_depletion_age")
    summary = {
        "inputs": body.params,
        "results": {k: body.result.get(k) for k in keep},
        "median_wealth_by_age": dict(
            list(zip(body.result.get("ages", []), body.result.get("p50", []), strict=False))[::5]
        ),
    }
    text, model = llm.chat(
        db,
        [
            {"role": "system", "content": EXPLAIN.format(lang=body.language)},
            {"role": "user", "content": json.dumps(summary, default=str)},
        ],
        client=client,
        temperature=0.2,
    )
    return {"explanation": text, "model": model}


# ---- document extraction (ING-09, AI-03, DOC-03) ---------------------------------------------------


def _x_out(x: Extraction) -> dict[str, Any]:
    return {
        "id": x.id,
        "source": x.source,
        "filename": x.filename,
        "data": x.data,
        "progress": x.progress,
        "paperless_id": x.paperless_id,
        "title": x.title,
        "kind": x.kind,
        "status": x.status,
        "fields": x.fields,
        "account_id": x.account_id,
        "model": x.model,
        "error": x.error,
        "applied": x.applied,
        "created_at": x.created_at,
    }


class ExtractIn(BaseModel):
    paperless_id: int
    kind: str | None = None


@router.post("/extract", status_code=201)
def extract_document(
    body: ExtractIn, _: CurrentUser, db: Db, client: Any = None, llm_client: Any = None
) -> dict[str, Any]:
    if body.kind is not None and body.kind not in EXTRACTION_KINDS:
        raise HTTPException(422, f"Unknown document type {body.kind}")
    url, token = paperless(db)
    r = (client or OutboundClient()).get(
        f"{url}/api/documents/{body.paperless_id}/",
        headers={"Authorization": f"Token {token}", "Accept": "application/json; version=5"},
    )
    if r.status_code >= 400:
        raise HTTPException(502, f"Paperless answered {r.status_code}")
    doc = r.json()
    text = str(doc.get("content") or "")
    if not text.strip():
        raise HTTPException(422, "Paperless has no text for this document (OCR missing)")
    x = Extraction(
        paperless_id=body.paperless_id, title=str(doc.get("title") or body.paperless_id)[:255], kind="unknown"
    )
    try:
        x.kind, x.fields, x.model = extract.extract(db, text, body.kind, llm_client)
        x.status = "review" if x.fields else "error"
        x.error = None if x.fields else "No values recognised; choose the document type and try again"
        x.account_id = extract.suggest_account(db, x.kind, x.fields)
    except ValueError as exc:
        x.status, x.error = "error", f"The model's answer could not be read: {exc}"
    db.add(x)
    db.flush()
    return _x_out(x)


@router.get("/extractions")
def extractions(_: CurrentUser, db: Db, status: str | None = None) -> list[dict[str, Any]]:
    q = select(Extraction).order_by(Extraction.created_at.desc()).limit(50)
    if status:
        q = q.where(Extraction.status.in_(status.split(",")))
    return [_x_out(x) for x in db.scalars(q)]


class AcceptIn(BaseModel):
    values: dict[str, str] = {}  # corrections by field name
    account_id: int | None = None
    # holdings statements
    mode: str = "compare"  # compare | book | value
    rows: list[dict[str, Any]] | None = None  # edited rows; omitted = as read
    as_of: date | None = None
    new_account: dict[str, str] | None = None  # {"name", "kind"}: create the account the statement belongs to
    # fund factsheets
    instrument_id: int | None = None
    factsheet: dict[str, Any] | None = None  # edited figures; omitted = as read


@router.post("/extractions/{xid}/accept")
def accept(xid: int, body: AcceptIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    x = db.get(Extraction, xid)
    if x is None or x.status != "review":
        raise HTTPException(404, "Nothing to confirm here")
    account_id = body.account_id if body.account_id is not None else x.account_id
    if x.kind == "factsheet":
        x.applied = _accept_factsheet(db, x, body)
        x.status, x.decided_at = "accepted", utcnow()
        audit(db, user.username, "extraction.accept", "extraction", x.id, after={"applied": x.applied})
        return _x_out(x)
    if account_id is None and body.new_account:
        account_id = _create_account(db, x, body.new_account)
    if x.kind == "holdings":
        h = body
        on = h.as_of or extract.parse_date((x.data.get("header") or {}).get("as_of"))
        if on is None:
            raise HTTPException(422, "Give the date the statement's values apply to")
        if account_id is None:
            raise HTTPException(422, "Choose the account the statement belongs to")
        rows = h.rows if h.rows is not None else list(x.data.get("rows") or [])
        try:
            x.applied = holdings_apply.apply_holdings(db, x, rows, account_id, h.mode, on)
        except (ValueError, ArithmeticError) as exc:
            raise HTTPException(422, str(exc)) from exc
        x.data = {**x.data, "rows": rows, "header": {**(x.data.get("header") or {}), "as_of": on.isoformat()}}
    else:
        fields = {k: dict(v) for k, v in x.fields.items()}
        for k, v in body.values.items():
            if k in extract.FIELDS.get(x.kind, {}):
                fields[k] = {**fields.get(k, {}), "value": v, "edited": True}
        try:
            x.applied = extract.apply(db, x, user, fields, account_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        x.fields = fields
    x.account_id = account_id
    x.status, x.decided_at = "accepted", utcnow()
    audit(db, user.username, "extraction.accept", "extraction", x.id, after={"applied": x.applied})
    out = _x_out(x)
    if x.kind == "holdings" and x.applied.get("mode") != "compare":
        out["undo_id"] = make_undo(db, "extraction.undo", f"Statement {x.title}", {"id": x.id}, minutes=60 * 24)
    elif "valuation_id" in x.applied:
        out["undo_id"] = make_undo(db, "valuation.delete", "Extracted value", {"id": x.applied["valuation_id"]})
    return out


def _accept_factsheet(db: Session, x: Extraction, body: AcceptIn) -> dict[str, Any]:
    """Save the figures as the instrument's factsheet profile, replacing an earlier factsheet profile."""
    if body.instrument_id is None or (inst := db.get(Instrument, body.instrument_id)) is None:
        raise HTTPException(422, "Choose the fund this factsheet belongs to")
    fs = body.factsheet if body.factsheet is not None else dict(x.data.get("factsheet") or {})
    try:
        data = factsheet.profile_data(fs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    as_of = body.as_of or extract.parse_date(fs.get("as_of"))
    profiles.save(db, inst.id, "factsheet", as_of, data)
    x.data = {**x.data, "factsheet": fs}
    return {"instrument_id": inst.id, "instrument": inst.name, "fields": sorted(data)}


def _create_account(db: Session, x: Extraction, spec: dict[str, str]) -> int:
    from app.modules.core.models import ACCOUNT_KINDS, Account, Entity, Person
    from app.modules.core.service import OwnerShare, set_owners

    kind = spec.get("kind") or "aot"
    if kind not in ACCOUNT_KINDS:
        raise HTTPException(422, f"Unknown account kind {kind}")
    ref = str((x.data.get("header") or {}).get("account") or "") or None
    a = Account(name=(spec.get("name") or f"Nordnet {ref or ''}").strip()[:128], kind=kind, external_ref=ref)
    db.add(a)
    db.flush()
    me = db.scalar(select(Person).where(Person.is_self.is_(True)).limit(1))
    ent = db.scalar(select(Entity).where(Entity.person_id == me.id)) if me else None
    if ent is not None:
        set_owners(db, a, [OwnerShare(entity_id=ent.id, share=Decimal(1))], date(1990, 1, 1))
    return a.id


@undo_handler("extraction.undo")
def _undo_extraction(db: Session, inverse: dict[str, Any]) -> None:
    holdings_apply.undo(db, int(inverse["id"]))


# ---- uploads ---------------------------------------------------------------------------------------------


def _start_reader(db: Session) -> None:
    from app.platform.models import JobRun

    busy = db.scalar(
        select(JobRun.id).where(JobRun.job == "ai.attachments", JobRun.status.in_(["queued", "running"])).limit(1)
    )
    if not busy:
        enqueue(db, "ai.attachments", trigger="manual")


@router.post("/attachments", status_code=201)
async def upload_attachments(
    user: CurrentUser,
    db: Db,
    files: list[UploadFile] = File(...),
    account_id: int | None = Form(None),
    kind: str | None = Form(None),
) -> list[dict[str, Any]]:
    """Queue documents for the local AI: PDFs, scans, screenshots, CSV or Excel (ING-09, AI-03)."""
    if kind is not None and kind not in statements.KINDS:
        raise HTTPException(422, f"Unknown document type {kind}")
    llm.config(db)  # fail now, not in the background, when no LLM is configured
    out = []
    for f in files[:20]:
        content = await f.read(attachments.MAX_BYTES + 1)
        name = (f.filename or "upload")[:255]
        try:
            attachments.kind_of(name, content)
            if len(content) > attachments.MAX_BYTES:
                raise attachments.AttachmentError("The file is larger than 25 MB")
        except attachments.AttachmentError as exc:
            out.append({"filename": name, "status": "error", "error": str(exc)})
            continue
        text_head = nordnet.decode(content)[:4000] if attachments.kind_of(name, content) == "text" else ""
        if nordnet.sniff(text_head) or crypto_history.sniff(text_head) in {"kraken", "binance", "ledgerlive"}:
            out.append(
                {
                    "filename": name,
                    "status": "error",
                    "error": "This is a transaction export: upload it under Imports for an exact ledger.",
                }
            )
            continue
        sha = hashlib.sha256(content).hexdigest()
        path = ai_jobs.file_path(sha, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        x = Extraction(
            source="upload",
            filename=name,
            file_sha256=sha,
            title=name,
            kind=kind or "unknown",
            status="queued",
            account_id=account_id,
            progress={"done": 0, "total": 0, "step": "queued"},
        )
        db.add(x)
        db.flush()
        audit(db, user.username, "extraction.upload", "extraction", x.id, after={"filename": name, "sha256": sha})
        out.append(_x_out(x))
    if any(o.get("status") == "queued" for o in out):
        _start_reader(db)
    return out


@router.post("/extractions/{xid}/retry")
def retry(xid: int, _: CurrentUser, db: Db, kind: str | None = None) -> dict[str, Any]:
    x = db.get(Extraction, xid)
    if x is None or x.source != "upload" or x.status not in ("error", "review", "rejected"):
        raise HTTPException(404, "Nothing to read again")
    if kind is not None and kind not in statements.KINDS:
        raise HTTPException(422, f"Unknown document type {kind}")
    x.kind, x.status, x.error, x.data, x.fields = kind or "unknown", "queued", None, {}, {}
    x.progress = {"done": 0, "total": 0, "step": "queued"}
    _start_reader(db)
    return _x_out(x)


@router.get("/extractions/{xid}/file")
def original(xid: int, _: CurrentUser, db: Db) -> FileResponse:
    x = db.get(Extraction, xid)
    if x is None or not x.file_sha256:
        raise HTTPException(404, "No file")
    path = ai_jobs.file_path(x.file_sha256, x.filename or "")
    if not path.exists():
        raise HTTPException(404, "The original file is no longer stored")
    return FileResponse(path, filename=x.filename or path.name)


@router.get("/gateway")
def gateway(_: CurrentUser, db: Db) -> dict[str, Any]:
    """How the LAN AI gateway sees this app (tier, limits) and whether it is up."""
    cfg = llm.config(db)
    root = cfg.base.removesuffix("/v1")
    c = OutboundClient(timeout=10)
    out: dict[str, Any] = {"url": root, "model": cfg.model, "vision_model": cfg.vision_model, "app": cfg.app}
    try:
        out["health"] = c.get(f"{root}/health").json()
        who = c.get(f"{root}/api/whoami", params={"app": cfg.app})
        if who.status_code == 200:
            out["policy"] = who.json().get("policy")
    except Exception as exc:  # noqa: BLE001 - informational only
        out["error"] = str(exc)
    return out


@router.post("/extractions/{xid}/reject")
def reject(xid: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    x = db.get(Extraction, xid)
    if x is None or x.status not in ("review", "error", "queued"):
        raise HTTPException(404, "Nothing to reject here")
    x.status, x.decided_at = "rejected", utcnow()
    audit(db, user.username, "extraction.reject", "extraction", x.id)
    return _x_out(x)
