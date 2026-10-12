"""Attachments read by the local AI: file types, page splitting, holdings checks, review and booking (ING-09)."""

import io
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from app.modules.ai import attachments, statements
from app.modules.ai.jobs import read_attachments
from app.platform.config import get_settings
from app.platform.jobs import JobContext

STATEMENT = [
    "Nordnet Bank AB Suomen sivuliike",
    "Arvo-osuustili 12345678 Sailytysraportti 30.9.2026",
    "Arvopaperi ISIN Maara Kurssi Valuutta Markkina-arvo Hankinta-arvo",
    "iShares Core MSCI World IE00B4L5Y983 65 98,42 EUR 6 397,30 4 289,75",
    "Sampo Oyj A FI4000552500 120 41,18 EUR 4 941,60 4 210,00",
    "Nordea Bank Abp FI4000297767 300 12,64 EUR 3 792,00 2 970,40",
    "Kateinen EUR 412,87",
    "Yhteensa 15 543,77",
]


def text_pdf(lines: list[str], pages: int = 1, more: list[list[str]] | None = None) -> bytes:
    """A minimal PDF with a real text layer (Helvetica), built by hand; `more` adds pages with other lines."""
    page_lines = [lines] * pages + (more or [])
    pages = len(page_lines)
    objs: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    font_ref = 3 + 2 * pages
    for page in page_lines:
        n = len(objs) + 1
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R "
            f"/Resources << /Font << /F1 {font_ref} 0 R >> >> >>".encode()
        )
        body = "BT /F1 10 Tf 40 750 Td 14 TL " + " ".join(f"({ln}) Tj T*" for ln in page) + " ET"
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def image_bytes(w: int, h: int, fmt: str = "PNG") -> bytes:
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).text((10, 10), "Sampo 120 41,18", fill="black")
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


ROWS = [
    ["iShares Core MSCI World", "IE00B4L5Y983", 65, 98.42, "EUR", 6397.3, "EUR", 4289.75],
    ["Sampo Oyj A", "FI4000552500", 120, 41.18, "EUR", 4941.6, "EUR", 4210.0],
    ["Nordea Bank Abp", "FI4000297767", 300, 12.64, "EUR", 3792.0, "EUR", 2970.4],
]


class FakeGateway:
    """Answers like the LAN gateway: classify, then holdings per unit; can run out of output once."""

    def __init__(self, rows: list[list[Any]] | None = None, length_once: bool = False, total: float = 15543.77) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows = rows if rows is not None else ROWS
        self.length_once = length_once
        self.total = total

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.calls.append({"url": url, "payload": json, "headers": headers})
        schema = json["response_format"]["json_schema"]["schema"]
        finish = "stop"
        if "kind" in schema["properties"]:
            body: dict[str, Any] = {
                "kind": "holdings",
                "institution": "Nordnet Bank AB",
                "account": "12345678",
                "as_of": "2026-09-30",
                "language": "fi",
            }
        else:
            if self.length_once:
                self.length_once = False
                finish = "length"
            body = {
                "account": "12345678",
                "as_of": "2026-09-30",
                "rows": self.rows,
                "cash": 412.87,
                "cash_ccy": "EUR",
                "total": self.total,
                "total_ccy": "EUR",
            }
        content = __import__("json").dumps(body)
        resp = {
            "model": json["model"],
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {},
        }
        return httpx.Response(200, text=__import__("json").dumps(resp), request=httpx.Request(method, url))


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "lan_allowlist", ["192.168.1.20"])
    monkeypatch.setattr(get_settings(), "llm_url", "http://192.168.1.20:4000")


