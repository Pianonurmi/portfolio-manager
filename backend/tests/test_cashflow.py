"""YNAB sync, balance mapping, push and analytics with a fake YNAB (CF-01..03)."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.cashflow import ynab
from app.platform.db import today


class FakeYnab:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        t = today()
        m1 = (t.replace(day=1) - timedelta(days=20)).isoformat()
        m2 = (t.replace(day=1) - timedelta(days=50)).isoformat()
        self.data = {
            "/budgets": {"budgets": [{"id": "b1", "name": "Perhe"}]},
            "/budgets/b1/accounts": {
                "accounts": [
                    {
                        "id": "a-sp",
                        "name": "S-Pankki",
                        "type": "checking",
                        "on_budget": True,
                        "closed": False,
                        "balance": 6210000,
                    },
                    {
                        "id": "a-nw",
                        "name": "Bank Norwegian",
                        "type": "creditCard",
                        "on_budget": True,
                        "closed": False,
                        "balance": -1240500,
                    },
                    {
                        "id": "a-trk",
                        "name": "Nordnet (tracking)",
                        "type": "otherAsset",
                        "on_budget": False,
                        "closed": False,
                        "balance": 50000000,
                    },
                ]
            },
            "/budgets/b1/categories": {
                "category_groups": [
                    {
                        "name": "Internal Master Category",
                        "categories": [{"id": "c-in", "name": "Inflow: Ready to Assign"}],
                    },
                    {
                        "name": "Kodin kulut",
                        "categories": [{"id": "c-food", "name": "Ruoka"}, {"id": "c-vast", "name": "Vastike"}],
                    },
                ]
            },
            "/budgets/b1/transactions": {
                "server_knowledge": 42,
                "transactions": [
                    {
                        "id": "t1",
                        "date": m1,
                        "amount": 4200000,
                        "account_id": "a-sp",
                        "category_id": "c-in",
                        "payee_name": "Palkka",
                    },
                    {
                        "id": "t2",
                        "date": m1,
                        "amount": -650000,
                        "account_id": "a-sp",
                        "category_id": "c-food",
                        "payee_name": "K-Market",
                    },
                    {"id": "t3", "date": m1, "amount": -410000, "account_id": "a-sp", "category_id": "c-vast"},
                    {"id": "t4", "date": m1, "amount": -500000, "account_id": "a-sp", "transfer_account_id": "a-nw"},
                    {"id": "t5", "date": m2, "amount": 4200000, "account_id": "a-sp", "category_id": "c-in"},
                    {
                        "id": "t6",
                        "date": m2,
                        "amount": -900000,
                        "account_id": "a-nw",
                        "category_id": None,
                        "subtransactions": [
                            {"id": "t6a", "amount": -600000, "category_id": "c-food"},
                            {"id": "t6b", "amount": -300000, "category_id": "c-vast"},
                        ],
                    },
                ],
            },
        }

    def get(self, url: str, params: Any = None, headers: Any = None) -> httpx.Response:
        path = url.replace(ynab.BASE, "")
        assert headers["Authorization"] == "Bearer test-token-123"
        return httpx.Response(200, text=json.dumps({"data": self.data[path]}), request=httpx.Request("GET", url))

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.posted.append(json["transaction"])
        return httpx.Response(201, text="{}", request=httpx.Request(method, url))


def test_sync_balances_push_and_analytics(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    sp = c.post("/api/accounts", json={"name": "S-Pankki", "kind": "bank", "owners": own}).json()
    card = c.post("/api/accounts", json={"name": "Bank Norwegian", "kind": "credit_card", "owners": own}).json()
    aot = c.post("/api/accounts", json={"name": "AOT", "kind": "aot", "owners": own}).json()
    c.post(
        "/api/ledger/transactions",
        json={"account_id": aot["id"], "date": "2025-01-02", "kind": "deposit", "amount": "51234"},
    )
    c.put("/api/connections/ynab", json={"config": {"budget_id": "b1"}, "secret": "test-token-123"})
    fake = FakeYnab()
    stats = ynab.sync(db, client=fake)
    db.commit()
    assert stats["accounts"] == 3 and stats["transactions"] == 6
    c.put("/api/cashflow/ynab/accounts/a-sp", json={"account_id": sp["id"]})
    c.put("/api/cashflow/ynab/accounts/a-nw", json={"account_id": card["id"]})
    assert (
        c.put("/api/cashflow/ynab/accounts/a-sp", json={"push_account_id": aot["id"]}).status_code == 422
    )  # on-budget
    c.put("/api/cashflow/ynab/accounts/a-trk", json={"push_account_id": aot["id"], "push_precision": 100})
    nw = c.get("/api/networth").json()
    items = {i["name"]: i for g in nw["groups"] for i in g["items"]}
    assert items["S-Pankki"]["value"]["value"] == "6210.00"
    assert items["Bank Norwegian"]["value"]["value"] == "-1240.50"
    # analytics: two full months, transfers excluded, split categories counted
    s = c.get("/api/cashflow/summary", params={"months": 3}).json()
    months = {m["month"]: m for m in s["months"]}
    full = [m for m in months.values() if Decimal(m["income"]) > 0]
    assert {m["spending"] for m in full} == {"1060.00", "900.00"}
    assert {c_["category"] for c_ in s["categories"]} == {"Kodin kulut · Ruoka", "Kodin kulut · Vastike"}
    assert nw["monthly_spending"] is not None  # YNAB average replaces the manual figure
    # push: tracking account set to 51 234 € rounded to 100 € -> adjustment +1 200 €
    n = ynab.push_values(db, {aot["id"]: Decimal("51234")}, client=fake)
    assert n == 1 and fake.posted[0]["amount"] == 1200000 and fake.posted[0]["account_id"] == "a-trk"


def test_connection_secret_is_encrypted_and_hidden(auth_client: TestClient, db: Session) -> None:
    from app.platform.models import Connection

    r = auth_client.put(
        "/api/connections/ynab", json={"config": {"budget_id": "b1"}, "secret": "very-secret-token"}
    ).json()
    assert r["has_secret"] is True and "secret" not in json.dumps(r).replace("has_secret", "")
    row = db.query(Connection).one()
    assert row.secret_enc and "very-secret-token" not in row.secret_enc
