"""Document extraction with the local LLM: staged, verified against quotes, applied only when confirmed."""

import json
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.ai import extract
from app.modules.ai.api import ExtractIn, extract_document
from app.platform.config import get_settings

LOAN_LETTER = """Nordea Pankki Suomi Oyj
Lainanumero 1234 5678 90
Helsinki 3.3.2026
Ilmoitus koronmuutoksesta
Lainan jäljellä oleva pääoma 187 432,15 euroa.
Viitekorko 12 kk Euribor 2,215 % ja marginaali 0,55 %.
Seuraava koronmuutospäivä 3.3.2027.
Uusi kuukausierä 1 012,40 €. Viimeinen eräpäivä 3.3.2049.
"""

ANSWER = {
    "kind": "loan_letter",
    "fields": {
        "as_of": {"value": "2026-03-03", "quote": "Helsinki 3.3.2026"},
        "outstanding_balance": {"value": 187432.15, "quote": "jäljellä oleva pääoma 187 432,15 euroa"},
        "reference_rate_name": {"value": "12 kk Euribor", "quote": "Viitekorko 12 kk Euribor 2,215 %"},
        "reference_rate": {"value": 2.215, "quote": "12 kk Euribor 2,215 %"},
        "margin": {"value": "0,55", "quote": "marginaali 0,55 %"},
        "next_reset": {"value": "3.3.2027", "quote": "Seuraava koronmuutospäivä 3.3.2027"},
        "monthly_payment": {"value": 1100, "quote": "Uusi kuukausierä 1 012,40 €"},  # wrong: not in the quote
        "end_date": {"value": "2049-03-03", "quote": "Viimeinen eräpäivä 3.3.2049"},
        "account_hint": {"value": "1234 5678 90", "quote": "Lainanumero 1234 5678 90"},
        "institution": {"value": "Nordea", "quote": "invented text"},
    },
}


class FakePaperless:
    def get(self, url: str, headers: Any = None, **kw: Any) -> httpx.Response:
        assert url == "http://paperless.lan:8000/api/documents/42/"
        body = {"id": 42, "title": "Koronmuutosilmoitus 2026", "content": LOAN_LETTER}
        return httpx.Response(200, text=json.dumps(body), request=httpx.Request("GET", url))


class FakeLlm:
    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.payload = json
        content = __import__("json").dumps(ANSWER)
        return httpx.Response(
            200,
            text=__import__("json").dumps({"choices": [{"message": {"content": f"```json\n{content}\n```"}}]}),
            request=httpx.Request(method, url),
        )


def test_numbers_and_dates() -> None:
    assert extract.parse_number("187 432,15") == Decimal("187432.15")
    assert extract.parse_number("1.234,56 €") == Decimal("1234.56")
    assert extract.parse_number("1,234.56") == Decimal("1234.56")
    assert extract.parse_date("3.3.2027").isoformat() == "2027-03-03"  # type: ignore[union-attr]


def test_loan_letter_extracted_verified_and_applied(auth_client: TestClient, db: Session, monkeypatch: Any) -> None:
    c = auth_client
    monkeypatch.setattr(get_settings(), "lan_allowlist", ["ollama.lan", "paperless.lan"])
    c.put("/api/connections/llm", json={"config": {"url": "http://ollama.lan:11434", "model": "qwen2.5"}})
    c.put("/api/connections/paperless", json={"config": {"url": "http://paperless.lan:8000"}, "secret": "tok"})
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    loan = c.post(
        "/api/accounts", json={"name": "Asuntolaina", "kind": "mortgage", "external_ref": "1234567890", "owners": own}
    ).json()
    c.post("/api/accounts", json={"name": "Nordea käyttötili", "kind": "bank", "owners": own})
    llm = FakeLlm()
    x = extract_document(ExtractIn(paperless_id=42), None, db, client=FakePaperless(), llm_client=llm)  # type: ignore[arg-type]
    db.commit()
    assert (
        llm.payload["response_format"] == {"type": "json_object"}
        and "DOCUMENT TEXT" in llm.payload["messages"][1]["content"]
    )
    f = x["fields"]
    assert x["kind"] == "loan_letter" and x["status"] == "review" and x["account_id"] == loan["id"]
    assert f["outstanding_balance"] == {
        "value": "187432.15",
        "quote": "jäljellä oleva pääoma 187 432,15 euroa",
        "found": True,
        "type": "money",
    }
    assert f["margin"]["value"] == "0.55" and f["margin"]["found"] and f["next_reset"]["value"] == "2027-03-03"
    assert f["monthly_payment"]["found"] is False  # the value is not in its own quote
    assert f["institution"]["found"] is False  # the quote is not in the document
    # nothing changed yet
    assert c.get(f"/api/accounts/{loan['id']}/meta").json() == {}
    listed = c.get("/api/ai/extractions", params={"status": "review"}).json()
    assert [e["id"] for e in listed] == [x["id"]]
    r = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"values": {"monthly_payment": "1012.40"}}).json()
    assert r["status"] == "accepted" and r["undo_id"]
    meta = c.get(f"/api/accounts/{loan['id']}/meta").json()
    assert meta["reference_rate"] == "0.022150" and meta["margin"] == "0.005500" and meta["next_reset"] == "2027-03-03"
    vals = c.get("/api/valuations", params={"account_id": loan["id"]}).json()
    assert Decimal(vals[0]["value"]) == Decimal("187432.15") and vals[0]["date"] == "2026-03-03"
    docs = c.get("/api/documents/links", params={"subject_type": "account", "subject_id": loan["id"]}).json()
    assert docs[0]["paperless_id"] == 42
    assert c.post(f"/api/ai/extractions/{x['id']}/accept", json={}).status_code == 404  # only once


def test_pension_record_updates_planning(auth_client: TestClient, db: Session) -> None:
    from app.modules.ai.models import Extraction
    from app.platform.models import AppUser

    c = auth_client
    c.post("/api/persons", json={"name": "Alex", "is_self": True})
    text = "Työeläkeote 2026. Karttunut eläkkeesi 31.12.2025 on 1 432,10 euroa kuukaudessa. Ansiot 2025: 58 200 €"
    fields = extract.verify(
        "pension_record",
        {
            "statement_year": {"value": 2026, "quote": "Työeläkeote 2026"},
            "accrued_monthly": {"value": "1432.10", "quote": "1 432,10 euroa kuukaudessa"},
            "annual_earnings": {"value": 58200, "quote": "Ansiot 2025: 58 200 €"},
        },
        text,
    )
    assert all(f["found"] for f in fields.values())
    x = Extraction(paperless_id=7, title="Työeläkeote", kind="pension_record", status="review", fields=fields)
    db.add(x)
    db.commit()
    assert c.post(f"/api/ai/extractions/{x.id}/accept", json={}).status_code == 200
    db.expire_all()
    user = db.query(AppUser).one()
    assert user.settings["pension"]["accrued_monthly"] == 1432.10 and user.settings["pension"]["statement_year"] == 2026
