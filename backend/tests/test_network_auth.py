"""Network auth mode: no sign-in from the LAN/VPN, but a public address anywhere in the chain must sign in."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.platform.config import get_settings


@pytest.fixture
def lan_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.main import app

    monkeypatch.setattr(get_settings(), "auth_mode", "network")

    def make(peer: str) -> TestClient:
        return TestClient(app, client=(peer, 50000), headers={"X-PM-Request": "1"})

    return make


def test_lan_requests_need_no_login(lan_client: Any) -> None:
    c = lan_client("192.168.1.10")  # the LAN reverse proxy
    st = c.get("/api/auth/status", headers={"X-Forwarded-For": "192.168.1.50"}).json()
    assert st["signed_in"] is True and st["open"] is True and st["setup_required"] is False
    assert st["user"]["username"] == "owner"
    r = c.post("/api/persons", json={"name": "Alex", "is_self": True}, headers={"X-Forwarded-For": "10.8.0.2"})
    assert r.status_code == 201  # VPN client through the proxy
    assert c.get("/api/persons").status_code == 200


def test_public_address_in_the_chain_must_sign_in(lan_client: Any) -> None:
    c = lan_client("192.168.1.10")
    st = c.get("/api/auth/status", headers={"X-Forwarded-For": "203.0.113.7"}).json()
    assert st["signed_in"] is False and st["open"] is False
    assert c.get("/api/persons", headers={"X-Forwarded-For": "192.168.1.5, 203.0.113.7"}).status_code == 401
    assert lan_client("203.0.113.7").get("/api/persons").status_code == 401  # direct public peer
    assert c.get("/api/persons", headers={"X-Forwarded-For": "not-an-ip"}).status_code == 401


def test_csrf_header_still_required(lan_client: Any) -> None:
    from app.main import app

    c = TestClient(app, client=("192.168.1.10", 50000))
    assert c.post("/api/persons", json={"name": "x"}).status_code == 403


def test_login_mode_is_the_default(client: TestClient) -> None:
    assert get_settings().auth_mode == "login"
    st = client.get("/api/auth/status").json()
    assert st["signed_in"] is False and st["open"] is False
