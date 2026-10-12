"""Instrument profiles and the X-ray: Morningstar parsing, per-field precedence, look-through totals, the job."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument, InstrumentProfile
from app.modules.portfolio import profiles
from app.modules.portfolio.allocation import allocation, rebalance, xray
from app.modules.portfolio.jobs import refresh_profiles
from app.modules.portfolio.valuation import Scope
from app.modules.pricing.providers import morningstar
from app.platform.db import today
from app.platform.jobs import JobContext
from app.platform.models import AuditLog
from tests.test_pricing import FakeClient

FIX = Path("/srv/fixtures/morningstar")


def snap(name: str) -> list[dict[str, Any]]:
    return json.loads((FIX / f"snapshot_{name}.json").read_text())  # type: ignore[no-any-return]


def near(a: Any, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - b) <= tol


# ---- parsing -------------------------------------------------------------------------------------


def test_parse_world_etf() -> None:
    as_of, d = profiles.parse_morningstar(snap("world_etf")[0])
    assert as_of == date(2026, 10, 8)
    assert near(d["ter"], 0.002)  # OngoingCharge 0.2 is percent
    assert near(d["asset_classes"]["equity"], 0.995170, 1e-5) and d["asset_classes"]["bond"] == 0
    assert near(d["regions"]["United States"], 0.734343, 1e-5) and near(d["regions"]["Japan"], 0.058402, 1e-5)
    assert near(d["countries"]["USA"], d["regions"]["United States"], 1e-3)  # net entry, equity sleeve
    assert near(d["market"]["developed"], 0.997268, 1e-5)
    assert near(sum(d["sectors"].values()), 0.995, 0.01) and "Technology" in d["sectors"]
    assert near(d["cap_size"]["giant"], 0.468991, 1e-5)
    assert d["style_box"] == "Large Blend"
    assert "bond" not in d and "credit" not in d


def test_parse_bond_etf() -> None:
    as_of, d = profiles.parse_morningstar(snap("bond_etf")[0])
    assert as_of == date(2026, 10, 7)
    assert near(d["ter"], 0.001)
    assert near(d["asset_classes"]["bond"], 0.991118, 1e-5) and d["asset_classes"]["equity"] == 0
    assert d["bond"]["credit_quality"] == "BBB"
    assert near(d["bond"]["modified_duration"], 6.0546, 1e-4)  # years, not a percentage
    assert near(d["bond"]["ytm"], 0.041)
    assert near(d["credit"]["AA"], 0.383889, 1e-5) and "regions" not in d
    assert d["bond_countries"]["AUS"] > 0 and "countries" not in d


def test_parse_fund_and_fetch_through_client() -> None:
    secid = "F00000XXXX"
    client = FakeClient({f"{morningstar.BASE}/security_details/{secid}": snap("fund")})
    as_of, d = profiles.fetch_morningstar(secid, client)  # type: ignore[arg-type]
    assert as_of == date(2026, 8, 31) and near(d["ter"], 0.003)
    assert near(d["asset_classes"]["equity"], 0.996871, 1e-5)
    assert "viewId=ETFsnapshot" not in client.calls[0]  # parameters travel separately


def test_derived_from_isin() -> None:
    fi = Instrument(type="stock", name="Sampo", isin="FI4000552500", currency="EUR")
    assert profiles.derived(fi) == {
        "asset_classes": {"equity": 1.0},
        "ter": 0.0,
        "countries": {"FIN": 1.0},
        "regions": {"Eurozone": 1.0},
        "market": {"developed": 1.0},
    }
    odd = profiles.derived(Instrument(type="stock", name="X", isin="ZZ0000000000", currency="EUR"))
    assert "countries" not in odd and odd["regions"] == {"Other regions": 1.0}
    assert "countries" not in profiles.derived(Instrument(type="stock", name="Rights", isin=None, currency="EUR"))
    assert profiles.derived(Instrument(type="etf", name="F", isin=None, currency="EUR")) == {}
    assert profiles.derived(Instrument(type="crypto", name="BTC", isin=None, currency="EUR"))["asset_classes"] == {
        "crypto": 1.0
    }


def test_stock_facts_bands() -> None:
    row = {"SectorName": "Financial Services", "MarketCap": 23_215_515_592, "EquityStyleBox": 1}
    d = profiles.parse_stock_facts(row)
    assert d == {
        "sectors": {"Financial Services": 1.0},
        "cap_size": {"large": 1.0},
        "style": {"Large Value": 1.0},
        "style_box": "Large Value",
    }
    assert profiles.parse_stock_facts({"MarketCap": 60e9})["cap_size"] == {"giant": 1.0}
    assert profiles.parse_stock_facts({"MarketCap": 1e8})["cap_size"] == {"micro": 1.0}


# ---- precedence ------------------------------------------------------------------------------------


def _fund(db: Session, **kw: Any) -> Instrument:
    inst = Instrument(**{"type": "etf", "name": "World ETF", "currency": "EUR", "isin": "IE00B4L5Y983", **kw})
    db.add(inst)
    db.flush()
    return inst


def test_effective_precedence_is_per_field(db: Session) -> None:
    inst = _fund(db)
    as_of, data = profiles.parse_morningstar(snap("world_etf")[0])
    profiles.save(db, inst.id, "morningstar", as_of, data)
    profiles.set_manual(db, inst, profiles.clean_manual({"ter": 0.0035}))
    eff, src = profiles.effective(db, inst)
    assert eff["ter"] == 0.0035 and src["ter"]["source"] == "manual"
    assert eff["regions"] == data["regions"] and src["regions"] == {"source": "morningstar", "as_of": "2026-10-08"}
    # removing the override (null) hands the field back to Morningstar
    profiles.set_manual(db, inst, profiles.clean_manual({"ter": None}))
    eff, src = profiles.effective(db, inst)
    assert near(eff["ter"], 0.002) and src["ter"]["source"] == "morningstar"
    assert db.scalar(select(InstrumentProfile).where(InstrumentProfile.source == "manual")) is None


def test_factsheet_beats_derived_but_not_morningstar(db: Session) -> None:
    inst = _fund(db, type="stock", name="Sampo", isin="FI4000552500")
    profiles.save(db, inst.id, "factsheet", None, {"sectors": {"Utilities": 1.0}, "ter": 0.01})
    profiles.save(db, inst.id, "morningstar", None, {"sectors": {"Financial Services": 1.0}})
    eff, src = profiles.effective(db, inst)
    assert eff["sectors"] == {"Financial Services": 1.0} and eff["ter"] == 0.01 and src["ter"]["source"] == "factsheet"
    assert src["countries"]["source"] == "derived"  # live, before any refresh


def test_manual_validation() -> None:
    for bad in ({"ter": 2}, {"nope": 1}, {"asset_classes": {"equity": 0.8, "bond": 0.4}}, {"asset_classes": {"x": 1}}):
        try:
            profiles.clean_manual(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ---- X-ray -----------------------------------------------------------------------------------------


def _buy(c: TestClient, acct: int, inst: int, qty: int, price: int) -> None:
    r = c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct,
            "date": "2024-01-03",
            "kind": "buy",
            "amount": str(-qty * price),
            "instrument_id": inst,
            "quantity": str(qty),
        },
    )
    assert r.status_code < 300, r.text
    c.post("/api/prices", json={"instrument_id": inst, "date": "2024-01-03", "close": str(price)})


def _portfolio(c: TestClient, db: Session) -> dict[str, int]:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts",
        json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()["id"]
    c.post(
        "/api/ledger/transactions",
        json={"account_id": acct, "date": "2024-01-02", "kind": "deposit", "amount": "10000"},
    )

    def mk(**kw: Any) -> int:
        return int(c.post("/api/instruments", json={"currency": "EUR", **kw}).json()["id"])

    ids = {
        "world": mk(type="etf", name="World ETF", isin="IE00B4L5Y983"),
        "bond": mk(type="etf", name="Aggregate Bond ETF", isin="IE00B3DKXQ41"),
        "mixed": mk(type="fund", name="Balanced fund", isin="FI0000000001"),
        "stock": mk(type="stock", name="Sampo", isin="FI4000552500"),
        "mystery": mk(type="etf", name="Mystery ETF", isin="IE0000000002", asset_class="equity"),
    }
    _buy(c, acct, ids["world"], 10, 100)
    _buy(c, acct, ids["bond"], 20, 50)
    _buy(c, acct, ids["mixed"], 10, 100)
    _buy(c, acct, ids["stock"], 10, 40)
    _buy(c, acct, ids["mystery"], 1, 100)
    for key, name in (("world", "world_etf"), ("bond", "bond_etf")):
        as_of, data = profiles.parse_morningstar(snap(name)[0])
        profiles.save(db, ids[key], "morningstar", as_of, data)
    mixed = db.get(Instrument, ids["mixed"])
    assert mixed
    profiles.set_manual(
        db, mixed, {"asset_classes": {"equity": 0.6, "bond": 0.4}, "ter": 0.005, "bond": {"modified_duration": 4.0}}
    )
    db.commit()
    return ids


def test_xray_buckets_sum_to_total_in_every_dimension(auth_client: TestClient, db: Session) -> None:
    _portfolio(auth_client, db)
    x = xray(db, Scope(None), today())
    assert x["total"] == Decimal(10000)
    for dim, items in x["dimensions"].items():
        assert abs(sum((i["value"] for i in items), Decimal(0)) - x["total"]) < Decimal("0.000001"), dim
    ac = {i["key"]: i["value"] for i in x["dimensions"]["asset_class"]}
    assert set(ac) == {"Equity", "Fixed income", "Cash", "Other"}
    assert ac["Cash"] > Decimal(6400)  # the account's cash, plus a sliver inside the ETFs
    # bond ETF 1000 x 99.1 % + 40 % of the balanced fund
    assert abs(ac["Fixed income"] - Decimal("1391.118")) < Decimal("0.5")
    region = {i["key"]: i["value"] for i in x["dimensions"]["region"]}
    assert region["Unknown"] > 0 and "Cash" in region  # the mystery ETF and the balanced fund say nothing of regions
    assert x["dimensions"]["sector"][-1]["key"] == "Not applicable"
    countries = {i["key"]: i for i in x["dimensions"]["country"]}
    assert countries["FIN"]["label"] == "Finland" and countries["FIN"]["value"] >= Decimal(400)
    assert x["dimensions"]["credit"][0]["key"] in ("AA", "A", "AAA")  # bond sleeve ranked first, not Not applicable


def test_xray_ter_bonds_and_coverage(auth_client: TestClient, db: Session) -> None:
    ids = _portfolio(auth_client, db)
    x = xray(db, Scope(None), today())
    # 1000 x 0.2 % + 1000 x 0.1 % + 1000 x 0.5 % (stocks and cash cost nothing); the mystery ETF is unknown
    assert abs(x["ter"]["eur_per_year"] - Decimal(8)) < Decimal("0.000001")
    assert near(x["ter"]["fraction"], 8 / 9900, 1e-9)
    b = x["bond"]
    assert near(b["modified_duration"], (991.118 * 6.0546 + 400 * 4.0) / (991.118 + 400), 1e-3)
    # Morningstar's average quality of the bond ETF, not its largest rating slice (AA)
    assert near(b["ytm"], 0.041) and b["credit_quality"] == "BBB"
    assert near(x["coverage"], 0.99)  # everything but the 100 EUR mystery ETF
    rows = {r["instrument_id"]: r for r in x["holdings"]}
    assert not rows[ids["mystery"]]["covered"] and rows[ids["mystery"]]["sources"] == []
    mixed = rows[ids["mixed"]]
    assert mixed["sources"][0]["source"] == "manual" and set(mixed["sources"][0]["fields"]) >= {"ter", "asset_classes"}
    world = rows[ids["world"]]
    assert world["top_region"] == "United States" and world["sources"][0] == {
        "source": "morningstar",
        "as_of": "2026-10-08",
        "fields": world["sources"][0]["fields"],
    }


def test_look_through_in_allocation_and_rebalance(auth_client: TestClient, db: Session) -> None:
    ids = _portfolio(auth_client, db)
    cur = allocation(db, Scope(None), "asset_class", today())
    assert sum(cur.values(), Decimal(0)) == Decimal(10000)
    bond_only = rebalance(db, Scope(None), {"Fixed income": Decimal(1)}, today())
    assert abs(next(r for r in bond_only["rows"] if r["class"] == "Fixed income")["value"] - Decimal("1391.118")) < 1
    # equity is overweight against a 10 % target: the balanced fund (dominant class Equity) is a sell candidate
    r = rebalance(
        db, Scope(None), {"Equity": Decimal("0.1"), "Fixed income": Decimal("0.1"), "Cash": Decimal("0.8")}, today()
    )
    sold = {t["instrument"] for t in r["trades"] if t["action"] == "sell" and t["class"] == "Equity"}
    assert "Balanced fund" in sold and "Aggregate Bond ETF" not in sold
    mixed = db.get(Instrument, ids["mixed"])
    assert mixed
    from app.modules.portfolio.allocation import classify

    data = profiles.effective(db, mixed)[0]
    assert classify(mixed, "asset_class", data) == "Equity"


def test_sixty_forty_fund_splits_allocation(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    acct = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()["id"]
    c.post(
        "/api/ledger/transactions", json={"account_id": acct, "date": "2024-01-02", "kind": "deposit", "amount": "1000"}
    )
    fund = c.post("/api/instruments", json={"type": "fund", "name": "60/40", "currency": "EUR"}).json()["id"]
    _buy(c, acct, fund, 10, 100)
    inst = db.get(Instrument, fund)
    assert inst
    profiles.save(db, fund, "factsheet", None, {"asset_classes": {"equity": 0.6, "bond": 0.4}})
    db.commit()
    assert allocation(db, Scope(None), "asset_class", today()) == {
        "Equity": Decimal("600"),
        "Fixed income": Decimal("400"),
    }
    assert c.get("/api/portfolio/allocation").json()["items"][0] == {"key": "Equity", "value": "600.00", "weight": 0.6}
    reb = c.get("/api/portfolio/rebalance").json()
    values = {r["class"]: r["value"] for r in reb["rows"]}
    assert values["Equity"] == "600.00" and values["Fixed income"] == "400.00" and values["Cash"] == "0.00"


# ---- endpoints -------------------------------------------------------------------------------------


def test_xray_endpoint_and_profile_roundtrip(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    ids = _portfolio(c, db)
    r = c.get("/api/portfolio/xray").json()
    assert r["total"] == "10000.00" and r["ter"]["eur_per_year"] == "8.00"
    assert [d for d in r["dimensions"]] == ["asset_class", "region", "country", "sector", "size", "style", "credit"]
    p = c.get(f"/api/instruments/{ids['world']}/profile").json()
    assert p["sources"]["ter"]["source"] == "morningstar" and near(p["data"]["ter"], 0.002)
    assert [s["source"] for s in p["stored"]] == ["morningstar"]
    # a hand edit wins and shows in the X-ray and the costs report
    put = c.put(f"/api/instruments/{ids['world']}/profile", json={"data": {"ter": 0.01}})
    assert put.status_code == 200 and put.json()["sources"]["ter"]["source"] == "manual"
    assert c.get("/api/portfolio/xray").json()["ter"]["eur_per_year"] == "16.00"
    costs = c.get("/api/portfolio/costs").json()
    assert costs["fund_costs_estimate"] == "16.00"
    assert (
        c.put(f"/api/instruments/{ids['world']}/profile", json={"data": {"ter": None}}).json()["sources"]["ter"][
            "source"
        ]
        == "morningstar"
    )
    assert c.put(f"/api/instruments/{ids['world']}/profile", json={"data": {"ter": 5}}).status_code == 422
    assert c.put("/api/instruments/9999/profile", json={"data": {}}).status_code == 404
    assert c.get("/api/instruments/9999/profile").status_code == 404
    db.expire_all()
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert "instrument.profile" in actions


# ---- the job -----------------------------------------------------------------------------------------


class FlakyClient(FakeClient):
    """Fails the screener lookup for one ISIN, like a provider hiccup."""

    def get(self, url: str, **kw: Any) -> httpx.Response:
        if kw.get("params", {}).get("term") == "FI0000FAIL01":
            raise httpx.ConnectError("boom")
        return super().get(url, **kw)


def test_job_writes_profiles_and_survives_a_failure(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    ids = _portfolio(c, db)
    bad = c.post(
        "/api/instruments", json={"type": "stock", "name": "Flaky Oyj", "currency": "EUR", "isin": "FI0000FAIL01"}
    ).json()["id"]
    acct = c.get("/api/accounts").json()[0]["id"]
    _buy(c, acct, bad, 1, 10)
    world = db.get(Instrument, ids["world"])
    assert world
    world.meta = {"ms_secid": "0P0000LZYG"}
    db.commit()
    client = FakeClient(
        {
            f"{morningstar.BASE}/security_details/0P0000LZYG": snap("world_etf"),
            morningstar.SCREENER: {
                "rows": [
                    {"Isin": "FI4000552500", "SectorName": "Financial Services", "MarketCap": 23e9, "EquityStyleBox": 1}
                ]
            },
        }
    )
    ctx = JobContext(db=db)
    stats = refresh_profiles(ctx, FlakyClient(client.routes))  # type: ignore[arg-type]
    db.commit()
    assert stats["failed"] == 1 and ctx.status == "partial"
    assert any("Flaky Oyj" in line for line in ctx.lines)
    rows = {(r.instrument_id, r.source): r for r in db.scalars(select(InstrumentProfile))}
    assert rows[(ids["stock"], "derived")].data["countries"] == {"FIN": 1.0}
    assert rows[(ids["stock"], "morningstar")].data["cap_size"] == {"large": 1.0}
    assert near(rows[(ids["world"], "morningstar")].data["ter"], 0.002)
    assert (bad, "derived") not in rows  # its savepoint rolled back; the others kept going
    assert db.get(Instrument, ids["world"]).asset_class == "equity"  # type: ignore[union-attr]
    # the instrument without a Morningstar id and no search result is reported, not fatal
    assert any("Mystery ETF" in line for line in ctx.lines)


def test_job_corrects_a_fund_booked_as_a_share(auth_client: TestClient, db: Session) -> None:
    """Statements create index funds as shares; the X-ray must not count them as one Swedish company."""
    c = auth_client
    _portfolio(c, db)
    fund = c.post(
        "/api/instruments",
        json={"type": "stock", "name": "Handelsbanken USA Index (A1 EUR)", "currency": "EUR", "isin": "SE0006800140"},
    ).json()["id"]
    _buy(c, c.get("/api/accounts").json()[0]["id"], fund, 10, 100)
    row = {"SecId": "F00000VD4I", "Name": "Handelsbanken USA Index (A1 EUR)", "Isin": "SE0006800140"}
    client = FakeClient(
        {
            morningstar.SCREENER: {"rows": [{**row, "PriceCurrency": "EUR", "holdingTypeId": 2}]},
            f"{morningstar.BASE}/security_details/F00000VD4I": snap("world_etf"),
        }
    )
    refresh_profiles(JobContext(db=db), client)  # type: ignore[arg-type]
    db.commit()
    inst = db.get(Instrument, fund)
    assert inst and inst.type == "fund" and inst.meta["ms_secid"] == "F00000VD4I"
    data = profiles.effective_many(db, [inst])[fund].data
    assert data["countries"]["USA"] > 0.7 and data["countries"].get("SWE", 0) < 0.05  # its holdings, not its domicile