def test_file_types_become_units(tmp_path: Any) -> None:
    pdf = attachments.load("raportti.pdf", text_pdf(STATEMENT, pages=2))
    assert [u.label for u in pdf] == ["page 1", "page 2"] and "IE00B4L5Y983" in (pdf[0].text or "")
    assert pdf[0].as_image is not None and pdf[0].as_image()[0].is_image  # fallback rendering works
    scan = io.BytesIO()
    Image.open(io.BytesIO(image_bytes(850, 1100))).save(scan, "PDF")
    units = attachments.load("skannaus.pdf", scan.getvalue())
    assert len(units) == 1 and units[0].is_image and units[0].data_url().startswith("data:image/jpeg;base64,")
    tall = attachments.load("screenshot.png", image_bytes(1080, 4800))
    assert len(tall) >= 3 and all(u.is_image for u in tall) and "tile 1/" in tall[0].label
    csv_units = attachments.load("holdings.csv", ("Name;ISIN;Qty\n" + "Sampo;FI4000552500;120\n" * 600).encode())
    assert len(csv_units) > 1 and all((u.text or "").startswith("Name;ISIN;Qty") for u in csv_units)
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Rahasto", "Osuudet", "Arvo"])
    ws.append(["Seligson Global Top 25", 512.33, 9120.5])
    buf = io.BytesIO()
    wb.save(buf)
    x = attachments.load("rahastot.xlsx", buf.getvalue())
    assert "Seligson Global Top 25 | 512.33 | 9120.5" in (x[0].text or "")
    with pytest.raises(attachments.AttachmentError):
        attachments.load("notes.docx", b"PK\x03\x04...")


def test_split_when_output_runs_out_and_checks(db: Session, gateway: None) -> None:
    units = attachments.load("raportti.pdf", text_pdf(STATEMENT))
    fake = FakeGateway(length_once=True)
    header, _ = statements.classify(db, units, fake)
    res = statements.holdings(db, units, header, fake)
    # first answer ran out of room: the page was read again as two halves (rows deduplicated)
    assert len(fake.calls) == 4 and len(res.rows) == 3
    assert all(r["status"] == "ok" and r["in_text"] for r in res.rows)
    assert res.checks == [
        {
            "account": "12345678",
            "what": "holdings + cash = statement total",
            "computed": "15543.77",
            "stated": "15543.77",
            "ok": True,
        }
    ]
    sent = fake.calls[-1]
    assert sent["payload"]["max_tokens"] == 4096  # the gateway's budget for this app; it must be asked for
    assert sent["headers"]["X-Local-AI-Source"] == "portfolio-manager"
    assert sent["headers"]["X-Local-AI-Priority"] == "background"
    assert sent["url"] == "http://192.168.1.20:4000/v1/chat/completions"
    assert "IE00B4L5Y983" not in sent["headers"].get("X-Local-AI-Task-Label", "")  # no content in metadata


def test_misread_numbers_are_flagged(db: Session, gateway: None) -> None:
    units = attachments.load("raportti.pdf", text_pdf(STATEMENT))
    bad = [list(r) for r in ROWS]
    bad[1][2], bad[1][5] = 12, 494.16  # quantity and value both misread, consistent with each other, not on the page
    res = statements.holdings(db, units, {"institution": "Nordnet", "as_of": "2026-09-30"}, FakeGateway(rows=bad))
    sampo = next(r for r in res.rows if r["isin"] == "FI4000552500")
    # the row is rebuilt from its own printed line: 120 × 41,18 = 4 941,60
    assert sampo["quantity"] == Decimal("120") and sampo["value"] == Decimal("4941.60") and sampo["status"] == "ok"
    assert "re-read from the line" in sampo["fixed"] and res.checks[0]["ok"] is True


