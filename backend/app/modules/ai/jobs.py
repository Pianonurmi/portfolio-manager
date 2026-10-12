"""Background reading of uploaded documents: the GPU queue can take minutes, so uploads never wait on a request."""

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai import attachments, extract, factsheet, nordnet_lots, statements
from app.modules.ai.models import Extraction
from app.platform.config import get_settings
from app.platform.db import session_factory
from app.platform.jobs import JobContext, job

TRANSACTIONS_HELP = (
    "This is a list of transactions. For an exact history use the broker's own export under Imports "
    "(Nordnet: Tapahtumat ja laskelmat → Vie CSV; Kraken, Binance and Ledger Live exports also work). "
    "A holdings or portfolio statement can be read here."
)


def file_path(sha: str, filename: str) -> Path:
    suffix = Path(filename).suffix.lower()[:8] or ".bin"
    return Path(get_settings().imports_dir) / "ai" / f"{sha}{suffix}"


def plain(v: Any) -> Any:
    """JSON-safe copy (Decimals as strings)."""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def process(db: Session, x: Extraction, client: Any = None) -> None:
    content = file_path(x.file_sha256 or "", x.filename or "").read_bytes()
    if attachments.kind_of(x.filename or "upload", content) == "text":
        text = attachments._decode(content)
        if nordnet_lots.sniff(text):  # exact export: no AI needed, and it has real purchase dates per lot
            header, rows = nordnet_lots.parse(text)
            statements.match_instruments(db, rows)
            x.kind, x.model = "holdings", "Nordnet lots export (parsed exactly)"
            x.data = plain({"header": header, "rows": rows, "checks": [], "notes": []})
            x.account_id = x.account_id or statements.suggest_account(db, header, rows)
            x.status, x.error = "review", None
            x.progress = {"done": 1, "total": 1, "step": "done"}
            return
    units = attachments.load(x.filename or "upload", content)
    x.progress = {"done": 0, "total": len(units), "step": "reading"}
    db.commit()
    hint = x.kind if x.kind in statements.KINDS else None
    header, model = statements.classify(db, units, client)
    x.model = model
    kind = hint or header["kind"]
    x.kind = kind
    x.progress = {"done": 0, "total": len(units), "step": statements.KINDS.get(kind, kind)}
    db.commit()

    def progress(done: int, total: int) -> None:
        x.progress = {"done": done, "total": total, "step": "holdings"}
        db.commit()

    if kind == "holdings":
        res = statements.holdings(db, units, header, client, progress)
        x.data = plain({"header": res.header, "rows": res.rows, "checks": res.checks, "notes": res.notes})
        x.model = res.model or model
        if not res.rows:
            x.status, x.error = "error", "No holdings were found. If this is a holdings statement, try a clearer copy."
            return
        x.account_id = x.account_id or statements.suggest_account(db, res.header, res.rows)
    elif kind == "factsheet":
        fs, x.model = factsheet.read(db, units, client)
        fs["instrument_id"] = factsheet.match_instrument(db, fs)
        x.data = plain({"header": header, "factsheet": fs, "options": factsheet.options()})
        if not factsheet.has_content(fs):
            x.status, x.error = "error", "No fund figures were recognised; choose the document type and try again"
            return
    elif kind in extract.FIELDS:
        x.fields, x.model = statements.fields_from_units(db, kind, units, client)
        on = statements.statement_date(header.get("as_of"))
        if on and "as_of" in extract.FIELDS[kind] and "as_of" not in x.fields:  # shown, so it can be corrected
            x.fields = {**x.fields, "as_of": {"value": on.isoformat(), "quote": "", "found": False, "type": "date"}}
        x.data = plain({"header": header})
        if not x.fields:
            x.status, x.error = "error", "No values recognised; choose the document type and try again"
            return
        x.account_id = x.account_id or extract.suggest_account(db, kind, x.fields)
    elif kind == "transactions":
        x.data = plain({"header": header})
        x.status, x.error = "error", TRANSACTIONS_HELP
        return
    else:
        x.data = plain({"header": header})
        x.status, x.error = "error", "This does not look like a statement, loan letter or pension record."
        return
    x.status, x.error = "review", None
    x.progress = {"done": len(units), "total": len(units), "step": "done"}


@job("ai.attachments", "Read uploaded documents with the local AI")
def read_attachments(ctx: JobContext, client: Any = None) -> None:
    n = 0
    sf = session_factory()
    while True:
        with sf() as db:
            x = db.scalar(
                select(Extraction)
                .where(Extraction.status == "queued")
                .order_by(Extraction.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if x is None:
                break
            x.status, x.error = "processing", None
            db.commit()
            try:
                process(db, x, client)
            except attachments.AttachmentError as exc:
                x.status, x.error = "error", str(exc)
            except HTTPException as exc:
                x.status, x.error = "error", f"Local AI: {exc.detail}"
                ctx.warn(f"{x.filename}: {exc.detail}")
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the queue
                db.rollback()
                x = db.get(Extraction, x.id)
                assert x is not None
                x.status, x.error = "error", f"Could not read the document: {type(exc).__name__}: {exc}"[:2000]
                ctx.warn(f"{x.filename}: {exc}")
            db.commit()
            ctx.log(f"{x.filename}: {x.kind} → {x.status}")
            n += 1
    ctx.summary = f"{n} documents read"
