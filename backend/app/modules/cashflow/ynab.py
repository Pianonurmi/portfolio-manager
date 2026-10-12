"""YNAB API client (https://api.ynab.com/v1) and sync. Read-only except the opt-in balance push (CF-02, D2)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetAccount, BudgetCategory, BudgetTransaction
from app.modules.networth.models import Valuation
from app.platform.connections import get_connection, secret
from app.platform.db import today, utcnow
from app.platform.jobs import JobContext
from app.platform.outbound import OutboundClient

BASE = "https://api.ynab.com/v1"
MILLI = Decimal(1000)


class YnabError(RuntimeError):
    pass


class Ynab:
    def __init__(self, token: str, client: Any | None = None) -> None:
        self.token = token
        self.client = client or OutboundClient()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = self.client.get(f"{BASE}{path}", params=params or None, headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code == 401:
            raise YnabError(
                "YNAB rejected the token (401). Create a new personal access token in YNAB → Account settings."
            )
        if r.status_code == 429:
            raise YnabError("YNAB rate limit reached (200 requests/hour). Try again later.")
        r.raise_for_status()
        data: dict[str, Any] = r.json()["data"]
        return data

    def budgets(self) -> list[dict[str, Any]]:
        return list(self._get("/budgets")["budgets"])

    def accounts(self, budget: str) -> list[dict[str, Any]]:
        return list(self._get(f"/budgets/{budget}/accounts")["accounts"])

    def categories(self, budget: str) -> list[dict[str, Any]]:
        return list(self._get(f"/budgets/{budget}/categories")["category_groups"])

    def transactions(self, budget: str, since: date | None, knowledge: int | None) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {}
        if knowledge:
            params["last_knowledge_of_server"] = knowledge
        elif since:
            params["since_date"] = since.isoformat()
        d = self._get(f"/budgets/{budget}/transactions", **params)
        return list(d["transactions"]), int(d.get("server_knowledge") or 0)

    def create_transaction(self, budget: str, txn: dict[str, Any]) -> None:
        r = self.client.request(
            "POST",
            f"{BASE}/budgets/{budget}/transactions",
            json={"transaction": txn},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if r.status_code >= 400:
            raise YnabError(f"YNAB refused the transaction ({r.status_code}): {r.text[:200]}")


def _m(v: int | None) -> Decimal:
    return Decimal(v or 0) / MILLI


def client_from_db(db: Session, client: Any | None = None) -> tuple[Ynab, str]:
    conn = get_connection(db, "ynab")
    if conn is None or not conn.secret_enc:
        raise YnabError("YNAB is not connected. Add a personal access token in Settings → Connections.")
    budget = (conn.config or {}).get("budget_id")
    if not budget:
        raise YnabError("Choose a YNAB budget in Settings → Connections.")
    return Ynab(secret(conn) or "", client), str(budget)


def sync(db: Session, ctx: JobContext | None = None, client: Any | None = None) -> dict[str, int]:
    api, budget = client_from_db(db, client)
    conn = get_connection(db, "ynab")
    assert conn is not None
    stats = {"accounts": 0, "categories": 0, "transactions": 0, "balances": 0}
    for a in api.accounts(budget):
        row = db.get(BudgetAccount, a["id"]) or BudgetAccount(id=a["id"])
        row.name, row.type, row.on_budget, row.closed = a["name"], a["type"], bool(a["on_budget"]), bool(a["closed"])
        row.balance = _m(a.get("balance"))
        db.add(row)
        stats["accounts"] += 1
    for g in api.categories(budget):
        for c in g.get("categories", []):
            cat = db.get(BudgetCategory, c["id"]) or BudgetCategory(id=c["id"])
            cat.group_name, cat.name = g["name"], c["name"]
            cat.hidden, cat.deleted = bool(c.get("hidden")), bool(c.get("deleted"))
            db.add(cat)
            stats["categories"] += 1
    knowledge = (conn.config or {}).get("server_knowledge")
    since = today() - timedelta(days=int((conn.config or {}).get("history_days", 1100)))
    txns, new_knowledge = api.transactions(budget, since, knowledge)
    for t in txns:
        subs = [s for s in t.get("subtransactions") or [] if not s.get("deleted")]
        _upsert_txn(db, t, None, deleted=bool(t.get("deleted")) or bool(subs))
        for s in subs:
            _upsert_txn(
                db,
                {
                    **s,
                    "date": t["date"],
                    "account_id": t["account_id"],
                    "payee_name": s.get("payee_name") or t.get("payee_name"),
                },
                t["id"],
                deleted=False,
            )
        stats["transactions"] += 1
    db.flush()
    stats["balances"] = pull_balances(db)
    conn.config = {**(conn.config or {}), "server_knowledge": new_knowledge}
    conn.status, conn.error, conn.last_sync = "ok", None, utcnow()
    if ctx:
        ctx.log(f"YNAB: {stats}")
    return stats


def _upsert_txn(db: Session, t: dict[str, Any], parent: str | None, deleted: bool) -> None:
    row = db.get(BudgetTransaction, t["id"]) or BudgetTransaction(id=t["id"])
    row.date = date.fromisoformat(t["date"])
    row.amount = _m(t.get("amount"))
    row.account_id = t["account_id"]
    row.category_id = t.get("category_id")
    row.payee = (t.get("payee_name") or None) and str(t["payee_name"])[:255]
    row.memo = t.get("memo")
    row.transfer_account_id = t.get("transfer_account_id")
    row.parent_id = parent
    row.deleted = deleted
    db.add(row)


DEBT_TYPES = {
    "creditCard",
    "lineOfCredit",
    "otherDebt",
    "mortgage",
    "autoLoan",
    "studentLoan",
    "personalLoan",
    "medicalDebt",
}


def pull_balances(db: Session) -> int:
    """Mapped budget accounts become dated valuations of our accounts (balances; debts as positive amounts)."""
    from app.modules.core.models import Account

    n = 0
    d = today()
    for ba in db.scalars(
        select(BudgetAccount).where(BudgetAccount.account_id.is_not(None), BudgetAccount.closed.is_(False))
    ):
        target = db.get(Account, ba.account_id)
        debt = ba.type in DEBT_TYPES or (target is not None and target.is_liability)
        value = -ba.balance if debt else ba.balance
        v = db.scalar(
            select(Valuation).where(
                Valuation.account_id == ba.account_id, Valuation.date == d, Valuation.method == "ynab"
            )
        )
        if v is None:
            db.add(
                Valuation(
                    account_id=ba.account_id,
                    date=d,
                    value=max(value, Decimal(0)),
                    method="ynab",
                    note=f"{'Actual' if ba.type == 'actual' else 'YNAB'} · {ba.name}",
                )
            )
        else:
            v.value = max(value, Decimal(0))
        n += 1
    return n


def push_values(
    db: Session, values: dict[int, Decimal], client: Any | None = None, ctx: JobContext | None = None
) -> int:
    """Post a balance adjustment to each opted-in YNAB tracking account so its balance equals our (rounded) value."""
    api, budget = client_from_db(db, client)
    n = 0
    for ba in db.scalars(
        select(BudgetAccount).where(BudgetAccount.push_account_id.is_not(None), BudgetAccount.closed.is_(False))
    ):
        if ba.on_budget:
            if ctx:
                ctx.warn(f"{ba.name} is an on-budget account; only tracking accounts can receive values")
            continue
        target = values.get(int(ba.push_account_id or 0))
        if target is None:
            continue
        step = Decimal(max(1, ba.push_precision))
        rounded = (target / step).quantize(Decimal(1)) * step
        delta = rounded - ba.balance
        if abs(delta) < step / 2:
            continue
        api.create_transaction(
            budget,
            {
                "account_id": ba.id,
                "date": today().isoformat(),
                "amount": int(delta * MILLI),
                "payee_name": "Portfolio Manager",
                "memo": f"Value update to {rounded} €",
                "cleared": "cleared",
                "approved": True,
            },
        )
        ba.balance = rounded
        n += 1
    return n