def test_upload_read_review_book_and_undo(
    auth_client: TestClient, db: Session, gateway: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path))
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    aot = c.post(
        "/api/accounts",
        json={
            "name": "Nordnet AOT",
            "kind": "aot",
            "external_ref": "12345678",
            "owners": [{"entity_id": me["entity_id"], "share": "1"}],
        },
    ).json()
    r = c.post(
        "/api/ai/attachments",
        files=[
            ("files", ("raportti.pdf", text_pdf(STATEMENT), "application/pdf")),
            ("files", ("readme.docx", b"PK\x03\x04", "application/octet-stream")),
        ],
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out[0]["status"] == "queued" and out[1]["status"] == "error"
    # a Nordnet transaction export belongs under Imports
    from tests.test_portfolio import FIX

    nn = c.post(
        "/api/ai/attachments", files=[("files", ("t.csv", (FIX / "transactions-fi.csv").read_bytes(), "text/csv"))]
    )
    assert "Imports" in nn.json()[0]["error"]
    read_attachments(JobContext(db=db), client=FakeGateway())
    x = c.get("/api/ai/extractions").json()[0]
    assert x["status"] == "review" and x["kind"] == "holdings" and x["account_id"] == aot["id"]
    assert len(x["data"]["rows"]) == 3 and x["progress"]["step"] == "done"
    assert c.get(f"/api/ai/extractions/{x['id']}/file").content.startswith(b"%PDF")
    # book: opening positions with the statement's purchase values, cash, and statement prices
    a = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"mode": "book"}).json()
    assert a["applied"]["transactions"] == 4 and a["undo_id"]
    pos = {p["instrument"]: Decimal(p["quantity"]) for p in c.get("/api/ledger/positions").json()}
    assert pos["Sampo Oyj A"] == 120 and pos["Cash EUR"] == Decimal("412.87")
    h = c.get("/api/portfolio/holdings", params={"on": "2026-09-30", "scope": f"entity:{me['entity_id']}"}).json()
    vals = {r_["isin"]: r_ for acc in h["accounts"] for r_ in acc["holdings"] if r_["isin"]}
    assert Decimal(vals["FI4000552500"]["value"]["value"]) == Decimal("4941.60")  # statement price used
    assert Decimal(vals["FI4000552500"]["cost"]["value"]) == Decimal("4210.00")
    # undo voids the bookings; reading the same statement in compare mode then shows the differences
    assert c.post(f"/api/undo/{a['undo_id']}").status_code == 204
    assert "Sampo Oyj A" not in {p["instrument"] for p in c.get("/api/ledger/positions").json()}
    cmp_ = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"mode": "compare"}).json()
    assert cmp_["applied"]["differences"] == 4


def test_value_only_for_insurance_style_statements(
    auth_client: TestClient, db: Session, gateway: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path))
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    ps = c.post(
        "/api/accounts",
        json={"name": "PS-sopimus", "kind": "ps", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    funds = [
        ["Fund A", None, None, None, None, 12000.0, "EUR", None],
        ["Fund B", None, None, None, None, 3000.5, "EUR", None],
    ]
    c.post(
        "/api/ai/attachments",
        files=[("files", ("ps.png", image_bytes(900, 1200), "image/png"))],
        data={"account_id": str(ps["id"])},
    )
    read_attachments(JobContext(db=db), client=FakeGateway(rows=funds, total=15000.5))
    x = c.get("/api/ai/extractions").json()[0]
    assert x["data"]["rows"][0]["read_from"] == "image" and x["data"]["rows"][0]["in_text"] is None
    a = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"mode": "value", "account_id": ps["id"]}).json()
    assert a["applied"]["value"] == "15413.37"  # funds + 412,87 cash
    assert json.dumps(a)


def test_cash_listed_as_a_holding_is_moved_to_cash(db: Session, gateway: None) -> None:
    units = attachments.load("scan.png", image_bytes(900, 1200))
    rows = [*ROWS, ["Katelinen", None, None, None, "EUR", 412.87, "EUR", None]]  # OCR-ish misreading of Käteinen
    res = statements.holdings(db, units, {"as_of": "2026-09-30"}, FakeGateway(rows=rows))
    assert len(res.rows) == 3 and res.checks[0]["ok"] is True


def test_misread_last_digit_is_corrected_from_the_page(db: Session, gateway: None) -> None:
    units = attachments.load("raportti.pdf", text_pdf(STATEMENT))
    rows = [list(r) for r in ROWS]
    rows[0][7] = 4289.7  # a dropped last digit of 4 289,75
    res = statements.holdings(db, units, {"as_of": "2026-09-30"}, FakeGateway(rows=rows))
    ishares = next(r for r in res.rows if r["isin"] == "IE00B4L5Y983")
    assert ishares["cost"] == Decimal("4289.75") and ishares["status"] == "ok" and "corrected" in ishares["fixed"]


