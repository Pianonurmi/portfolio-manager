"""Fund factsheets read by the local AI into a "factsheet" instrument profile (fallback behind Morningstar)."""

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.ai import attachments, factsheet, statements
from app.modules.ai.jobs import read_attachments
from app.modules.core.models import Instrument
from app.modules.portfolio import profiles
from app.platform.config import get_settings
from app.platform.jobs import JobContext
from tests.test_attachments import gateway, text_pdf  # noqa: F401

PAGE = [
    "Esimerkki Maailma Osake UCITS ETF  IE00B4L5Y983  Kuukausiraportti 30.9.2026",
    "Juoksevat kulut 0,20 %",
    "Osakkeet 98,5 %  Kateinen 1,5 %",
    "Yhdysvallat 70,1 %  Japani 6,2 %  Euroalue 10,0 %  Pohjois-Amerikka 3,0 %",
    "Teknologia 24,0 %  Rahoitus 15,5 %  Terveydenhuolto 11,0 %",
    "Saksa 2,9 %",
    "Duration 4,2 vuotta",
]
ANSWER: dict[str, Any] = {
    "fund_name": "Esimerkki Maailma Osake UCITS ETF",
    "isins": ["IE00B4L5Y983"],
    "share_class": "Acc",
    "as_of": "2026-09-30",
    "ongoing_charge": 0.2,
    "asset_allocation": {"equity": 98.5, "bond": None, "cash": 1.5, "other": None},
    "regions": [
        {"name": "Yhdysvallat", "pct": 70.1},
        {"name": "Japani", "pct": 6.2},
        {"name": "Euroalue", "pct": 10.0},
        {"name": "Pohjois-Amerikka", "pct": 3.0},
    ],
    "countries": [{"name": "Saksa", "pct": 2.9}, {"name": "Germany", "pct": 99.9}],
    "sectors": [
        {"name": "Teknologia", "pct": 24.0},
        {"name": "Financials", "pct": 15.5},
        {"name": "Health Care", "pct": 11.0},
        {"name": "Mystery", "pct": 5.0},
    ],
    "cap_size": [],
    "bond": {"duration": 4.2, "ytm": None, "credit_quality": None},
}


class FactsheetGateway:
    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer if answer is not None else ANSWER
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.calls.append({"payload": json})
        schema = json["response_format"]["json_schema"]["schema"]
        body = (
            {"kind": "factsheet", "institution": "iShares", "account": None, "as_of": "2026-09-30", "language": "fi"}
            if "kind" in schema["properties"]
            else self.answer
        )
        resp = {
            "model": json["model"],
            "choices": [{"message": {"content": __import__("json").dumps(body)}, "finish_reason": "stop"}],
            "usage": {},
        }
        return httpx.Response(200, text=__import__("json").dumps(resp), request=httpx.Request(method, url))


def _fund(db: Session, isin: str | None = "IE00B4L5Y983") -> Instrument:
    inst = Instrument(type="etf", name="Esimerkki World", currency="EUR", isin=isin)
    db.add(inst)
    db.flush()
    return inst


def test_classification_routes_to_factsheet(db: Session, gateway: None) -> None:  # noqa: F811
    units = attachments.load("esite.pdf", text_pdf(PAGE))
    header, _ = statements.classify(db, units, FactsheetGateway())
    assert header["kind"] == "factsheet" and "factsheet" in statements.KINDS


