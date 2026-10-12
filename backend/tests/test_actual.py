"""Actual Budget as the budget backend through actual-http-api (CF-04), with a fake server."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.cashflow import backend
from app.modules.cashflow.models import BudgetCategory
from app.platform.db import today

BASE = "http://actual.lan:5007/v1/budgets/sync-1"


class FakeActual:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        m1 = (today().replace(day=1) - timedelta(days=20)).isoformat()
        self.routes: dict[str, Any] = {
            "/accounts": [
                {"id": "chk", "name": "S-Pankki", "offbudget": False, "closed": False, "workingBalance": 621000},
                {"id": "cc", "name": "Norwegian", "offbudget": False, "closed": False, "workingBalance": -124050},
                {
                    "id": "inv",
                    "name": "Nordnet (tracking)",
                    "offbudget": True,
                    "closed": False,
                    "workingBalance": 5000000,
                },
            ],
            "/categorygroups": [
                {
                    "id": "g1",
                    "name": "Kodin kulut",
                    "is_income": False,
                    "categories": [{"id": "food", "name": "Ruoka"}, {"id": "rent", "name": "Vastike"}],
                },
                {"id": "g2", "name": "Income", "is_income": True, "categories": [{"id": "sal", "name": "Palkka"}]},
            ],
            "/payees": [{"id": "p1", "name": "Prisma"}, {"id": "p2", "name": "Työnantaja"}],
            "/accounts/chk/transactions": [
                {"id": "t1", "account": "chk", "date": m1, "amount": 350000, "payee": "p2", "category": "sal"},
                {
                    "id": "t2",
                    "account": "chk",
                    "date": m1,
                    "amount": -30000,
                    "payee": "p1",
                    "category": None,
                    "subtransactions": [
                        {"id": "t2a", "amount": -20000, "category": "food"},
                        {"id": "t2b", "amount": -10000, "category": "rent"},
                    ],
                },
                {"id": "t3", "account": "chk", "date": m1, "amount": -50000, "transfer_id": "t4"},
            ],
            "/accounts/cc/transactions": [
                {"id": "t4", "account": "cc", "date": m1, "amount": 50000, "transfer_id": "t3"},
            ],
            "/accounts/inv/transactions": [],
        }

    def get(self, url: str, params: Any = None, headers: Any = None) -> httpx.Response:
        assert headers["x-api-key"] == "key-123"
        path = url.replace(BASE, "")
        return httpx.Response(200, text=json.dumps({"data": self.routes[path]}), request=httpx.Request("GET", url))

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.posted.append((url.replace(BASE, ""), json["transaction"]))
        return httpx.Response(200, text='{"message":"ok"}', request=httpx.Request(method, url))


def test_actual_sync_analytics_and_push(auth_client: TestClient, db: Session, monkeypatch: Any) -> None:
    from app.platform.config import get_settings

    monkeypatch.setattr(get_settings(), "lan_allowlist", ["actual.lan"])
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    card = c.post("/api/accounts", json={"name": "Norwegian", "kind": "credit_card", "owners": own}).json()
    aot = c.post("/api/accounts", json={"name": "AOT", "kind": "aot", "owners": own}).json()
    c.post(
        "/api/ledger/transactions",
        json={"account_id": aot["id"], "date": "2025-01-02", "kind": "deposit", "amount": "51234"},
    )
    # a stale YNAB category from before the switch must not survive the first Actual sync
    db.add(BudgetCategory(id="ynab-old", group_name="Old", name="Old"))
    db.commit()
    c.put(
        "/api/connections/actual",
        json={"config": {"url": "http://actual.lan:5007", "sync_id": "sync-1"}, "secret": "key-123"},
    )
    assert c.get("/api/cashflow/backend").json() == {"kind": "actual", "name": "Actual Budget"}
    fake = FakeActual()
    stats = backend.sync(db, client=fake)
    db.commit()
    assert stats == {"accounts": 3, "categories": 3, "transactions": 4, "balances": 0}
    assert db.get(BudgetCategory, "ynab-old") is None
    c.put("/api/cashflow/ynab/accounts/cc", json={"account_id": card["id"]})
    c.put("/api/cashflow/ynab/accounts/inv", json={"push_account_id": aot["id"], "push_precision": 100})
    nw = c.get("/api/networth").json()
    items = {i["name"]: i for g in nw["groups"] for i in g["items"]}
    assert items["Norwegian"]["value"]["value"] == "-1240.50"  # liability from the account kind
    s = c.get("/api/cashflow/summary", params={"months": 3}).json()
    month = next(m for m in s["months"] if Decimal(m["income"]) > 0)
    # income from the income category; split counted per category; transfer to the card excluded
    assert month["income"] == "3500.00" and month["spending"] == "300.00"
    assert {x["category"] for x in s["categories"]} == {"Kodin kulut · Ruoka", "Kodin kulut · Vastike"}
    n = backend.push_values(db, {aot["id"]: Decimal("51234")}, client=fake)
    assert n == 1 and fake.posted[0][0] == "/accounts/inv/transactions" and fake.posted[0][1]["amount"] == 120000