def test_decimal_comma_units_and_split_cards(db: Session, gateway: None) -> None:
    units = attachments.load("app.png", image_bytes(900, 1200))
    rows = [
        ["OP-Maailma Indeksi", None, 1450000, 19.882, "EUR", 28828.9, "EUR", None],  # 1 450,000 read as 1450000
        ["Aktia Nordic", None, 402.112, 48.77, "EUR", None, "EUR", None],  # card cut by a tile edge
        ["Aktia Nordic", None, None, None, "EUR", 19611.0, "EUR", None],  # its value in the next tile
    ]
    res = statements.holdings(db, units, {"as_of": "2026-09-30"}, FakeGateway(rows=rows, total=48852.77))
    op = next(r for r in res.rows if r["name"] == "OP-Maailma Indeksi")
    assert op["quantity"] == Decimal("1450") and op["status"] == "ok" and "decimal comma" in op["fixed"]
    aktia = [r for r in res.rows if r["name"] == "Aktia Nordic"]
    assert len(aktia) == 1 and aktia[0]["value"] == Decimal("19611.0") and aktia[0]["quantity"] == Decimal("402.112")


def test_glued_columns_are_rebuilt_from_the_page(db: Session, gateway: None) -> None:
    units = attachments.load("raportti.pdf", text_pdf(STATEMENT))
    rows = [list(r) for r in ROWS]
    rows[1][2] = 120.4118  # "120 41,18" glued into one number
    res = statements.holdings(db, units, {"as_of": "2026-09-30"}, FakeGateway(rows=rows))
    sampo = next(r for r in res.rows if r["isin"] == "FI4000552500")
    assert sampo["quantity"] == Decimal("120") and sampo["status"] == "ok" and "quantity" in sampo["fixed"]


LOTS = (
    "Tulostuspäivä\tSalkku\tHankintapäivä\tInstrumentti\tTunnus\tISIN\tValuutta\tMäärä\t"
    "Hankintahinta (Noteerausvaluutta)\tHankintahinta (EUR)\tHankinta-arvo (Noteerausvaluutta)\tHankinta-arvo (EUR)\t"
    "Markkina-arvo (Noteerausvaluutta)\tMarkkina-arvo (EUR)\tTuotto-%\tTuotto-% (p.a)\n"
    "2026-10-11\t1234567\t2018-04-17\tHandelsbanken Europa Index\tHB\tSE0006789715\tEUR\t1,422\t11,25\t11,25\t16\t16\t"
    "32,839\t32,839\t105,24\t8,84\n"
    "2026-10-11\t1234567\t2021-06-01\tHandelsbanken Europa Index\tHB\tSE0006789715\tEUR\t10\t20\t20\t200\t200\t"
    "230,93\t230,93\t15,47\t3,1\n"
    "2026-10-11\t1234567\t2024-01-15\tSampo Oyj A\tSAMPO\tFI4000552500\tEUR\t50\t39\t39\t1 950\t1 950\t"
    "2 059\t2 059\t5,59\t2,0\n"
)


def test_nordnet_lots_export_is_parsed_exactly_and_booked_by_lot(
    auth_client: TestClient, db: Session, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, gateway: None
) -> None:
    from datetime import date

    from app.modules.portfolio import lots as lotmod

    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path))
    c = auth_client
    c.post("/api/persons", json={"name": "Alex", "is_self": True})
    content = b"\xff\xfe" + LOTS.encode("utf-16-le")
    assert (
        c.post("/api/ai/attachments", files=[("files", ("ostoerittain.csv", content, "text/csv"))]).status_code == 201
    )
    fake = FakeGateway()
    read_attachments(JobContext(db=db), client=fake)
    assert fake.calls == []  # no AI involved
    x = c.get("/api/ai/extractions").json()[0]
    assert x["status"] == "review" and x["account_id"] is None and len(x["data"]["rows"]) == 3
    assert x["data"]["rows"][0]["quantity"] == "1.422" and x["data"]["rows"][2]["cost"] == "1950"
    r = c.post(
        f"/api/ai/extractions/{x['id']}/accept",
        json={"mode": "book", "new_account": {"name": "Nordnet AOT", "kind": "aot"}},
    )
    assert r.status_code == 200, r.text
    acct = r.json()["account_id"]
    book = lotmod.build(db)
    hb = [(lot.acquired, lot.quantity, lot.cost) for (a, _), ls in book.open.items() if a == acct for lot in ls]
    assert (date(2018, 4, 17), Decimal("1.422"), Decimal("16")) in hb and (
        date(2024, 1, 15),
        Decimal("50"),
        Decimal("1950"),
    ) in hb
    accts = {a["id"]: a for a in c.get("/api/accounts").json()}
    assert accts[acct]["external_ref"] == "1234567" and accts[acct]["owners"]