def test_normalise_percent_aliases_and_unknowns(db: Session, gateway: None) -> None:  # noqa: F811
    fs = factsheet.normalise(ANSWER, "\n".join(PAGE))
    assert fs["ter"] == 0.002 and fs["asset_classes"] == {"equity": 0.985, "cash": 0.015}
    labels = {r["name"]: r["label"] for r in fs["regions"]}
    assert labels == {
        "Yhdysvallat": "United States",
        "Japani": "Japan",
        "Euroalue": "Eurozone",
        "Pohjois-Amerikka": None,
    }
    assert {c["name"]: c["label"] for c in fs["countries"]} == {"Saksa": "DEU", "Germany": "DEU"}
    assert {s["name"]: s["label"] for s in fs["sectors"]} == {
        "Teknologia": "Technology",
        "Financials": "Financial Services",
        "Health Care": "Healthcare",
        "Mystery": None,
    }
    assert fs["bond"] == {"modified_duration": 4.2} and fs["as_of"] == "2026-09-30"
    data = factsheet.profile_data(fs)  # unmapped names are left out, never guessed
    assert data["regions"] == {"United States": 0.701, "Japan": 0.062, "Eurozone": 0.1}
    assert data["sectors"]["Technology"] == 0.24 and "Mystery" not in data["sectors"]
    assert factsheet.match_instrument(db, fs) is None


def test_figure_not_in_the_text_is_flagged() -> None:
    fs = factsheet.normalise({**ANSWER, "ongoing_charge": 0.75}, "\n".join(PAGE))
    assert fs["checks"]["ter"] is False and fs["checks"]["asset_classes.equity"] is True
    shown = {r["name"]: r["found"] for r in fs["regions"]}
    assert shown["Japani"] is True
    assert factsheet.normalise(ANSWER, None)["checks"]["ter"] is None  # a scan: nothing to compare with


def test_upload_read_and_accept_as_fund_profile(
    auth_client: TestClient,
    db: Session,
    gateway: None,  # noqa: F811
    tmp_path: Any,
    monkeypatch: Any,  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path))
    inst = _fund(db)
    db.commit()
    c = auth_client
    r = c.post("/api/ai/attachments", files=[("files", ("esite.pdf", text_pdf(PAGE), "application/pdf"))])
    assert r.status_code == 201, r.text
    read_attachments(JobContext(db=db), client=FactsheetGateway())
    x = c.get("/api/ai/extractions").json()[0]
    assert x["status"] == "review" and x["kind"] == "factsheet"
    assert x["data"]["factsheet"]["instrument_id"] == inst.id  # picked by ISIN
    assert c.post(f"/api/ai/extractions/{x['id']}/accept", json={}).status_code == 422  # no fund chosen
    edited = x["data"]["factsheet"]
    edited["regions"][3]["label"] = "United States"  # the person decides what North America means here
    a = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"instrument_id": inst.id, "factsheet": edited})
    assert a.status_code == 200, a.text
    assert a.json()["applied"]["instrument"] == "Esimerkki World"
    db.expire_all()
    data, sources = profiles.effective(db, inst)
    assert sources["ter"]["source"] == "factsheet" and data["ter"] == 0.002
    assert data["regions"]["United States"] == 0.731 and sources["regions"]["as_of"] == "2026-09-30"
    # Morningstar wins per field once it has data; the factsheet still fills what Morningstar lacks
    profiles.save(db, inst.id, "morningstar", None, {"ter": 0.0018, "regions": {"Japan": 1.0}})
    data, sources = profiles.effective(db, inst)
    assert data["ter"] == 0.0018 and sources["ter"]["source"] == "morningstar"
    assert data["regions"] == {"Japan": 1.0}
    assert sources["sectors"]["source"] == "factsheet" and data["sectors"]["Technology"] == 0.24
    # accepting a newer factsheet replaces the earlier factsheet profile
    c.post("/api/ai/attachments", files=[("files", ("esite2.pdf", text_pdf(PAGE), "application/pdf"))])
    read_attachments(JobContext(db=db), client=FactsheetGateway({**ANSWER, "ongoing_charge": 0.2}))
    y = c.get("/api/ai/extractions").json()[0]
    c.post(f"/api/ai/extractions/{y['id']}/accept", json={"instrument_id": inst.id, "factsheet": {"ter": 0.0025}})
    db.expire_all()
    rows = [p for p in profiles.effective_many(db, [inst])[inst.id].rows if p.source == "factsheet"]
    assert len(rows) == 1 and rows[0].data == {"ter": 0.0025}
    json.dumps(rows[0].data)
