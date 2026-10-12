"""Stress tests, costs, backtest (Phase 12); insurance and estate package (Phase 14); shared timeline (LIFE-02)."""

import html
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.ledger.models import Posting, Transaction
from app.modules.life import estate as package_doc
from app.modules.life import jobs as estate_jobs
from app.modules.life.models import EstateItem, Policy
from app.modules.networth.models import Valuation
from app.modules.networth.service import Sheet
from app.modules.planning import engine
from app.modules.portfolio import profiles
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing.service import PriceBook
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.models import Event
from app.platform.registry import TIMELINE_PROVIDERS, calendar_provider
from app.platform.security import encrypt_secret

router = APIRouter(prefix="/api", tags=["life"])
ZERO = Decimal(0)


class StressIn(BaseModel):
    params: dict[str, Any]


@router.post("/plan/stress")
def stress(body: StressIn, _: CurrentUser) -> dict[str, Any]:
    out = {}
    for name in (None, "crash_at_retirement", "lost_decade"):
        r = engine.simulate(engine.Params.from_dict({**body.params, "stress": name}), paths=1200)
        out[name or "base"] = {
            "success": r["success"],
            "p50_at_90": r["p50"][min(len(r["ages"]) - 1, max(0, 90 - r["ages"][0]))],
        }
    return out


@router.get("/portfolio/costs")
def costs(_: CurrentUser, db: Db, year: int | None = None) -> dict[str, Any]:
    """Trading fees booked in the ledger and estimated fund running costs from TER (PORT-07)."""
    y = year or today().year
    fees = (
        db.scalar(
            select(func.coalesce(func.sum(Posting.amount), 0))
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(Posting.role == "fee", Transaction.voided_at.is_(None), func.extract("year", Transaction.date) == y)
        )
        or ZERO
    )
    v = Valuer(db, Scope(None))
    insts = {i.id: i for i in db.scalars(select(Instrument))}
    eff = profiles.effective_many(db, insts.values())
    ter_rows, ter_total = [], ZERO
    for h in v.holdings(today()):
        ter = eff[h.instrument_id].data.get("ter")
        if ter:
            c = h.value * Decimal(str(ter))
            ter_total += c
            ter_rows.append(
                {
                    "instrument": insts[h.instrument_id].name,
                    "ter": float(ter),
                    "yearly_cost": str(c.quantize(Decimal("0.01"))),
                }
            )
    return {
        "year": y,
        "trading_fees": str(Decimal(fees).quantize(Decimal("0.01"))),
        "fund_costs_estimate": str(ter_total.quantize(Decimal("0.01"))),
        "funds": ter_rows,
        "note": "TER comes from the instrument profile (Morningstar, or set by hand on the Portfolio screen).",
    }


@router.get("/portfolio/backtest")
def backtest(
    _: CurrentUser, db: Db, instrument_id: int, start: date, monthly: Decimal = Decimal(100)
) -> dict[str, Any]:
    """Monthly investing into one instrument from `start` using stored prices (PORT-08)."""
    book = PriceBook(db, {instrument_id})
    if not book.prices.get(instrument_id):
        raise HTTPException(404, "No price history for that instrument")
    units, invested, d, rows = ZERO, ZERO, start, []
    while d <= today():
        unit, _pp, _fx = book.unit_value_eur(instrument_id, d)
        if unit > 0:
            units += monthly / unit
            invested += monthly
            rows.append({"date": d, "invested": str(invested), "value": str((units * unit).quantize(Decimal("0.01")))})
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=min(start.day, 28))
    return {"rows": rows, "invested": str(invested), "value": rows[-1]["value"] if rows else "0"}


class PolicyIn(BaseModel):
    kind: str
    insurer: str = Field(min_length=1)
    policy_number: str | None = None
    insured_entity_id: int | None = None
    coverage: Decimal | None = None
    premium_yearly: Decimal | None = None
    renewal_date: date | None = None
    beneficiaries: str | None = None
    notes: str | None = None


def _pol(p: Policy) -> dict[str, Any]:
    return {
        c: (str(getattr(p, c)) if isinstance(getattr(p, c), Decimal) else getattr(p, c))
        for c in (
            "id",
            "kind",
            "insurer",
            "policy_number",
            "insured_entity_id",
            "coverage",
            "premium_yearly",
            "renewal_date",
            "beneficiaries",
            "notes",
        )
    }