def test_task_label_is_ascii(db: Session, gateway: None) -> None:
    from app.modules.ai import llm

    fake = FakeGateway()
    llm.complete(
        db,
        [{"role": "user", "content": "x"}],
        label="text lines 1–120 ä",
        client=fake,
        schema={"type": "object", "properties": {"kind": {"type": "string"}}},
    )
    assert fake.calls[0]["headers"]["X-Local-AI-Task-Label"].isascii()


def test_price_next_to_currency_decides_column_order(db: Session, gateway: None) -> None:
    line = "SPDR MSCI ACWI IMI UCITS ETF 11,89 EUR 9,10 1 000 11 890,00 +2 790,00 30,66%"
    units = attachments.load("raportti.pdf", text_pdf(["Omistukset 12345678 10.10.2026", line, "Likvidit varat 21,40"]))
    rows = [["SPDR MSCI ACWI IMI UCITS ETF", None, 11.89, 1000, "EUR", 11890.00, "EUR", None]]  # swapped by the model
    res = statements.holdings(db, units, {"as_of": "2026-10-10"}, FakeGateway(rows=rows))
    r = res.rows[0]
    assert (r["quantity"], r["price"], r["value"]) == (Decimal("1000"), Decimal("11.89"), Decimal("11890.00"))


OVERVIEW = ["Tilin arvo yhteensa", "1 542,30 EUR", "Markkina-arvo", "1 410,75 EUR", "Osakkeet"]
POSITIONS = [
    "Nimi Muutos Kurssi Omistus",
    "Sampo Oyj A +1,15% 8,82 EUR 211,68 EUR 24 kpl",
    "Osakesaastotili 131,55 EUR",
]


class StatementGateway(FakeGateway):
    """Calls the document a single balance and finds no date among its fields; the classifier did see one."""

    def request(self, method: str, url: str, json: Any = None, headers: Any = None) -> httpx.Response:  # noqa: A002
        self.calls.append({"url": url, "payload": json, "headers": headers})
        props = json["response_format"]["json_schema"]["schema"]["properties"]
        if "kind" in props:
            body: dict[str, Any] = {
                "kind": "statement",
                "institution": "Nordea",
                "account": None,
                "as_of": "2026-10-11",
                "language": "fi",
            }
        else:
            body = {"fields": {"balance": {"value": "1542.30", "quote": "1 542,30"}}}
        resp = {"model": "m", "choices": [{"message": {"content": __import__("json").dumps(body)}}], "usage": {}}
        return httpx.Response(200, text=__import__("json").dumps(resp), request=httpx.Request(method, url))


def test_classifier_sees_positions_after_an_overview_page(db: Session, gateway: None) -> None:
    units = attachments.load("Nordea OST.pdf", text_pdf(OVERVIEW, more=[POSITIONS]))
    fake = FakeGateway()
    statements.classify(db, units, fake)
    sent = fake.calls[0]["payload"]["messages"][1]["content"]
    assert "Tilin arvo yhteensa" in sent and "Sampo Oyj A" in sent  # page 2's positions are part of the question


