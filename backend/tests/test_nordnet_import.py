"""Golden tests for the Nordnet adapter and the import pipeline (MVP acceptance criteria 1 and 2)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.ingest.adapters import nordnet

FIX = Path("/srv/fixtures/nordnet")
EXPECTED_QTY = {
    "IE00B4L5Y983": "55",
    "FI4000552500": "50",
    "FI4000415252": "40.123",
    "FI0009000681": "500",
    "FI0009013403": "30",
    "US67066G1040": "20",
}
EXPECTED_CASH = {"12345678": Decimal("1984.60"), "87654321": Decimal("3166.18")}


@pytest.mark.parametrize("name", ["transactions-fi.csv", "transactions-en.csv"])
def test_parse_variants(name: str) -> None:
    content = (FIX / name).read_bytes()
    assert nordnet.sniff(nordnet.decode(content))
    rows, meta = nordnet.parse(content)
    assert len(rows) == 23 and not [r for r in rows if r.error]
    by_id = {r.parsed["id"]: r.parsed for r in rows}
    assert by_id["1002"]["kind"] == "buy" and by_id["1002"]["quantity"] == "40"
    assert by_id["1007"]["quantity"] == "40.123"
    assert by_id["1010"]["cancelled"] is True
    assert by_id["1021"]["kind"] is None  # unknown type: never guessed
    assert by_id["1011"]["fx_rate"] == "0.9500"
    assert meta["language"] == ("fi" if name.endswith("fi.csv") else "en")


def test_header_currency_columns_attach_to_previous_field() -> None:
    f = nordnet.map_headers(["Id", "Kokonaiskulut", "Valuutta", "Summa", "Valuutta", "Saldo"])
    assert f == ["id", "fees", "fees_ccy", "amount", "amount_ccy", "balance"]


def _setup(c: TestClient) -> dict[str, int]:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    inst = c.post("/api/institutions", json={"name": "Nordnet", "kind": "broker"}).json()
    own = [{"entity_id": me["entity_id"], "share": "1"}]
    aot = c.post(
        "/api/accounts",
        json={
            "name": "Nordnet AOT",
            "kind": "aot",
            "institution_id": inst["id"],
            "external_ref": "12345678",
            "owners": own,
        },
    ).json()
    ost = c.post(
        "/api/accounts",
        json={
            "name": "Nordnet OST",
            "kind": "ost",
            "institution_id": inst["id"],
            "external_ref": "8765 4321",
            "owners": own,
        },
    ).json()
    return {"aot": aot["id"], "ost": ost["id"]}


def _upload(c: TestClient, name: str) -> dict:
    r = c.post("/api/imports", files={"file": (name, (FIX / name).read_bytes(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()


def test_full_import_flow(auth_client: TestClient) -> None:
    c = auth_client
    acc = _setup(c)
    b = _upload(c, "transactions-fi.csv")
    detail = c.get(f"/api/imports/{b['id']}").json()
    assert detail["counts"].get("ignored") == 1  # cancelled row
    assert detail["unknown_types"] == ["OMISTUSMUUTOS"]
    assert len(detail["missing_instruments"]) == 6

    created = c.post(f"/api/imports/{b['id']}/create-instruments").json()
    assert created["created"] == 6
    nvda = next(i for i in c.get("/api/instruments", params={"q": "nvidia"}).json())
    assert nvda["currency"] == "USD" and nvda["type"] == "stock"
    etf = next(i for i in c.get("/api/instruments", params={"q": "ishares"}).json())
    assert etf["type"] == "etf"

    # unknown type: tell the system it means nothing for the ledger
    c.post(f"/api/imports/{b['id']}/map-type", json={"raw_type": "OMISTUSMUUTOS", "kind": "ignore"})
    detail = c.get(f"/api/imports/{b['id']}").json()
    assert detail["counts"] == {"new": 21, "ignored": 2}

    res = c.post(f"/api/imports/{b['id']}/commit").json()
    assert res["committed"] == 21 and res["errors"] == 0
    assert all(r["ok"] for r in res["reconciliation"].values()), res["reconciliation"]
    assert all(r["row_mismatches"] == 0 for r in res["reconciliation"].values())

    pos = c.get("/api/ledger/positions").json()
    insts = {i["id"]: i for i in c.get("/api/instruments").json()}
    got = {insts[p["instrument_id"]]["isin"]: p["quantity"] for p in pos if insts[p["instrument_id"]]["isin"]}
    assert {k: Decimal(v) for k, v in got.items()} == {k: Decimal(v) for k, v in EXPECTED_QTY.items()}
    cash = {p["account_id"]: Decimal(p["quantity"]) for p in pos if insts[p["instrument_id"]]["type"] == "cash"}
    assert cash == {acc["aot"]: EXPECTED_CASH["12345678"], acc["ost"]: EXPECTED_CASH["87654321"]}

    recon = c.get("/api/reconciliation").json()
    assert recon and all(r["ok"] for r in recon)

    # provenance: position -> postings -> source row
    tree = c.get(f"/api/explain/pos:{acc['aot']}:{etf['id']}").json()
    assert tree["value"] == "55" and len(tree["children"]) == 3
    raw_ref = tree["children"][0]["children"][0]["ref"]
    raw = c.get(f"/api/explain/{raw_ref}").json()
    assert raw["label"].startswith("Row ") and "transactions-fi.csv" in raw["label"]

    # re-import: everything is a duplicate, nothing new
    b2 = _upload(c, "transactions-en.csv")
    d2 = c.get(f"/api/imports/{b2['id']}").json()
    assert d2["counts"].get("new", 0) == 0
    assert d2["counts"]["duplicate"] == 21

    # undo the first import: positions disappear, rows can be committed again
    assert c.post(f"/api/undo/{res['undo_id']}").status_code == 204
    assert c.get("/api/ledger/positions").json() == []
    again = c.post(f"/api/imports/{b['id']}/commit").json()
    assert again["committed"] == 21


def test_unmapped_portfolio_needs_account(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()
    b = _upload(c, "transactions-en.csv")
    d = c.get(f"/api/imports/{b['id']}").json()
    assert set(d["unmapped_portfolios"]) == {"12345678", "87654321"}
    c.post(f"/api/imports/{b['id']}/map-account", json={"portfolio": "12345678", "account_id": a["id"]})
    d = c.get(f"/api/imports/{b['id']}").json()
    assert d["unmapped_portfolios"] == ["87654321"]
    assert c.get(f"/api/accounts/{a['id']}").json()["external_ref"] == "12345678"


def test_not_a_nordnet_file(auth_client: TestClient) -> None:
    r = auth_client.post("/api/imports", files={"file": ("x.csv", b"a,b,c\n1,2,3\n", "text/csv")})
    assert r.status_code == 422 and "not recognised" in r.json()["detail"]


def test_manual_transaction_and_undo(auth_client: TestClient) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts",
        json={"name": "Svea", "kind": "savings", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    t = c.post(
        "/api/ledger/transactions",
        json={"account_id": a["id"], "date": "2026-01-02", "kind": "deposit", "amount": "9400"},
    ).json()
    assert sum(Decimal(p["amount"]) for p in t["postings"]) == 0
    assert c.get("/api/ledger/positions").json()[0]["quantity"] == "9400.00000000"
    c.post(f"/api/undo/{t['undo_id']}")
    assert c.get("/api/ledger/positions").json() == []


CORPORATE_ACTIONS = [
    # Id, date, type, security, ISIN, quantity, price, amount, purchase value (Nordnet's row types; amounts invented)
    ("100", "2025-01-05", "TALLETUS", "", "", "", "", "30000", ""),
    ("101", "2025-01-10", "OSTO", "Amundi MSCI World V UCITS ET", "LU1781541179", "200", "13,5", "-2700", "2700"),
    ("102", "2025-02-24", "ETF_KK SÄÄSTÖN PALVELUMAKSU", "", "", "", "", "-2,5", ""),
    ("103", "2025-02-27", "VAIHTO AP-OTTO", "Amundi MSCI World V UCITS ET", "LU1781541179", "200", "0", "0", ""),
    (
        "104",
        "2025-02-27",
        "VAIHTO AP-JÄTTÖ",
        "Amundi Core MSCI World ETF A",
        "IE000BI8OT95",
        "29,3451",
        "0",
        "0",
        "2730,40",
    ),
    (
        "105",
        "2025-03-04",
        "DESIM KIRJAUS OTTO",
        "Amundi Core MSCI World ETF A",
        "IE000BI8OT95",
        "0,3451",
        "129,47",
        "44,68",
        "",
    ),
    (
        "106",
        "2025-03-04",
        "DESIM. ULOSKIRJ. KÄT",
        "Amundi Core MSCI World ETF A",
        "IE000BI8OT95",
        "0,3451",
        "129,47",
        "44,68",
        "",
    ),
    ("107", "2025-06-02", "OSTO", "SPDR MSCI ACWI IMI UCITS ETF", "IE00B3YLTY66", "40", "250", "-10000", "10000"),
    ("108", "2026-02-23", "SPLIT AP OTTO", "SPDR MSCI ACWI IMI UCITS ETF", "IE00B3YLTY66", "40", "0", "0", ""),
    ("109", "2026-02-23", "SPLIT AP JÄTTÖ", "SPDR MSCI ACWI IMI UCITS ETF", "IE00B3YLTY66", "1000", "0", "0", ""),
    ("110", "2026-03-31", "LAINAKORKO", "", "", "", "", "-0,44", ""),
]


def _nordnet_csv(rows: list[tuple[str, ...]], portfolio: str) -> bytes:
    head = nordnet.decode((FIX / "transactions-fi.csv").read_bytes()).splitlines()[0]
    lines = [head]
    for rid, d, typ, sec, isin, q, p, amt, pv in rows:
        cols = [rid, d, d, d, portfolio, typ, sec, isin, q, p, "", "0", "EUR", amt, "EUR", pv, "EUR" if pv else ""]
        lines.append("\t".join(cols + [""] * 5 + ["", "", "", f"V{rid}", "0", "EUR"]))
    return ("\n".join(lines) + "\n").encode("utf-16")


def test_nordnet_corporate_actions_and_statement_bookings_replaced(auth_client: TestClient, db: Session) -> None:
    from app.modules.ledger.models import Transaction
    from app.modules.ledger.service import TxnSpec, record
    from app.modules.portfolio import lots

    c = auth_client
    acc = _setup(c)
    # holdings booked earlier from a statement stand in for the history: the import must not add to them
    stmt = record(
        db,
        TxnSpec(
            account_id=acc["aot"],
            date=date(2026, 10, 1),
            kind="deposit",
            currency="EUR",
            amount=Decimal("100"),
            source="statement",
            external_id="stmt:1:0",
        ),
    )
    db.commit()
    r = c.post("/api/imports", files={"file": ("t.csv", _nordnet_csv(CORPORATE_ACTIONS, "12345678"), "text/csv")})
    b = r.json()
    detail = c.get(f"/api/imports/{b['id']}").json()
    assert detail["unknown_types"] == []  # all of Nordnet's corporate-action types are known
    c.post(f"/api/imports/{b['id']}/create-instruments")
    detail = c.get(f"/api/imports/{b['id']}").json()
    assert detail["counts"] == {"new": 10, "ignored": 1}  # the KÄT cash leg of the fraction sale
    assert detail["replaces_statements"] == [{"account_id": acc["aot"], "account": "Nordnet AOT", "entries": 1}]

    res = c.post(f"/api/imports/{b['id']}/commit").json()
    assert res["committed"] == 10 and res["errors"] == 0
    assert "Replaced 1 entries booked from statements" in " ".join(res["warnings"])
    db.expire_all()
    assert db.get(Transaction, stmt.id).voided_at is not None

    insts = {i["id"]: i for i in c.get("/api/instruments").json()}
    pos = {
        insts[p["instrument_id"]]["isin"] or "cash": Decimal(p["quantity"])
        for p in c.get("/api/ledger/positions").json()
    }
    assert pos["IE000BI8OT95"] == 29 and pos["IE00B3YLTY66"] == 1000 and "LU1781541179" not in pos
    # 30000 − 2700 − 2,50 fee + 44,68 fraction sold − 10000 − 0,44 loan interest
    assert pos["cash"] == Decimal("17341.74")
    by_isin = {i["isin"]: i["id"] for i in insts.values() if i["isin"]}
    book = lots.build(db, [acc["aot"]])
    assert book.cost_basis(acc["aot"], by_isin["IE00B3YLTY66"]) == Decimal("10000")  # the split keeps the cost
    mwre = book.cost_basis(acc["aot"], by_isin["IE000BI8OT95"])
    assert abs(mwre - Decimal("2730.40") * 29 / Decimal("29.3451")) < Decimal("0.01")  # exchange carries the cost

    # undoing the import brings the statement bookings back
    assert c.post(f"/api/undo/{res['undo_id']}").status_code == 204
    db.expire_all()
    assert db.get(Transaction, stmt.id).voided_at is None


def test_instrument_added_meanwhile_is_reused(auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select

    from app.modules.core.models import Instrument
    from app.platform.db import session_factory

    c = auth_client
    _setup(c)
    b = c.post("/api/imports", files={"file": ("t.csv", (FIX / "transactions-fi.csv").read_bytes(), "text/csv")}).json()
    isins = {
        "iShares Core MSCI World": "IE00B4L5Y983",
        "Sampo": "FI4000552500",
        "Kone": "FI0009013403",
        "Nordnet Indeksirahasto Suomi": "FI4000415252",
        "Nokia": "FI0009000681",
        "NVIDIA": "US67066G1040",
    }
    real, added = nordnet.guess_instrument_type, []

    def racing(name: str) -> str:
        if not added:  # another process (the benchmarks job) commits the same ISIN between lookup and insert
            isin = next(i for n, i in isins.items() if name.startswith(n))
            with session_factory()() as other:
                other.add(Instrument(type="etf", name="added meanwhile", isin=isin, currency="EUR"))
                other.commit()
            added.append(isin)
        return real(name)

    monkeypatch.setattr(nordnet, "guess_instrument_type", racing)
    assert c.post(f"/api/imports/{b['id']}/create-instruments").status_code == 200
    with session_factory()() as db:
        same = db.scalars(select(Instrument).where(Instrument.isin == added[0])).all()
    assert [i.name for i in same] == ["added meanwhile"]
