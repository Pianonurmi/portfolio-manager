"""Cash flow endpoints: YNAB connection, account mapping, sync, spending analytics."""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.modules.cashflow import actual, backend, ynab
from app.modules.cashflow.models import BudgetAccount, BudgetCategory
from app.modules.cashflow.service import monthly
from app.modules.core.models import Account
from app.platform.auth import CurrentUser, Db
from app.platform.connections import get_connection, secret
from app.platform.explain import Figure
from app.platform.jobs import enqueue

router = APIRouter(prefix="/api/cashflow", tags=["cashflow"])
CENT = Decimal("0.01")


class ActualIn(BaseModel):
    url: str = Field(min_length=8)
    sync_id: str = Field(min_length=8)
    api_key: str = Field(min_length=4)


class TokenIn(BaseModel):
    token: str = Field(min_length=10)


@router.post("/ynab/budgets")
def ynab_budgets(body: TokenIn, _: CurrentUser) -> list[dict[str, Any]]:
    """List budgets for a token before saving it (connection test)."""
    try:
        return [
            {"id": b["id"], "name": b["name"], "last_modified_on": b.get("last_modified_on")}
            for b in ynab.Ynab(body.token).budgets()
        ]
    except ynab.YnabError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/ynab/budgets")
def ynab_budgets_saved(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    conn = get_connection(db, "ynab")
    if conn is None or not conn.secret_enc:
        raise HTTPException(404, "YNAB is not connected")
    try:
        return [{"id": b["id"], "name": b["name"]} for b in ynab.Ynab(secret(conn) or "").budgets()]
    except ynab.YnabError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/backend")
def which_backend(_: CurrentUser, db: Db) -> dict[str, str | None]:
    kind = backend.active(db)
    return {"kind": kind, "name": backend.NAMES.get(kind or "")}


@router.post("/actual/test")
def actual_test(body: ActualIn, _: CurrentUser) -> dict[str, Any]:
    """Check the actual-http-api URL, sync id and key before saving them."""
    from urllib.parse import urlsplit

    from app.platform.config import get_settings
    from app.platform.outbound import host_allowed

    host = urlsplit(body.url).hostname or ""
    if not host_allowed(host, get_settings().lan_allowlist):
        raise HTTPException(422, f"{host} must be on PM_LAN_ALLOWLIST (Actual runs on your LAN)")
    try:
        accts = actual.Actual(body.url, body.sync_id, body.api_key).accounts()
    except actual.ActualError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach actual-http-api: {exc}") from exc
    return {"accounts": len(accts), "names": [a["name"] for a in accts][:10]}


@router.post("/ynab/sync", status_code=202)
def ynab_sync(_: CurrentUser, db: Db) -> dict[str, int]:
    return {"run_id": enqueue(db, "ynab.sync").id}


@router.get("/ynab/accounts")
def ynab_accounts(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "on_budget": a.on_budget,
            "closed": a.closed,
            "balance": str(a.balance),
            "account_id": a.account_id,
            "push_account_id": a.push_account_id,
            "push_precision": a.push_precision,
        }
        for a in db.scalars(
            select(BudgetAccount).order_by(BudgetAccount.closed, BudgetAccount.on_budget.desc(), BudgetAccount.name)
        )
    ]


class MappingIn(BaseModel):
    account_id: int | None = None
    push_account_id: int | None = None
    push_precision: int = Field(default=100, ge=1, le=100000)


@router.put("/ynab/accounts/{ynab_id}")
def map_ynab_account(ynab_id: str, body: MappingIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    ba = db.get(BudgetAccount, ynab_id)
    if ba is None:
        raise HTTPException(404, "YNAB account not found; sync first")
    for aid in (body.account_id, body.push_account_id):
        if aid is not None and db.get(Account, aid) is None:
            raise HTTPException(404, "Account not found")
    if body.push_account_id is not None and ba.on_budget:
        raise HTTPException(422, "Values can only be pushed to tracking (off-budget) accounts")
    ba.account_id, ba.push_account_id, ba.push_precision = body.account_id, body.push_account_id, body.push_precision
    ynab.pull_balances(db)
    return {"id": ba.id}


@router.get("/categories")
def categories(_: CurrentUser, db: Db) -> list[dict[str, str]]:
    return [
        {"id": c.id, "group": c.group_name, "name": c.name}
        for c in db.scalars(
            select(BudgetCategory)
            .where(BudgetCategory.deleted.is_(False))
            .order_by(BudgetCategory.group_name, BudgetCategory.name)
        )
    ]


@router.get("/summary")
def summary(_: CurrentUser, db: Db, months: int = 12) -> dict[str, Any]:
    m = monthly(db, max(2, min(months, 60)) + 1)

    def q(v: Decimal) -> str:
        return str(Decimal(v).quantize(CENT))

    return {
        "months": [
            {
                "month": r["month"],
                "income": q(r["income"]),
                "spending": q(r["spending"]),
                "saved": q(r["saved"]),
                "rate": r["rate"],
            }
            for r in m["months"]
        ],
        "avg_income": Figure(value=Decimal(m["avg_income"]).quantize(CENT), ref=None),
        "avg_spending": Figure(value=Decimal(m["avg_spending"]).quantize(CENT), ref=None),
        "savings_rate": m["savings_rate"],
        "categories": [
            {
                "category": c["category"],
                "total": q(c["total"]),
                "monthly_avg": q(c["monthly_avg"]),
                "by_month": {k: q(v) for k, v in c["by_month"].items()},
            }
            for c in m["categories"][:40]
        ],
        "connected": backend.active(db) is not None,
        "backend": backend.active(db),
    }
