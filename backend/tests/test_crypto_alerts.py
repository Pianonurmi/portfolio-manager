"""Crypto balance sync and alert channels with fake services (ING-10, ALERT-01..03)."""

import json
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.alerts import notify
from app.modules.alerts.models import AlertLog
from app.modules.crypto import providers
from app.modules.crypto.service import sync_all
from app.platform.outbound import redact


class Fake:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes, self.calls = routes, []

    def _resp(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append((method, url, kw))
        for k, v in self.routes.items():
            if url.startswith(k):
                return httpx.Response(200, text=json.dumps(v), request=httpx.Request(method, url))
        return httpx.Response(404, request=httpx.Request(method, url))

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self._resp("GET", url, **kw)

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        return self._resp(method, url, **kw)


def test_kraken_asset_codes() -> None:
    assert [providers.kraken_asset(c) for c in ["XXBT", "XETH", "ZEUR", "ETH.S", "DOT", "USDC.M"]] == [
        "BTC",
        "ETH",
        "EUR",
        "ETH",
        "DOT",
        "USDC",
    ]


def test_crypto_sync_books_balance_changes(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts",
        json={"name": "Kraken", "kind": "crypto_exchange", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    c.put("/api/connections/kraken", json={"config": {"api_key": "k", "account_id": a["id"]}, "secret": "c2VjcmV0"})
    fake = Fake(
        {
            "https://api.kraken.com/0/private/Balance": {
                "error": [],
                "result": {"XXBT": "0.034", "ETH.S": "0.2", "ZEUR": "12.5"},
            }
        }
    )
    assert sync_all(db, clients={"kraken": fake})["kraken"] == 3
    db.commit()
    pos = {p["instrument"]: Decimal(p["quantity"]) for p in c.get("/api/ledger/positions").json()}
    assert pos == {"BTC": Decimal("0.034"), "ETH": Decimal("0.2"), "Cash EUR": Decimal("12.5")}
    # second sync with a smaller BTC balance books only the difference; unchanged assets untouched
    fake.routes["https://api.kraken.com/0/private/Balance"]["result"]["XXBT"] = "0.030"
    assert sync_all(db, clients={"kraken": fake})["kraken"] == 1
    sig = fake.calls[0][2]["headers"]["API-Sign"]
    assert len(sig) > 40 and "API-Key" in fake.calls[0][2]["headers"]


def test_address_balances_btc_and_eth() -> None:
    fake = Fake(
        {
            "https://mempool.space/api/address/bc1q": {
                "chain_stats": {"funded_txo_sum": 5000000, "spent_txo_sum": 1000000}
            },
            "https://ethereum-rpc.publicnode.com": {"result": hex(2 * 10**17)},
        }
    )
    bal = providers.address_balances(
        [{"chain": "btc", "address": "bc1qxyz"}, {"chain": "eth", "address": "0xabc"}], fake
    )
    assert bal == {"BTC": Decimal("0.04"), "ETH": Decimal("0.2")}


def test_notifications_dedupe_and_channels(auth_client: TestClient, db: Session, monkeypatch: Any) -> None:
    auth_client.put("/api/connections/gotify", json={"config": {"url": "http://gotify.lan"}, "secret": "tok"})
    sent: list[str] = []
    monkeypatch.setattr(notify, "gotify", lambda db, title, body, priority=5: sent.append(title) or True)
    assert notify.send(db, "k1", "Hello", "World") is True
    assert notify.send(db, "k1", "Hello", "World") is False  # quiet period
    assert sent == ["Hello"]
    assert db.scalar(select(AlertLog.channels)) == "gotify"


def test_email_channel(auth_client: TestClient, db: Session) -> None:
    auth_client.put("/api/connections/smtp", json={"config": {"host": "mail.lan", "port": 25, "to": "me@example.org"}})
    msgs: list[Any] = []

    class FakeSMTP:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *a: Any) -> None: ...
        def send_message(self, m: Any) -> None:
            msgs.append(m)

    assert notify.email(db, "Hi", "Body", smtp_factory=FakeSMTP) is True
    assert msgs[0]["To"] == "me@example.org" and msgs[0]["Subject"] == "[Portfolio Manager] Hi"


def test_outbound_log_redacts_secrets() -> None:
    assert redact("/message?token=abc&x=1") == "/message?token=***&x=1"
    assert redact("/api/v3/account?timestamp=1&signature=deadbeef") == "/api/v3/account?timestamp=1&signature=***"