@router.get("/policies")
def policies(_: CurrentUser, db: Db) -> dict[str, Any]:
    rows = [_pol(p) for p in db.scalars(select(Policy).order_by(Policy.kind))]
    life = sum((Decimal(r["coverage"]) for r in rows if r["kind"] in ("life", "disability") and r["coverage"]), ZERO)
    debts = -sum((i.value for i in Sheet(db, Scope(None)).items(today()) if i.group == "liabilities"), ZERO)
    return {
        "policies": rows,
        "adequacy": {
            "life_and_disability_cover": str(life),
            "household_debt": str(debts.quantize(Decimal(1))),
            "covers_debt": life >= debts,
            "note": "A common rule of thumb: cover debts plus several years of family spending.",
        },
    }


@router.post("/policies", status_code=201)
def add_policy(body: PolicyIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    p = Policy(**body.model_dump())
    db.add(p)
    db.flush()
    audit(db, user.username, "policy.create", "policy", p.id)
    return _pol(p)


@router.delete("/policies/{pid}", status_code=204)
def del_policy(pid: int, _: CurrentUser, db: Db) -> None:
    p = db.get(Policy, pid)
    if p:
        db.delete(p)


class EstateIn(BaseModel):
    section: str = Field(pattern="^(contact|instruction|wish|location)$")
    title: str = Field(min_length=1, max_length=255)
    body: str


@router.get("/estate")
def estate(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {"id": e.id, "section": e.section, "title": e.title, "body": e.body}
        for e in db.scalars(select(EstateItem).order_by(EstateItem.section))
    ]


@router.post("/estate", status_code=201)
def add_estate(body: EstateIn, _: CurrentUser, db: Db) -> dict[str, int]:
    if any(w in body.body.lower() for w in ("password:", "salasana:", "pin:", "seed phrase")):
        raise HTTPException(422, "Do not store passwords, PINs or seed phrases here. Say where they are kept instead.")
    e = EstateItem(**body.model_dump())
    db.add(e)
    db.flush()
    return {"id": e.id}


@router.delete("/estate/{eid}", status_code=204)
def del_estate(eid: int, _: CurrentUser, db: Db) -> None:
    e = db.get(EstateItem, eid)
    if e:
        db.delete(e)


@router.get("/estate/package", response_class=HTMLResponse)
def package(user: CurrentUser, db: Db) -> HTMLResponse:
    """Printable 'if something happens to me' package (EST-01/02). Contains no credentials."""
    esc = html.escape
    pkg = package_doc.collect(db)

    def table(rows: list[dict[str, str]], cols: list[str], right: str | None = None) -> str:
        return "".join(
            "<tr>"
            + "".join(f"<td{" style='text-align:right'" if c == right else ''}>{esc(r[c])}</td>" for c in cols)
            + "</tr>"
            for r in rows
        )

    est = "".join(
        f"<h3>{esc(e['section'].title())}: {esc(e['title'])}</h3><p style='white-space:pre-wrap'>{esc(e['body'])}</p>"
        for e in pkg.items
    )
    acct = ["name", "type", "institution", "reference", "value"]
    audit(db, user.username, "estate.package", "system", None)
    doc = f"""<!doctype html><meta charset="utf-8"><title>Estate package</title>
<style>body{{font:14px system-ui;margin:2rem;max-width:60rem}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ccc;padding:4px 8px;text-align:left}}</style>
<h1>If something happens to me</h1><p>Generated {pkg.generated:%-d.%-m.%Y}. Values are estimates. This document contains no passwords; it says where things are.</p>
<h2>People</h2><p>{esc(", ".join(pkg.people))}</p>
<h2>Accounts and assets</h2><table><tr><th>Account</th><th>Type</th><th>Institution</th><th>Reference</th><th>Value</th></tr>{table(pkg.assets, acct, "value")}</table>
<h2>Debts</h2><table><tr><th>Account</th><th>Type</th><th>Institution</th><th>Reference</th><th>Value</th></tr>{table(pkg.debts, acct, "value")}</table>
<h2>Insurance</h2><table><tr><th>Type</th><th>Insurer</th><th>Policy</th><th>Insured</th><th>Beneficiaries</th></tr>{table(pkg.policies, ["type", "insurer", "policy", "insured", "beneficiaries"])}</table>
<h2>Contacts, instructions and wishes</h2>{est or "<p>None recorded yet.</p>"}"""
    return HTMLResponse(doc, headers={"Content-Disposition": 'attachment; filename="estate-package.html"'})


class PackagePdfIn(BaseModel):
    password: str = Field(min_length=10, max_length=128)


@router.post("/estate/package.pdf")
def package_pdf(body: PackagePdfIn, user: CurrentUser, db: Db) -> Response:
    """The same package as a PDF that needs the given password to open (AES-256). The password is used once for
    this file: it is not stored, and the audit entry does not contain it."""
    data = package_doc.render_pdf(package_doc.collect(db), body.password)
    audit(db, user.username, "estate.package_pdf", "system", None)
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="estate-package.pdf"', "Cache-Control": "no-store"},
    )