def test_statement_date_comes_from_the_header_when_the_fields_miss_it(
    auth_client: TestClient, db: Session, gateway: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path))
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts",
        json={"name": "Nordea OST", "kind": "ost", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    pdf = text_pdf(OVERVIEW, more=[POSITIONS])
    assert c.post("/api/ai/attachments", files=[("files", ("ost.pdf", pdf, "application/pdf"))]).status_code == 201
    read_attachments(JobContext(db=db), client=StatementGateway())
    x = c.get("/api/ai/extractions").json()[0]
    assert x["kind"] == "statement" and x["fields"]["as_of"]["value"] == "2026-10-11"  # shown, so it can be fixed
    r = c.post(f"/api/ai/extractions/{x['id']}/accept", json={"account_id": acct["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["valuation_id"]


def test_app_view_rows_over_several_lines(db: Session, gateway: None) -> None:
    """Nordea's netbank print: each holding spread over lines (change %, change €, day %, price, value, kpl)."""
    page = [
        "Osakkeet",
        "Arvo yhteensa: 533,41 EUR",
        "Nimi Muutos Kurssi Omistus",
        " Aktia Bank Abp 17,99%",
        "11,60 EUR",
        "-0,47%",
        "12,68 EUR",
        "114,12 EUR",
        "9 kpl",
        " Pihlajalinna Oyj 9,76%",
        "11,33 EUR",
        "+1,53%",
        "10,62 EUR",
        "159,30 EUR",
        "15 kpl",
        " REVENIO SUBS - 2,22 EUR 6,66 EUR",
        "3 kpl",
        " Oma Saastopankki 30,17% 0,00% 162,40 EUR",
        " Wul\ufffd-Yhtiot Oyj 2,96%",  # the text layer lost the ff ligature
        "1,62 EUR",
        "4,33 EUR",
        "90,93 EUR",
        "21 kpl",
        "Kateinen 412,87 EUR",
    ]
    misread = [
        ["Aktia Bank Abp", None, 9, 11.6, "EUR", 104.4, "EUR", None],  # the change in euros taken as the price
        ["Pihlajalinna Oyj", None, 15, 11.33, "EUR", 169.95, "EUR", None],
        ["REVENIO SUBS", None, 3, -2.22, "EUR", -6.66, "EUR", None],  # the dash read as a minus
        ["Oma Saastopankki", None, None, None, "EUR", 162.4, "EUR", None],  # continues on the next page
        ["Wulff-Yhtiot Oyj", None, 21, 4.33, "EUR", 90.93, "EUR", None],
        ["Kone Oyj", "FI0009013403", 468, 368.85, "EUR", 172621.8, "EUR", None],  # the prompt's example, copied
    ]
    units = attachments.load("ost.pdf", text_pdf(page))
    res = statements.holdings(db, units, {"as_of": "2026-10-11"}, FakeGateway(rows=misread, total=533.41))
    got = {r["name"]: (r["quantity"], r["price"], r["value"], r["status"]) for r in res.rows}
    assert got["Aktia Bank Abp"] == (9, Decimal("12.68"), Decimal("114.12"), "ok")
    assert got["Pihlajalinna Oyj"] == (15, Decimal("10.62"), Decimal("159.30"), "ok")
    assert got["REVENIO SUBS"] == (3, Decimal("2.22"), Decimal("6.66"), "ok")
    assert got["Oma Saastopankki"][3] == "check"  # no quantity: shown for a look, not booked silently
    assert got["Wulff-Yhtiot Oyj"][3] == "ok"  # name broken on the page, but its value is printed: kept
    assert "Kone Oyj" not in got and any("Kone Oyj is not on the page" in n for n in res.notes)
    # the stated total is the holdings alone; cash is listed separately
    assert res.checks[0]["ok"] and "cash shown separately" in res.checks[0]["what"]


def test_statement_date_is_never_in_the_future(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    class Today(date):
        @classmethod
        def today(cls) -> "Today":
            return cls(2026, 10, 11)

    monkeypatch.setattr(statements, "date", Today)
    assert statements.statement_date("2026-11-10") == date(2026, 10, 11)  # print footer 10/11/26 is month/day
    assert statements.statement_date("2026-09-30") == date(2026, 9, 30)
    assert statements.statement_date("2026-12-25") is None  # cannot be right either way: asked for by hand
