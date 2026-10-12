"""Merging duplicate instruments and finding them."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.modules.core.models import Instrument, InstrumentProfile
from app.modules.ingest.models import SourceMapping
from app.modules.ledger.models import Posting
from app.modules.pricing.models import Distribution, Price
from app.platform.db import session_factory


def _world(c: TestClient) -> tuple[int, int, int]:
    """(account, statement-made 'Nokia Oyj' without ISIN, broker-made 'Nokia Oyj' with ISIN), 10 and 5 units held."""
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()["id"]
    c.post(
        "/api/ledger/transactions", json={"account_id": acct, "date": "2024-01-02", "kind": "deposit", "amount": "9000"}
    )
    a = c.post("/api/instruments", json={"type": "stock", "name": "Nokia Oyj", "currency": "EUR"}).json()["id"]
    b = c.post(
        "/api/instruments", json={"type": "stock", "name": "Nokia Oyj", "currency": "EUR", "isin": "FI0009000681"}
    ).json()["id"]
    for inst, qty in ((a, 10), (b, 5)):
        r = c.post(
            "/api/ledger/transactions",
            json={
                "account_id": acct,
                "date": "2024-01-03",
                "kind": "buy",
                "amount": str(-qty * 4),
                "instrument_id": inst,
                "quantity": str(qty),
            },
        )
        assert r.status_code == 201, r.text
    return acct, a, b


def _seed_rows(a: int, b: int) -> None:
    with session_factory()() as s:
        s.add_all(
            [
                Price(instrument_id=a, date=date(2024, 5, 1), close=Decimal("4.1"), currency="EUR", source="yahoo"),
                Price(instrument_id=a, date=date(2024, 5, 2), close=Decimal("4.2"), currency="EUR", source="yahoo"),
                Price(instrument_id=b, date=date(2024, 5, 2), close=Decimal("4.25"), currency="EUR", source="yahoo"),
                Price(instrument_id=b, date=date(2024, 5, 3), close=Decimal("4.3"), currency="EUR", source="yahoo"),
                InstrumentProfile(instrument_id=a, source="manual", data={"who": "a"}),
                InstrumentProfile(instrument_id=a, source="morningstar", data={"who": "a"}),
                InstrumentProfile(instrument_id=b, source="manual", data={"who": "b"}),
                Distribution(
                    instrument_id=a, ex_date=date(2024, 4, 1), amount=Decimal("0.1"), currency="EUR", source="yahoo"
                ),
                Distribution(
                    instrument_id=b, ex_date=date(2024, 4, 1), amount=Decimal("0.1"), currency="EUR", source="yahoo"
                ),
                SourceMapping(source="nordea", kind="instrument", key="NOKIA", value=str(a)),
            ]
        )
        s.commit()


def test_merge_moves_everything_and_resolves_conflicts(auth_client: TestClient) -> None:
    c = auth_client
    acct, a, b = _world(c)
    _seed_rows(a, b)
    with session_factory()() as s:
        ia = s.get(Instrument, a)
        assert ia is not None
        ia.meta = {"created_from": "statement", "isin_conflict": "same ISIN FI0009000681 as instrument #2 — merge them"}
        ia.symbol = "NOKIA"
        s.commit()
    r = c.post(f"/api/instruments/{a}/merge", json={"drop_id": b})
    assert r.status_code == 200, r.text
    moved = r.json()["moved"]
    assert moved["ledger_posting"] == 1  # the drop side's one asset posting
    assert moved["price"] == 1 and moved["price:dropped"] == 1  # 05-03 moves, 05-02 stays the keep side's
    assert moved["instrument_profile:dropped"] == 1 and moved["distribution:dropped"] == 1
    with session_factory()() as s:
        assert s.get(Instrument, b) is None
        keep = s.get(Instrument, a)
        assert keep is not None and keep.isin == "FI0009000681" and keep.symbol == "NOKIA"
        assert "isin_conflict" not in keep.meta and keep.meta["created_from"] == "statement"
        px = {p.date: p.close for p in s.scalars(select(Price).where(Price.instrument_id == a))}
        assert px == {
            date(2024, 5, 1): Decimal("4.1"),
            date(2024, 5, 2): Decimal("4.2"),
            date(2024, 5, 3): Decimal("4.3"),
        }
        prof = {
            p.source: p.data for p in s.scalars(select(InstrumentProfile).where(InstrumentProfile.instrument_id == a))
        }
        assert prof == {"manual": {"who": "a"}, "morningstar": {"who": "a"}}
        m = s.scalar(select(SourceMapping).where(SourceMapping.key == "NOKIA"))
        assert m is not None and m.value == str(a)
        assert s.scalar(select(func.count()).select_from(Posting).where(Posting.instrument_id == b)) == 0
    pos = [p for p in c.get("/api/ledger/positions").json() if p["instrument_id"] in (a, b)]
    assert [(p["instrument_id"], Decimal(p["quantity"])) for p in pos] == [(a, Decimal(15))]
    assert c.get("/api/instruments/duplicates").json() == []


def test_merge_into_the_one_with_the_isin(auth_client: TestClient) -> None:
    c = auth_client
    _, a, b = _world(c)
    r = c.post(f"/api/instruments/{b}/merge", json={"drop_id": a})
    assert r.status_code == 200
    [i] = [x for x in c.get("/api/instruments").json() if x["name"] == "Nokia Oyj"]
    assert i["id"] == b and i["isin"] == "FI0009000681"
    holdings = c.get("/api/portfolio/holdings").json()
    lines = [h for acc in holdings["accounts"] for h in acc["holdings"] if h["name"] == "Nokia Oyj"]
    assert len(lines) == 1 and Decimal(lines[0]["quantity"]) == 15


def test_merge_refuses_currency_mismatch_cash_and_self(auth_client: TestClient) -> None:
    c = auth_client
    _, a, b = _world(c)
    usd = c.post("/api/instruments", json={"type": "stock", "name": "Nokia Oyj", "currency": "USD"}).json()["id"]
    r = c.post(f"/api/instruments/{a}/merge", json={"drop_id": usd})
    assert r.status_code == 422 and "currencies" in r.json()["detail"]
    cash = c.post("/api/instruments", json={"type": "cash", "name": "Cash EUR", "currency": "EUR"}).json()["id"]
    assert c.post(f"/api/instruments/{a}/merge", json={"drop_id": cash}).status_code == 422
    assert c.post(f"/api/instruments/{a}/merge", json={"drop_id": a}).status_code == 422
    assert c.post(f"/api/instruments/{a}/merge", json={"drop_id": 9999}).status_code == 422
    x = c.post(
        "/api/instruments", json={"type": "stock", "name": "Sampo Oyj A", "currency": "EUR", "isin": "FI4000552500"}
    )
    y = c.post(
        "/api/instruments", json={"type": "stock", "name": "Sampo Oyj A", "currency": "EUR", "isin": "FI0009003305"}
    )
    r = c.post(f"/api/instruments/{x.json()['id']}/merge", json={"drop_id": y.json()["id"]})
    assert r.status_code == 422 and "ISINs" in r.json()["detail"]  # same name, two securities
    with session_factory()() as s:
        assert s.get(Instrument, b) is not None  # nothing was touched


def test_duplicates_finds_isin_conflict_name_and_secid(auth_client: TestClient) -> None:
    c = auth_client
    _, a, b = _world(c)  # same name

    def mk(name: str, **kw: str) -> int:
        body = {"type": "stock", "name": name, "currency": "EUR", **kw}
        return int(c.post("/api/instruments", json=body).json()["id"])

    x = mk("Sampo Oyj A", isin="FI4000552500")
    y = mk("Sampo plc")  # different name; linked by the price job's ISIN conflict note
    p = mk("Terveystalo Oyj")
    q = mk("Terveystalo Healthcare")  # linked by Morningstar id
    other = mk("Stora Enso Oyj R", isin="FI0009005961")
    other2 = mk("Stora Enso Oyj A")  # share class differs: not a duplicate
    with session_factory()() as s:
        iy, ip, iq = s.get(Instrument, y), s.get(Instrument, p), s.get(Instrument, q)
        assert iy and ip and iq
        iy.meta = {"isin_conflict": f"same ISIN FI4000552500 as instrument #{x} — merge them"}
        ip.meta, iq.meta = {"ms_secid": "0P0000A5XX"}, {"ms_secid": "0P0000A5XX"}
        s.commit()
    groups = c.get("/api/instruments/duplicates").json()
    by_ids = {frozenset(i["id"] for i in g["instruments"]): g for g in groups}
    assert set(by_ids) == {frozenset({a, b}), frozenset({x, y}), frozenset({p, q})}
    assert by_ids[frozenset({a, b})]["keep_id"] == b  # has the ISIN
    assert by_ids[frozenset({x, y})]["keep_id"] == x and "ISIN conflict" in by_ids[frozenset({x, y})]["reasons"][0]
    g = by_ids[frozenset({a, b})]
    rows: dict[int, Any] = {i["id"]: i for i in g["instruments"]}
    assert rows[a]["quantity"].startswith("10") and rows[a]["postings"] == 1 and rows[b]["isin"] == "FI0009000681"
    assert other not in {i["id"] for g in groups for i in g["instruments"]} and other2 not in rows
