"""Actual Budget as the budget backend (CF-04), through actual-http-api (github.com/jhonderson/actual-http-api).

Actual itself only has a Node library; the HTTP wrapper runs next to the Actual server on the LAN. Same mirror
tables as YNAB, so analytics, balances and the value push work unchanged. Amounts are integer cents.
End-to-end encrypted budgets are not supported.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetAccount, BudgetCategory, BudgetTransaction
from app.platform.config import get_settings
from app.platform.connections import get_connection, secret
from app.platform.db import today, utcnow
from app.platform.jobs import JobContext
from app.platform.outbound import OutboundClient, host_allowed

CENTS = Decimal(100)


class ActualError(RuntimeError):
    pass


class Actual:
    def __init__(self, url: str, sync_id: str, api_key: str, client: Any | None = None) -> None:
        self.base = url.rstrip("/") + ("" if url.rstrip("/").endswith("/v1") else "/v1") + f"/budgets/{sync_id}"
        self.headers = {"x-api-key": api_key}
        self.client = client or OutboundClient(timeout=60)

    def _get(self, path: str, **params: Any) -> Any:
        r = self.client.get(f"{self.base}{path}", params=params or None, headers=self.headers)
        if r.status_code in (401, 403):
            raise ActualError("actual-http-api rejected the API key")
        if r.status_code >= 400:
            raise ActualError(f"actual-http-api {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()["data"]

    def accounts(self) -> list[dict[str, Any]]:
        return list(self._get("/accounts", include_balances="true"))

    def category_groups(self) -> list[dict[str, Any]]:
        return list(self._get("/categorygroups"))

    def payees(self) -> dict[str, str]:
        return {p["id"]: p.get("name") or "" for p in self._get("/payees")}

    def transactions(self, account_id: str, since: date) -> list[dict[str, Any]]:
        return list(self._get(f"/accounts/{account_id}/transactions", since_date=since.isoformat()))

    def add_transaction(self, account_id: str, txn: dict[str, Any]) -> None:
        r = self.client.request(
            "POST", f"{self.base}/accounts/{account_id}/transactions", json={"transaction": txn}, headers=self.headers
        )
        if r.status_code >= 400:
            raise ActualError(f"Actual refused the transaction ({r.status_code}): {r.text[:200]}")


def client_from_db(db: Session, client: Any | None = None) -> Actual:
    conn = get_connection(db, "actual")
    cfg = (conn.config or {}) if conn else {}
    if conn is None or not cfg.get("url") or not cfg.get("sync_id") or not conn.secret_enc:
        raise ActualError("Connect Actual in Settings → Connections (actual-http-api URL, budget sync id, API key)")
    host = urlsplit(str(cfg["url"])).hostname or ""
    if not host_allowed(host, get_settings().lan_allowlist + get_settings().outbound_allowlist):
        raise ActualError(f"{host} must be on PM_LAN_ALLOWLIST (Actual runs on your LAN)")
    return Actual(str(cfg["url"]), str(cfg["sync_id"]), secret(conn) or "", client)


def _c(v: int | None) -> Decimal:
    return Decimal(v or 0) / CENTS


def sync(db: Session, ctx: JobContext | None = None, client: Any | None = None) -> dict[str, int]:
    api = client_from_db(db, client)
    conn = get_connection(db, "actual")
    assert conn is not None
    cfg = dict(conn.config or {})
    if not cfg.get("initialized"):  # switching backends: the mirror held the other backend's data
        for model in (BudgetTransaction, BudgetCategory, BudgetAccount):
            db.execute(delete(model))
        cfg["initialized"] = True
    stats = {"accounts": 0, "categories": 0, "transactions": 0, "balances": 0}
    accts = api.accounts()
    for a in accts:
        ba = db.get(BudgetAccount, a["id"]) or BudgetAccount(id=a["id"])
        ba.name, ba.type = a["name"], "actual"
        ba.on_budget, ba.closed = not bool(a.get("offbudget")), bool(a.get("closed"))
        ba.balance = _c(a.get("workingBalance", a.get("balance")))
        db.add(ba)
        stats["accounts"] += 1
    for g in api.category_groups():
        for c in g.get("categories") or []:
            cat = db.get(BudgetCategory, c["id"]) or BudgetCategory(id=c["id"])
            cat.group_name, cat.name = g["name"], c["name"]
            cat.hidden, cat.deleted = bool(c.get("hidden")), False
            cat.is_income = bool(c.get("is_income") or g.get("is_income"))
            db.add(cat)
            stats["categories"] += 1
    payees = api.payees()
    since = today() - timedelta(days=int(cfg.get("history_days", 1100)))
    txns: list[dict[str, Any]] = []
    for a in accts:
        if not a.get("closed"):
            txns.extend(api.transactions(a["id"], since))
    account_of = {t["id"]: t["account"] for t in txns}
    for t in txns:
        account_of.update({s["id"]: t["account"] for s in t.get("subtransactions") or []})
    seen: set[str] = set()

    def upsert(t: dict[str, Any], parent: str | None, deleted: bool) -> None:
        row = db.get(BudgetTransaction, t["id"]) or BudgetTransaction(id=t["id"])
        row.date = date.fromisoformat(t["date"])
        row.amount = _c(t.get("amount"))
        row.account_id = t["account"]
        row.category_id = t.get("category")
        name = payees.get(t.get("payee") or "") or t.get("imported_payee")
        row.payee = str(name)[:255] if name else None
        row.memo = t.get("notes")
        row.transfer_account_id = account_of.get(t.get("transfer_id") or "")
        row.parent_id = parent
        row.deleted = deleted or bool(t.get("tombstone"))
        db.add(row)
        seen.add(t["id"])

    for t in txns:
        subs = [s for s in t.get("subtransactions") or [] if not s.get("tombstone")]
        upsert(t, None, deleted=bool(subs))
        for s in subs:
            upsert(
                {**s, "date": t["date"], "account": t["account"], "payee": s.get("payee") or t.get("payee")},
                t["id"],
                False,
            )
        stats["transactions"] += 1
    # Actual has no delta feed: anything in the window that disappeared was deleted there
    db.flush()
    for old in db.scalars(select(BudgetTransaction).where(BudgetTransaction.date >= since)):
        if old.id not in seen:
            old.deleted = True
    from app.modules.cashflow.ynab import pull_balances

    stats["balances"] = pull_balances(db)
    conn.config = cfg
    conn.status, conn.error, conn.last_sync = "ok", None, utcnow()
    if ctx:
        ctx.log(f"Actual: {stats}")
    return stats


def push_values(
    db: Session, values: dict[int, Decimal], client: Any | None = None, ctx: JobContext | None = None
) -> int:
    api = client_from_db(db, client)
    n = 0
    for ba in db.scalars(
        select(BudgetAccount).where(BudgetAccount.push_account_id.is_not(None), BudgetAccount.closed.is_(False))
    ):
        if ba.on_budget:
            if ctx:
                ctx.warn(f"{ba.name} is on budget; only off-budget accounts can receive values")
            continue
        target = values.get(int(ba.push_account_id or 0))
        if target is None:
            continue
        step = Decimal(max(1, ba.push_precision))
        rounded = (target / step).quantize(Decimal(1)) * step
        delta = rounded - ba.balance
        if abs(delta) < step / 2:
            continue
        api.add_transaction(
            ba.id,
            {
                "account": ba.id,
                "date": today().isoformat(),
                "amount": int(delta * CENTS),
                "payee_name": "Portfolio Manager",
                "notes": f"Value update to {rounded} €",
                "cleared": True,
            },
        )
        ba.balance = rounded
        n += 1
    return n