class PackageSettingsIn(BaseModel):
    enabled: bool = False
    path_dir: str | None = Field(default=None, max_length=500)
    keep: int = Field(default=3, ge=1, le=24)
    passphrase: str | None = Field(default=None, min_length=10, max_length=128)  # write-only; None keeps the stored one


def _package_settings(user: Any) -> dict[str, Any]:
    cfg = dict((user.settings or {}).get("estate_package") or {})
    return {
        "enabled": bool(cfg.get("enabled")),
        "path_dir": cfg.get("path_dir"),
        "effective_dir": str(estate_jobs.package_dir(cfg)),
        "keep": int(cfg.get("keep") or 3),
        "has_passphrase": bool(cfg.get("passphrase_enc")),
        "last": estate_jobs.last_written(cfg),
    }


@router.get("/estate/package-settings")
def get_package_settings(user: CurrentUser) -> dict[str, Any]:
    return _package_settings(user)


@router.put("/estate/package-settings")
def put_package_settings(body: PackageSettingsIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    cfg = dict((user.settings or {}).get("estate_package") or {})
    path_dir = (body.path_dir or "").strip() or None
    if path_dir and (not path_dir.startswith("/") or ".." in Path(path_dir).parts):
        raise HTTPException(422, "The folder must be an absolute path")
    if body.passphrase:
        cfg["passphrase_enc"] = encrypt_secret(body.passphrase)  # never stored as typed
    if body.enabled and not cfg.get("passphrase_enc"):
        raise HTTPException(422, "Set a passphrase before turning the scheduled package on")
    cfg.update(enabled=body.enabled, path_dir=path_dir, keep=body.keep)
    user.settings = {**(user.settings or {}), "estate_package": cfg}
    audit(db, user.username, "estate.package_settings", "system", None, after={"enabled": body.enabled})
    return _package_settings(user)


@router.get("/timeline")
def timeline(_: CurrentUser, db: Db, limit: int = 100) -> list[dict[str, Any]]:
    """Shared timeline across modules (LIFE-02): ledger, valuations, policies, generic events."""
    out: list[dict[str, Any]] = []
    for t in db.scalars(
        select(Transaction).where(Transaction.voided_at.is_(None)).order_by(Transaction.date.desc()).limit(limit)
    ):
        out.append({"date": t.date, "kind": f"ledger.{t.kind}", "title": t.description or t.kind, "ref": f"txn:{t.id}"})
    names = {a.id: a.name for a in db.scalars(select(Account))}
    for v in db.scalars(select(Valuation).order_by(Valuation.date.desc()).limit(limit)):
        out.append(
            {
                "date": v.date,
                "kind": "valuation",
                "title": f"{names.get(v.account_id, '?')}: {v.value:,.0f} €",
                "ref": None,
            }
        )
    for p in db.scalars(select(Policy).where(Policy.renewal_date.is_not(None))):
        out.append(
            {"date": p.renewal_date, "kind": "policy.renewal", "title": f"{p.insurer} {p.kind} renews", "ref": None}
        )
    for e in db.scalars(select(Event).order_by(Event.occurred_at.desc()).limit(limit)):
        out.append(
            {"date": e.occurred_at.date(), "kind": e.kind, "title": str(e.payload.get("title", e.kind)), "ref": None}
        )
    for provider in TIMELINE_PROVIDERS:
        out.extend(provider(db, limit))
    return sorted(out, key=lambda x: x["date"], reverse=True)[:limit]


@calendar_provider
def _calendar(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {
            "date": p.renewal_date,
            "title": f"{p.insurer} {p.kind} insurance renews",
            "source": "insurance",
            "category": "finance",
            "route": "/life",
        }
        for p in db.scalars(select(Policy).where(Policy.renewal_date >= start, Policy.renewal_date <= end))
    ]
