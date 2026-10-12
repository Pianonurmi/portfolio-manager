"""Document extraction (ING-09, AI-03, DOC-03): Paperless OCR text → local LLM → staged values → human confirms.

The model must quote the text each value comes from. A value is marked `found` only when its quote occurs in the
document and the quote contains the value; anything else is shown as unverified. Nothing changes until accepted.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai import llm
from app.modules.core.models import Account, Person
from app.modules.documents.models import DocumentLink
from app.modules.networth.models import Valuation

MAX_CHARS = 12000

FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    # name: (type, description for the model)
    "statement": {
        "as_of": ("date", "the date the balance applies to"),
        "balance": ("money", "the account balance or value on that date, in euros"),
        "account_hint": ("text", "account name, number or IBAN as printed"),
        "institution": ("text", "bank or provider name"),
    },
    "loan_letter": {
        "as_of": ("date", "the letter's date or the date the balance applies to"),
        "outstanding_balance": ("money", "remaining loan principal in euros"),
        "reference_rate_name": ("text", "reference rate, e.g. 12 kk Euribor"),
        "reference_rate": ("percent", "the reference rate value in percent"),
        "margin": ("percent", "the loan margin in percent"),
        "next_reset": ("date", "next interest rate check (koronmuutospäivä)"),
        "monthly_payment": ("money", "the new monthly instalment in euros"),
        "end_date": ("date", "final payment date"),
        "account_hint": ("text", "loan number as printed"),
        "institution": ("text", "lender name"),
    },
    "pension_record": {
        "statement_year": ("year", "the year the record (työeläkeote) was printed"),
        "accrued_monthly": ("money", "pension accrued so far, euros per month (karttunut eläke)"),
        "annual_earnings": ("money", "earnings of the latest full year in euros"),
        "target_age": ("number", "the target retirement age (tavoite-eläkeikä) in years"),
    },
}

SYSTEM = (
    "You extract values from Finnish or English financial documents. Reply with JSON only. "
    'Format: {"kind": "statement" | "loan_letter" | "pension_record" | "unknown", "fields": {name: {"value": ..., '
    '"quote": "..."}}}. The quote must be copied exactly from the document text and contain the value. '
    "Use ISO dates (YYYY-MM-DD), numbers with a dot as decimal separator and no thousands separators, percentages as "
    "percent numbers (3.25 for 3,25 %). Leave out any field you cannot find. Never guess."
)


def _prompt(kind: str | None, text: str) -> str:
    kinds = [kind] if kind else list(FIELDS)
    spec = "\n".join(f"- {k}: " + ", ".join(f"{n} ({t}: {d})" for n, (t, d) in FIELDS[k].items()) for k in kinds)
    return f"Document kinds and their fields:\n{spec}\n\nDOCUMENT TEXT:\n{text[:MAX_CHARS]}"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip().lower()


def parse_number(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return Decimal(str(v))
    s = str(v).strip().replace(" ", " ").replace("€", "").replace("%", "").replace("EUR", "").strip()
    s = s.replace("−", "-").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_date(v: Any) -> date | None:
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    return None


def _numbers_in(quote: str) -> list[Decimal]:
    out = []
    for tok in re.findall(r"-?\d[\d\s., ]*\d|-?\d", quote):
        n = parse_number(tok)
        if n is not None:
            out.append(n)
    return out


def _value_in_quote(typ: str, value: Any, quote: str) -> bool:
    if typ in ("money", "percent", "number", "year"):
        n = parse_number(value)
        return n is not None and any(abs(x - n) < Decimal("0.005") for x in _numbers_in(quote))
    if typ == "date":
        d = parse_date(value)
        if d is None:
            return False
        q = _norm(quote)
        return any(
            s in q for s in (d.isoformat(), f"{d.day}.{d.month}.{d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}")
        )
    return _norm(str(value)) in _norm(quote)


def verify(kind: str, raw: dict[str, Any], text: str) -> dict[str, dict[str, Any]]:
    doc = _norm(text)
    out: dict[str, dict[str, Any]] = {}
    for name, (typ, _) in FIELDS.get(kind, {}).items():
        item = raw.get(name)
        if item is None:
            continue
        if not isinstance(item, dict):
            item = {"value": item, "quote": ""}
        value, quote = item.get("value"), str(item.get("quote") or "")
        if value in (None, ""):
            continue
        if typ == "date":
            d = parse_date(value)
            value = d.isoformat() if d else str(value)
        elif typ in ("money", "percent", "number", "year"):
            n = parse_number(value)
            value = str(n) if n is not None else str(value)
        found = bool(quote) and _norm(quote) in doc and _value_in_quote(typ, value, quote)
        out[name] = {"value": value, "quote": quote[:300], "found": found, "type": typ}
    return out


def extract(
    db: Session, text: str, kind: str | None = None, client: Any | None = None
) -> tuple[str, dict[str, dict[str, Any]], str]:
    content, model = llm.chat(
        db,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(kind, text)}],
        json_mode=True,
        client=client,
        temperature=0.0,
    )
    data = llm.parse_json(content)
    k = kind or str(data.get("kind") or "unknown")
    if k not in FIELDS:
        return "unknown", {}, model
    return k, verify(k, dict(data.get("fields") or {}), text), model


def suggest_account(db: Session, kind: str, fields: dict[str, dict[str, Any]]) -> int | None:
    """Pick the account whose name, reference or institution best matches what the document says."""
    if kind == "pension_record":
        return None
    hint = " ".join(str(fields.get(k, {}).get("value") or "") for k in ("account_hint", "institution")).lower()
    digits = re.sub(r"\D", "", hint)
    kinds = {"loan_letter": {"mortgage", "loan", "housing_loan"}}.get(kind)
    best, score = None, 0
    for a in db.scalars(select(Account).where(Account.closed_on.is_(None))):
        if kinds and a.kind not in kinds:
            continue
        s = 0
        ref = re.sub(r"\D", "", a.external_ref or "")
        if ref and len(ref) >= 4 and (ref in digits or digits.endswith(ref[-4:])):
            s += 5
        for word in re.findall(r"\w{3,}", f"{a.name} {a.institution.name if a.institution else ''}".lower()):
            if word in hint:
                s += 1
        if s > score:
            best, score = a.id, s
    return best


def _dec(fields: dict[str, dict[str, Any]], name: str) -> Decimal | None:
    f = fields.get(name)
    return parse_number(f["value"]) if f else None


def apply(db: Session, x: Any, user: Any, fields: dict[str, dict[str, Any]], account_id: int | None) -> dict[str, Any]:
    """Book the confirmed values. Returns what changed (for the record and for undo)."""
    applied: dict[str, Any] = {}
    ref = f"paperless:{x.paperless_id}" if x.paperless_id else f"upload:{x.id}"

    def link(subject_type: str, subject_id: int) -> None:
        if not x.paperless_id:  # uploaded file: kept by the app, not in Paperless
            return
        exists = db.scalar(
            select(DocumentLink.id).where(
                DocumentLink.subject_type == subject_type,
                DocumentLink.subject_id == subject_id,
                DocumentLink.paperless_id == x.paperless_id,
            )
        )
        if not exists:
            db.add(
                DocumentLink(
                    subject_type=subject_type, subject_id=subject_id, paperless_id=x.paperless_id, title=x.title[:255]
                )
            )

    def valuation(value: Decimal, on: date) -> None:
        v = Valuation(
            account_id=account_id,
            date=on,
            value=value,
            method="statement",
            note=f"From {x.title}"[:500],
            document_ref=ref,
        )
        db.add(v)
        db.flush()
        applied["valuation_id"] = v.id

    if x.kind == "statement":
        bal, on = _dec(fields, "balance"), parse_date((fields.get("as_of") or {}).get("value"))
        on = on or parse_date(((x.data or {}).get("header") or {}).get("as_of"))
        if account_id is None or bal is None or on is None:
            raise ValueError("A statement needs an account, a balance and a date")
        valuation(abs(bal) if (a := db.get(Account, account_id)) is not None and a.is_liability else bal, on)
        link("account", account_id)
    elif x.kind == "loan_letter":
        if account_id is None:
            raise ValueError("Choose the loan account")
        a = db.get(Account, account_id)
        assert a is not None
        meta = dict(a.meta or {})
        for name, key in (("reference_rate", "reference_rate"), ("margin", "margin")):
            v = _dec(fields, name)
            if v is not None:
                meta[key] = str((v / 100).quantize(Decimal("0.000001")))
        for name in ("next_reset", "end_date"):
            d = parse_date((fields.get(name) or {}).get("value"))
            if d:
                meta[name] = d.isoformat()
        applied["meta_before"] = {k: (a.meta or {}).get(k) for k in meta if (a.meta or {}).get(k) != meta[k]}
        a.meta = meta
        bal, on = _dec(fields, "outstanding_balance"), parse_date((fields.get("as_of") or {}).get("value"))
        if bal is not None and on is not None:
            valuation(abs(bal), on)
        link("account", account_id)
    elif x.kind == "pension_record":
        pen = dict((user.settings or {}).get("pension") or {})
        applied["pension_before"] = dict(pen)
        for name in ("accrued_monthly", "annual_earnings"):
            v = _dec(fields, name)
            if v is not None:
                pen[name] = float(v)
        for name in ("statement_year", "target_age"):
            v = _dec(fields, name)
            if v is not None:
                pen[name] = int(v)
        user.settings = {**(user.settings or {}), "pension": pen}
        me = db.scalar(select(Person).where(Person.is_self.is_(True)).limit(1))
        if me:
            link("person", me.id)
    else:
        raise ValueError("Nothing to apply for an unknown document type")
    return applied
