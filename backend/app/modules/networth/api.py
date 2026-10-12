"""Net worth, valuations, loans and the cross-module attention list."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.core.models import ACCOUNT_KINDS, Account, Instrument, Ownership
from app.modules.ingest.models import ImportBatch, ReconciliationCheck
from app.modules.networth.loans import loan_terms, schedule
from app.modules.networth.models import METHODS, Valuation
from app.modules.networth.service import GROUP_LABEL, GROUPS, Sheet
from app.modules.portfolio.performance import period_start
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing.service import missing_splits, stale_text
from app.platform.api import make_undo
from app.platform.audit import audit
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.explain import ExplainNode, Figure, explainer
from app.platform.models import AppUser
from app.platform.registry import ATTENTION_PROVIDERS, calendar_provider, undo_handler

router = APIRouter(prefix="/api", tags=["networth"])
ZERO = Decimal(0)
CENT = Decimal("0.01")


def q(v: Decimal) -> Decimal:
    return v.quantize(CENT)


def fmt(v: Decimal) -> str:
    return ("−" if v < 0 else "") + f"{abs(v):,.2f}".replace(",", " ").replace(".", ",") + " €"


def _scope(s: str | None) -> Scope:
    try:
        return Scope.parse(s)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _spending(db: Session) -> Decimal | None:
    from app.modules.cashflow.service import average_spending

    ynab_avg = average_spending(db)
    if ynab_avg is not None:
        return ynab_avg.quantize(CENT)
    u = db.scalar(select(AppUser).limit(1))
    v = (u.settings or {}).get("monthly_spending") if u else None
    return Decimal(str(v)) if v else None


@router.get("/networth")
def networth(_: CurrentUser, db: Db, scope: str | None = None, on: date | None = None) -> dict[str, Any]:
    sc, d = _scope(scope), on or today()
    sheet = Sheet(db, sc)
    items = sheet.items(d)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for it in items:
        totals[it.group] += it.value
        groups[it.group].append(
            {
                "account_id": it.account.id,
                "name": it.account.name,
                "kind": it.account.kind,
                "kind_label": ACCOUNT_KINDS.get(it.account.kind, it.account.kind),
                "share": str(it.share),
                "value": Figure(
                    value=q(it.value),
                    ref=f"acct:{it.account.id}:{sc.token}:{d.isoformat()}",
                    estimated=it.estimated,
                    stale=("no valuation" if it.method == "none" else None),
                ),
                "gross": str(q(it.gross)),
                "method": it.method,
                "as_of": it.as_of,
            }
        )
    assets = sum((v for k, v in totals.items() if k != "liabilities"), ZERO)
    liab = -totals["liabilities"]
    liquid = totals["investments"] + totals["cash"]
    spend = _spending(db)
    out_groups = []
    for g in GROUPS:
        if groups.get(g):
            rows = sorted(groups[g], key=lambda r: -abs(float(r["value"].value)))
            out_groups.append(
                {
                    "id": g,
                    "label": GROUP_LABEL[g],
                    "total": Figure(value=q(totals[g]), ref=f"nwg:{sc.token}:{g}:{d.isoformat()}"),
                    "items": rows,
                }
            )
    return {
        "as_of": d,
        "scope": sc.token,
        "net_worth": Figure(value=q(assets - liab), ref=f"nw:{sc.token}:{d.isoformat()}"),
        "assets": Figure(value=q(assets), ref=f"nw:{sc.token}:{d.isoformat()}"),
        "liabilities": Figure(value=q(liab), ref=f"nwg:{sc.token}:liabilities:{d.isoformat()}"),
        "liquid": Figure(value=q(liquid), ref=None),
        "illiquid": Figure(value=q(assets - liquid), ref=None),
        "groups": out_groups,
        "monthly_spending": None if spend is None else str(spend),
        "emergency_months": float(totals["cash"] / spend) if spend else None,
        "liquid_months": float(liquid / spend) if spend else None,
        "home_equity": Figure(
            value=q(
                totals["property"]
                - sum((-i.value for i in items if i.account.kind in ("mortgage", "housing_loan")), ZERO)
            ),
            ref=None,
            estimated=any(i.estimated for i in items if i.group == "property"),
        ),
    }


@router.get("/networth/history")
def history(_: CurrentUser, db: Db, scope: str | None = None, period: str = "1Y") -> dict[str, Any]:
    sc = _scope(scope)
    sheet = Sheet(db, sc)
    end = today()
    firsts = [d for d in [sheet.valuer.first_date] + [vs[0].date for vs in sheet.vals.values() if vs] if d]
    if not firsts:
        return {"series": []}
    first = min(firsts)
    start = period_start(period, end, first)
    if start <= first:
        start = first - timedelta(days=1)
    days = (end - start).days
    step = 1 if days <= 62 else 7 if days <= 800 else 14
    rows = sheet.series(start, end, step)
    return {"series": [{k: (str(q(v)) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows]}


@router.get("/networth/change")
def change(_: CurrentUser, db: Db, scope: str | None = None, period: str = "YTD") -> dict[str, Any]:
    sc = _scope(scope)
    sheet = Sheet(db, sc)
    end = today()
    first = sheet.valuer.first_date or min((vs[0].date for vs in sheet.vals.values() if vs), default=end) or end
    start = period_start(period, end, first)
    if start <= first:
        start = first - timedelta(days=1)  # include the very first deposit as a contribution
    c = sheet.change(start, end)
    return {"start": start, "end": end, **{k: Figure(value=q(v), ref=f"nwc:{sc.token}:{period}") for k, v in c.items()}}


class SettingsIn(BaseModel):
    monthly_spending: Decimal | None = Field(default=None, ge=0)


@router.put("/networth/settings")
def nw_settings(body: SettingsIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    user.settings = {
        **(user.settings or {}),
        "monthly_spending": None if body.monthly_spending is None else str(body.monthly_spending),
    }
    return {"monthly_spending": user.settings["monthly_spending"]}


# ---- valuations ---------------------------------------------------------------------------------


class ValuationIn(BaseModel):
    account_id: int
    date: date
    value: Decimal = Field(ge=0)
    method: str = "manual"
    note: str | None = None
    document_ref: str | None = None


def val_out(v: Valuation) -> dict[str, Any]:
    return {
        "id": v.id,
        "account_id": v.account_id,
        "date": v.date,
        "value": str(v.value),
        "method": v.method,
        "note": v.note,
        "document_ref": v.document_ref,
    }


@router.get("/valuations")
def list_valuations(_: CurrentUser, db: Db, account_id: int) -> list[dict[str, Any]]:
    return [
        val_out(v)
        for v in db.scalars(select(Valuation).where(Valuation.account_id == account_id).order_by(Valuation.date.desc()))
    ]


@router.post("/valuations", status_code=201)
def add_valuation(body: ValuationIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if body.method not in METHODS:
        raise HTTPException(422, f"Unknown method {body.method}")
    if db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    v = Valuation(**body.model_dump())
    db.add(v)
    db.flush()
    audit(db, user.username, "valuation.create", "valuation", v.id, after=body.model_dump(mode="json"))
    return {**val_out(v), "undo_id": make_undo(db, "valuation.delete", "Add valuation", {"id": v.id})}


@router.delete("/valuations/{val_id}")
def delete_valuation(val_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    v = db.get(Valuation, val_id)
    if v is None:
        raise HTTPException(404, "Valuation not found")
    snap = val_out(v)
    db.delete(v)
    audit(db, user.username, "valuation.delete", "valuation", val_id, before={k: str(x) for k, x in snap.items()})
    return {
        "undo_id": make_undo(
            db, "valuation.restore", "Delete valuation", {k: str(x) if x is not None else None for k, x in snap.items()}
        )
    }


@undo_handler("valuation.delete")
def _undo_add(db: Session, inv: dict[str, Any]) -> None:
    v = db.get(Valuation, int(inv["id"]))
    if v:
        db.delete(v)


@undo_handler("valuation.restore")
def _undo_delete(db: Session, inv: dict[str, Any]) -> None:
    db.add(
        Valuation(
            account_id=int(inv["account_id"]),
            date=date.fromisoformat(inv["date"]),
            value=Decimal(inv["value"]),
            method=inv["method"],
            note=inv.get("note"),
            document_ref=inv.get("document_ref"),
        )
    )


# ---- account details (property / loan terms) -------------------------------------------------------


class AccountMetaIn(BaseModel):
    meta: dict[str, Any]


@router.put("/accounts/{account_id}/meta")
def set_meta(account_id: int, body: AccountMetaIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    before = dict(a.meta or {})
    a.meta = {**before, **body.meta}
    audit(db, user.username, "account.meta", "account", a.id, before=before, after=a.meta)
    return a.meta


@router.get("/accounts/{account_id}/meta")
def get_meta(account_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    return a.meta or {}


@router.get("/loans/{account_id}/schedule")
def loan_schedule(account_id: int, _: CurrentUser, db: Db) -> dict[str, Any]:
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    last = db.scalar(select(Valuation).where(Valuation.account_id == a.id).order_by(Valuation.date.desc()).limit(1))
    rate, kind, end, fixed = loan_terms(a.meta or {})
    if last is None or end is None:
        return {
            "ready": False,
            "missing": [
                x for x, ok in (("balance statement", last is not None), ("end date", end is not None)) if not ok
            ],
        }
    rows = schedule(last.value, rate, last.date, end, kind, fixed)
    t = today()
    nxt = next((r for r in rows if r.date >= t), None)
    interest_12m = sum((r.interest for r in rows if t <= r.date < t + timedelta(days=365)), ZERO)
    reset = (a.meta or {}).get("next_reset")
    return {
        "ready": True,
        "rate": str(rate),
        "kind": kind,
        "end_date": end,
        "from_balance": str(last.value),
        "from_date": last.date,
        "next_payment": None
        if nxt is None
        else {
            "date": nxt.date,
            "payment": str(nxt.payment),
            "interest": str(nxt.interest),
            "principal": str(nxt.principal),
        },
        "interest_next_12m": str(interest_12m),
        "next_reset": reset,
        "rows": [
            {
                "date": r.date,
                "payment": str(r.payment),
                "interest": str(r.interest),
                "principal": str(r.principal),
                "balance": str(r.balance),
            }
            for r in rows[:480]
        ],
    }


# ---- attention (Home "needs attention") ---------------------------------------------------------------


@router.get("/attention")
def attention(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = today()
    for b in db.scalars(
        select(ImportBatch).where(ImportBatch.status == "staged").order_by(ImportBatch.created_at.desc())
    ):
        c = b.stats.get("counts", {})
        pending = c.get("new", 0) + c.get("needs_mapping", 0) + c.get("needs_review", 0)
        if pending:
            out.append(
                {
                    "severity": "info",
                    "title": f"Import #{b.id} awaiting review",
                    "detail": f"{b.filename} · {pending} rows",
                    "route": f"/imports/{b.id}",
                    "chip": "review",
                }
            )
    bad = db.execute(
        select(ReconciliationCheck, Account.name)
        .join(Account, Account.id == ReconciliationCheck.account_id)
        .where(ReconciliationCheck.ok.is_(False))
    ).all()
    for chk, name in bad[:5]:
        out.append(
            {
                "severity": "bad",
                "title": f"Reconciliation differs: {name}",
                "detail": f"{chk.subject}: ledger {chk.computed}, source {chk.reported}",
                "route": "/imports",
                "chip": "differs",
            }
        )
    v = Valuer(db, Scope(None))
    seen: set[int] = set()
    for h in v.holdings(t):
        if h.instrument_id in v.book.cash_ccy or h.instrument_id in seen:
            continue
        seen.add(h.instrument_id)
        st = stale_text(h.price, t)
        if st:
            inst = db.get(Instrument, h.instrument_id)
            out.append(
                {
                    "severity": "warn",
                    "title": f"Stale price: {inst.name if inst else '?'}",
                    "detail": f"Using {st}",
                    "ref": f"price:{h.instrument_id}:{t.isoformat()}",
                    "chip": st,
                }
            )
    for m in missing_splits(db):
        inst = db.get(Instrument, int(m["instrument_id"]))
        out.append(
            {
                "severity": "bad",
                "title": f"Split missing from the ledger: {inst.name if inst else '?'}",
                "detail": f"{format(m['ratio'].normalize(), 'f')}:1 on {m['date']:%-d.%-m.%Y} "
                "reported by the price source. "
                "Book it to fix valuations.",
                "action": {"label": "Book split", "path": f"/api/corporate-actions/{m['id']}/book"},
                "chip": "split",
            }
        )
    for a in db.scalars(select(Account).where(Account.kind.in_(["mortgage", "loan"]))):
        reset = (a.meta or {}).get("next_reset")
        if reset:
            days = (date.fromisoformat(reset) - t).days
            if 0 <= days <= 45:
                out.append(
                    {
                        "severity": "info",
                        "title": f"Rate reset in {days} days: {a.name}",
                        "detail": f"Reset {date.fromisoformat(reset):%-d.%-m.%Y}",
                        "route": "/net-worth",
                        "chip": f"{date.fromisoformat(reset):%-d.%-m.}",
                    }
                )
    for a in db.scalars(select(Account).where(Account.kind == "property")):
        last = db.scalar(select(func.max(Valuation.date)).where(Valuation.account_id == a.id))
        if last is None or (t - last).days > 365:
            out.append(
                {
                    "severity": "warn",
                    "title": f"Update the value of {a.name}",
                    "detail": "No valuation in the last 12 months",
                    "route": "/net-worth",
                    "chip": "valuation",
                }
            )
    owned = {o for (o,) in db.execute(select(Ownership.account_id).distinct())}
    for a in db.scalars(select(Account).where(Account.closed_on.is_(None))):
        if a.id not in owned:
            out.append(
                {
                    "severity": "warn",
                    "title": f"No owner set: {a.name}",
                    "detail": "It is left out of personal views",
                    "route": "/settings/accounts",
                    "chip": "owner",
                }
            )
    for provider in ATTENTION_PROVIDERS:  # other modules: due dates, renewals, price changes
        out.extend(provider(db))
    return out


# ---- provenance -----------------------------------------------------------------------------------


@explainer("nw")
def explain_nw(db: Session, scope: str, on: str) -> ExplainNode:
    sc, d = Scope.parse(scope), date.fromisoformat(on)
    items = Sheet(db, sc).items(d)
    by: dict[str, Decimal] = defaultdict(Decimal)
    for it in items:
        by[it.group] += it.value
    assets = sum((v for k, v in by.items() if k != "liabilities"), ZERO)
    kids = [
        ExplainNode(label=GROUP_LABEL[g], value=fmt(by[g]), ref=f"nwg:{sc.token}:{g}:{on}", source="derived")
        for g in GROUPS
        if g in by
    ]
    return ExplainNode(
        label="Net worth",
        value=fmt(assets + by.get("liabilities", ZERO)),
        formula="assets − liabilities",
        children=kids,
    )


@explainer("nwg")
def explain_group(db: Session, scope: str, group: str, on: str) -> ExplainNode:
    sc, d = Scope.parse(scope), date.fromisoformat(on)
    items = [i for i in Sheet(db, sc).items(d) if i.group == group]
    kids = [
        ExplainNode(
            label=i.account.name, value=fmt(i.value), ref=f"acct:{i.account.id}:{sc.token}:{on}", source="derived"
        )
        for i in sorted(items, key=lambda i: -abs(i.value))
    ]
    return ExplainNode(
        label=GROUP_LABEL.get(group, group),
        value=fmt(sum((i.value for i in items), ZERO)),
        formula="Σ accounts",
        children=kids,
    )


@explainer("acct")
def explain_account(db: Session, account_id: str, scope: str | None = None, on: str | None = None) -> ExplainNode:
    sc = Scope.parse(scope or "household")
    d = date.fromisoformat(on) if on else today()
    sheet = Sheet(db, sc)
    it = next((i for i in sheet.items(d) if i.account.id == int(account_id)), None)
    a = db.get(Account, int(account_id))
    if a is None:
        raise LookupError("account not found")
    if it is None:
        return ExplainNode(label=a.name, value="—", note="Not part of this view on that date")
    if it.method == "ledger":
        return ExplainNode(
            label=a.name,
            value=fmt(it.value),
            formula="Σ holdings from the ledger",
            children=[
                ExplainNode(label="Holdings", ref=f"acctval:{a.id}:{sc.token}:{d.isoformat()}", source="derived")
            ],
        )
    kids = [
        ExplainNode(
            label=f"Valuation {v.date:%-d.%-m.%Y}",
            value=fmt(v.value),
            source="manual",
            note=" · ".join(x for x in (v.method, v.note, v.document_ref) if x),
        )
        for v in reversed(sheet.vals.get(a.id, [])[-12:])
    ]
    formula = (
        "latest valuation" if it.method != "estimate" else "last statement balance minus scheduled repayments since"
    )
    if it.share != 1:
        kids.insert(
            0,
            ExplainNode(
                label="Ownership share", value=f"{it.share * 100:.0f} %", ref=f"owners:{a.id}", source="manual"
            ),
        )
        formula += " × ownership share"
    if it.method == "estimate":
        rate, kind, end, _ = loan_terms(a.meta or {})
        kids.insert(
            0,
            ExplainNode(
                label="Estimated balance (100 %)",
                value=fmt(it.gross),
                source="derived",
                note=f"{kind} schedule at {rate * 100:.2f} % until {end:%-m/%Y}" if end else None,
            ),
        )
    return ExplainNode(label=a.name, value=fmt(it.value), formula=formula, children=kids)


@explainer("nwc")
def explain_change(db: Session, scope: str, period: str) -> ExplainNode:
    sc = Scope.parse(scope)
    sheet = Sheet(db, sc)
    end = today()
    first = sheet.valuer.first_date or end
    start = period_start(period, end, first)
    if start <= first:
        start = first - timedelta(days=1)
    c = sheet.change(start, end)
    labels = {
        "contributions": "Money you added to investments",
        "market": "Market returns, dividends and costs",
        "debt_paydown": "Debt paid down",
        "revaluation": "Revaluation of property and other assets",
    }
    return ExplainNode(
        label=f"Net worth change · {period}",
        value=fmt(c["total"]),
        formula=f"{start:%-d.%-m.%Y} → {end:%-d.%-m.%Y}",
        children=[ExplainNode(label=labels[k], value=fmt(c[k]), source="derived") for k in labels],
    )


class LoanScenarioIn(BaseModel):
    rate_delta: Decimal = Decimal(0)  # e.g. 0.01 = +1 percentage point
    extra_monthly: Decimal = Decimal(0)


@router.post("/loans/{account_id}/simulate")
def simulate_loan(account_id: int, body: LoanScenarioIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Rate shock and extra repayments versus the current schedule (RE-06)."""
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(404, "Account not found")
    last = db.scalar(select(Valuation).where(Valuation.account_id == a.id).order_by(Valuation.date.desc()).limit(1))
    rate, kind, end, fixed = loan_terms(a.meta or {})
    if last is None or end is None:
        raise HTTPException(422, "Add a balance statement and the final payment date first")

    def run(r: Decimal, extra: Decimal) -> dict[str, Any]:
        rows = schedule(last.value, r, last.date, end, kind, fixed)
        if extra > 0:  # extra repayment shortens the term at the same instalment
            bal, out, i = last.value, [], 0
            pay = rows[0].payment if rows else Decimal(0)
            from app.modules.networth.loans import Payment, add_months

            while bal > 0 and i < 600:
                i += 1
                interest = (bal * r / 12).quantize(CENT)
                principal = min(bal, pay - interest + extra)
                bal -= principal
                out.append(Payment(add_months(last.date, i), interest + principal, interest, principal, bal))
            rows = out
        return {
            "months": len(rows),
            "paid_off": rows[-1].date if rows else None,
            "total_interest": str(sum((p.interest for p in rows), ZERO).quantize(CENT)),
            "first_payment": str(rows[0].payment) if rows else None,
        }

    base = run(rate, Decimal(0))
    scen = run(rate + body.rate_delta, body.extra_monthly)
    return {
        "rate": str(rate),
        "base": base,
        "scenario": scen,
        "interest_saved": str((Decimal(base["total_interest"]) - Decimal(scen["total_interest"])).quantize(CENT)),
    }


