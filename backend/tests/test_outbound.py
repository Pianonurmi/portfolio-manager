import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.models import OutboundLog
from app.platform.outbound import OutboundBlocked, OutboundClient, host_allowed


def test_host_matching() -> None:
    allow = ["api.ynab.com", "ecb.europa.eu"]
    assert host_allowed("api.ynab.com", allow)
    assert host_allowed("data-api.ecb.europa.eu", allow)
    assert not host_allowed("evil-ecb.europa.eu.attacker.com", allow)
    assert not host_allowed("ynab.com", allow)


def test_blocked_request_is_logged(db: Session) -> None:
    with OutboundClient(allowlist=["api.ynab.com"]) as c, pytest.raises(OutboundBlocked):
        c.get("https://example.com/steal?x=1")
    with OutboundClient(allowlist=["api.ynab.com"]) as c, pytest.raises(OutboundBlocked):
        c.get("http://api.ynab.com/plain-http")
    rows = db.scalars(select(OutboundLog)).all()
    assert {r.host for r in rows} == {"example.com", "api.ynab.com"}
    assert all(r.blocked for r in rows)


def test_outbound_api_counts_blocked_calls(auth_client: TestClient) -> None:
    with OutboundClient(allowlist=["api.ynab.com"]) as c, pytest.raises(OutboundBlocked):
        c.get("https://example.com/steal")
    rows = {r["host"]: r for r in auth_client.get("/api/outbound").json()}
    assert rows["example.com"]["allowed"] is False
    assert rows["example.com"]["calls"] == 1 and rows["example.com"]["blocked"] == 1
