from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.modules.ai.api import AskIn, ask
from app.platform.config import get_settings


def test_stress_policies_estate_timeline(auth_client: TestClient) -> None:
    c = auth_client
    p = {
        "age_now": 40,
        "start_wealth": 300000,
        "monthly_savings": 1000,
        "retire_age": 55,
        "real_return": 0.045,
        "volatility": 0.14,
        "monthly_spending": 3000,
        "pension_monthly": 2000,
        "pension_age": 67,
    }
    s = c.post("/api/plan/stress", json={"params": p}).json()
    assert (
        s["crash_at_retirement"]["success"] <= s["base"]["success"]
        and s["lost_decade"]["success"] <= s["base"]["success"]
    )
    c.post(
        "/api/policies",
        json={"kind": "life", "insurer": "LähiTapiola", "coverage": "200000", "renewal_date": "2027-01-01"},
    )
    assert c.get("/api/policies").json()["adequacy"]["covers_debt"] is True
    assert (
        c.post("/api/estate", json={"section": "instruction", "title": "Bank", "body": "salasana: 1234"}).status_code
        == 422
    )
    c.post("/api/estate", json={"section": "location", "title": "Documents", "body": "Paperless and the fireproof box"})
    html = c.get("/api/estate/package").text
    assert "If something happens to me" in html and "LähiTapiola" in html and "fireproof" in html
    assert any(t["kind"] == "policy.renewal" for t in c.get("/api/timeline").json())


def test_ai_requires_lan_and_cites_data(auth_client: TestClient, db: Any, monkeypatch: Any) -> None:
    c = auth_client
    c.put("/api/connections/llm", json={"config": {"url": "http://ollama.lan:11434", "model": "qwen2.5"}})
    assert c.post("/api/ai/ask", json={"question": "What is my net worth?"}).status_code == 422  # not on LAN allowlist
    monkeypatch.setattr(get_settings(), "lan_allowlist", ["ollama.lan"])

    class Fake:
        def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
            assert url == "http://ollama.lan:11434/v1/chat/completions" and "DATA:" in json["messages"][1]["content"]
            return httpx.Response(
                200,
                text=__import__("json").dumps({"choices": [{"message": {"content": "Net worth is 0 €."}}]}),
                request=httpx.Request(method, url),
            )

    r = ask(AskIn(question="What is my net worth?"), None, db, client=Fake())  # type: ignore[arg-type]
    assert r["answer"] == "Net worth is 0 €." and "net_worth_eur" in r["data_used"]