class IndexEstimateIn(BaseModel):
    account_id: int
    base_date: date
    base_value: Decimal
    index_base: Decimal = Field(gt=0)
    index_now: Decimal = Field(gt=0)
    on: date | None = None


@router.post("/valuations/index-estimate", status_code=201)
def index_estimate(body: IndexEstimateIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Revalue a property with a price index (e.g. Statistics Finland, old dwellings by postal code) (RE-03)."""
    if db.get(Account, body.account_id) is None:
        raise HTTPException(404, "Account not found")
    value = (body.base_value * body.index_now / body.index_base).quantize(Decimal(1))
    v = Valuation(
        account_id=body.account_id,
        date=body.on or today(),
        value=value,
        method="index",
        note=f"index {body.index_now} / {body.index_base} × {body.base_value} ({body.base_date:%-d.%-m.%Y})",
    )
    db.add(v)
    db.flush()
    audit(db, user.username, "valuation.index", "valuation", v.id, after=body.model_dump(mode="json"))
    return {**val_out(v), "undo_id": make_undo(db, "valuation.delete", "Index estimate", {"id": v.id})}


@calendar_provider
def _loan_dates(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Rate reset (koronmuutospäivä) and final payment dates written by the loan-letter extraction."""
    out = []
    for a in db.scalars(select(Account).where(Account.kind.in_(["mortgage", "loan", "housing_loan"]))):
        for key, label in (("next_reset", "Rate reset"), ("end_date", "Final payment")):
            raw = (a.meta or {}).get(key)
            try:
                d = date.fromisoformat(raw) if raw else None
            except ValueError:
                d = None
            if d and start <= d <= end:
                out.append(
                    {
                        "date": d,
                        "title": f"{label}: {a.name}",
                        "source": "loans",
                        "category": "finance",
                        "route": "/networth",
                    }
                )
    return out


from app.modules.networth.property import router as property_router  # noqa: E402

extra_routers = [property_router]
