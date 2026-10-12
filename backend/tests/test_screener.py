"""ETF screener: universe refresh from canned Morningstar rows, filter/sort API, candidates."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument, InstrumentProfile
from app.modules.pricing.models import Price
from app.modules.pricing.providers import morningstar
from app.modules.pricing.service import held_instruments
from app.modules.screener import jobs, nordnet, service
from app.modules.screener.models import EtfListing
from app.platform.jobs import JobContext
from app.platform.models import JobRun
from tests.test_pricing import FakeClient, hold
from tests.test_profiles import snap

FIX = Path("/srv/fixtures/morningstar")
WORLD = "IE00B4L5Y983"  # iShares Core MSCI World (EUNL), in the fixture on XETR


def real_rows() -> list[dict[str, Any]]:
    return json.loads((FIX / "screener_etf.json").read_text())["rows"]  # type: ignore[no-any-return]


def extra_rows() -> list[dict[str, Any]]:
    return [
        # same ISIN on Stockholm in SEK: must lose to the XETR/EUR listing
        {
            "SecId": "0P0000MEHZ-SE",
            "Name": "iShares Core MSCI World ETF USD Acc",
            "Isin": WORLD,
            "ExchangeId": "EX$$$$XSTO",
            "Ticker": "EUNL",
            "PriceCurrency": "SEK",
            "holdingTypeId": 22,
            "OngoingCharge": 0.2,
            "ClosePrice": 1500,
        },
        # junk numbers, no Distribution field (name decides), a hedged class, a fund in the ETF universe, no ISIN
        {
            "SecId": "X1",
            "Name": "Acme Global Bond UCITS ETF EUR Hedged Dist",
            "Isin": "LU0000000001",
            "ExchangeId": "EX$$$$XHEL",
            "Ticker": "ACME",
            "PriceCurrency": "EUR",
            "holdingTypeId": 22,
            "OngoingCharge": "-",
            "FundTNAV": "n/a",
            "ReturnM12": 4.5,
            "AssetAllocEquity": 2.5,
            "EffectiveDuration": 6.2,
            "AverageCreditQuality": 5,
            "EquityStyleBox": 99,
            "InceptionDate": "not a date",
        },
        {"SecId": "X2", "Name": "Not an ETF", "Isin": "LU0000000002", "ExchangeId": "EX$$$$XETR", "holdingTypeId": 2},
        {"SecId": "X3", "Name": "No ISIN ETF", "ExchangeId": "EX$$$$XETR", "holdingTypeId": 22},
    ]


class ScreenerClient:
    """Serves the screener in pages like the real endpoint."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: list[dict[str, Any]] = []

    def get(self, url: str, **kw: Any) -> httpx.Response:
        p = kw["params"]
        self.params.append(p)
        size, page = p["pageSize"], p["page"]
        body = {"total": len(self.rows), "page": page, "rows": self.rows[(page - 1) * size : page * size]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def load(db: Session, rows: list[dict[str, Any]] | None = None) -> dict[str, int]:
    stats = service.refresh_universe(db, ScreenerClient(rows or real_rows() + extra_rows()))  # type: ignore[arg-type]
    db.commit()
    return stats


def test_refresh_groups_by_isin_and_picks_the_eur_listing(db: Session) -> None:
    c = ScreenerClient(real_rows() + extra_rows())
    stats = service.refresh_universe(db, c)  # type: ignore[arg-type]
    assert stats["rows"] == 9 and stats["etfs"] == 6 and stats["added"] == 6
    assert c.params[0]["filters"].startswith("ExchangeId:IN:EX$$$$XETR:EX$$$$XHEL:")
    world = db.scalar(select(EtfListing).where(EtfListing.isin == WORLD))
    assert world
    assert world.secid == "0P0000MEHZ" and world.close_currency == "EUR" and world.close_price == 131.64
    assert [x["exchange"] for x in world.listings] == ["XETR", "XSTO"]
    assert world.listings[1] == {"exchange": "XSTO", "ticker": "EUNL", "currency": "SEK", "secid": "0P0000MEHZ-SE"}
    # percents become fractions
    assert world.ter == 0.002 and world.returns["y1"] == 0.1956 and world.returns["y10"] == 0.1307
    assert abs((world.vol_3y or 0) - 0.1225275) < 1e-9 and abs((world.max_dd_3y or 0) + 0.2172589) < 1e-9
    assert world.equity_share is not None and abs(world.equity_share - 0.99517) < 1e-9
    assert world.fund_size_eur == 137006278881 and world.sharpe_3y == 1.24392
    assert (world.distribution, world.hedged, world.domicile, world.provider) == ("acc", False, "IRL", "iShares")
    assert world.style_box == "Large Blend" and str(world.inception) == "2009-09-25"
    assert world.benchmark == "MSCI World NR USD"


def test_refresh_infers_and_survives_junk(db: Session) -> None:
    load(db)
    rows = {e.isin: e for e in db.scalars(select(EtfListing))}
    assert "LU0000000002" not in rows and len(rows) == 6  # not an ETF; no-ISIN row skipped
    acme = rows["LU0000000001"]
    assert acme.distribution == "dist" and acme.hedged is True  # from the name
    assert acme.ter is None and acme.fund_size_eur is None and acme.inception is None and acme.style_box is None
    assert acme.returns == {"y1": 0.045} and acme.equity_share == 0.025
    assert acme.duration == 6.2 and acme.credit_quality == 5
    # "EUR H Dist" and the explicit Distribution field
    assert rows["IE00BKBF6H24"].hedged is True and rows["IE00BKBF6H24"].distribution == "dist"
    assert rows["IE000Y7V5EW2"].hedged is True and rows["IE000Y7V5EW2"].distribution == "acc"
    assert rows["IE00B5BMR087"].hedged is False


def test_refresh_pages_and_never_wipes_on_empty(db: Session) -> None:
    rows = [{**real_rows()[0], "Isin": f"IE{n:010d}", "SecId": f"S{n}"} for n in range(1, 2501)]
    c = ScreenerClient(rows)
    stats = service.refresh_universe(db, c)  # type: ignore[arg-type]
    assert stats["etfs"] == 2500 and [p["page"] for p in c.params] == [1, 2, 3]
    db.commit()
    # a provider answering with nothing keeps what we have
    again = service.refresh_universe(db, ScreenerClient([]))  # type: ignore[arg-type]
    assert again["removed"] == 0 and db.query(EtfListing).count() == 2500
    # a real refresh drops what vanished
    part = service.refresh_universe(db, ScreenerClient(rows[:10]))  # type: ignore[arg-type]
    assert part["removed"] == 2490 and db.query(EtfListing).count() == 10


def test_refresh_provider_error_keeps_data(db: Session) -> None:
    load(db)

    class Down:
        def get(self, url: str, **kw: Any) -> httpx.Response:
            return httpx.Response(503, request=httpx.Request("GET", url))

    try:
        service.refresh_universe(db, Down())  # type: ignore[arg-type]
        raise AssertionError("should raise")
    except httpx.HTTPStatusError:
        pass
    assert db.query(EtfListing).count() == 6


# ---- API ------------------------------------------------------------------------------------------


def test_list_filters_and_sorts(auth_client: TestClient, db: Session) -> None:
    load(db)
    c = auth_client

    def get(**p: Any) -> dict[str, Any]:
        r = c.get("/api/etfs", params=p)
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    everything = get()
    assert everything["total"] == 6 and everything["updated_at"]
    sizes = [r["fund_size_eur"] for r in everything["rows"]]
    assert sizes[:2] == [144230671451, 144230671451] and sizes[-1] is None  # nulls last, largest first
    assert everything["categories"][0]["count"] >= 1 and {"name": "iShares", "count": 4} in everything["providers"]
    assert get(q="eunl")["total"] == 1  # ticker, case-insensitive
    assert get(q="ie00b4l5y983")["total"] == 1
    assert get(q="msci world")["total"] == 2
    assert get(q="100%")["total"] == 0  # LIKE wildcards are literal
    assert {r["isin"] for r in get(max_ter=0.1)["rows"]} == {"IE00B5BMR087", "IE000Y7V5EW2", "IE00BFMXXD54"}
    assert get(min_size=140000)["total"] == 2
    assert get(distribution="dist")["total"] == 2
    assert get(hedged=True)["total"] == 3 and get(hedged=False)["total"] == 3
    assert get(equity_min=99)["total"] == 5 and get(equity_max=50)["total"] == 1
    assert get(domicile="irl")["total"] == 5
    assert get(min_years=10)["total"] == 2 and get(provider="iShares", category="Other Equity")["total"] == 2
    by_y1 = [r["isin"] for r in get(sort="y1")["rows"]]
    assert by_y1[0] == "IE00B5BMR087" and by_y1[-1] != "IE00B5BMR087"  # 20.7 % leads, nulls last
    assert get(sort="ter")["rows"][0]["ter"] == 0.0007  # cheapest first by default
    assert get(sort="name", desc=True)["rows"][0]["name"].startswith("iShares")
    assert len(get(limit=2, offset=4)["rows"]) == 2
    assert c.get("/api/etfs", params={"sort": "isin"}).status_code == 422


def test_detail_and_candidate_lifecycle(auth_client: TestClient, db: Session) -> None:
    load(db)
    c = auth_client
    d = c.get(f"/api/etfs/{WORLD}").json()
    assert d["profile"] is None and d["instrument_id"] is None and d["candidate_instrument_id"] is None
    assert len(d["listings"]) == 2 and c.get("/api/etfs/XX0000000000").status_code == 404

    r = c.post(f"/api/etfs/{WORLD}/candidate", json={})
    assert r.status_code == 200, r.text
    iid = r.json()["instrument_id"]
    inst = db.get(Instrument, iid)
    assert inst and (inst.type, inst.currency, inst.symbol, inst.mic) == ("etf", "EUR", "EUNL", "XETR")
    assert inst.meta == {
        "candidate": True,
        "ms_secid": "0P0000MEHZ",
        "ms_type": 22,
        "ms_universe": morningstar.ETF_UNIVERSE,
    }
    run = db.get(JobRun, r.json()["run_id"])
    assert run and run.job == "etfs.prepare" and run.status == "queued"
    assert [i.id for i in held_instruments(db)] == [iid]  # candidates are priced like holdings

    row = c.get("/api/etfs", params={"q": "EUNL"}).json()["rows"][0]
    assert row["candidate_instrument_id"] == iid and row["held"] is False
    cands = c.get("/api/etfs/candidates").json()
    assert [(x["id"], x["isin"], x["last_price"], x["profile_source"]) for x in cands] == [(iid, WORLD, None, None)]
    assert c.get(f"/api/etfs/{WORLD}").json()["profile"]["sources"] is not None  # an instrument exists now

    # a chosen non-default listing; an unknown one is rejected
    assert c.post(f"/api/etfs/{WORLD}/candidate", json={"listing_secid": "nope"}).status_code == 422
    assert c.delete(f"/api/etfs/{WORLD}/candidate").status_code == 204
    db.expire_all()
    inst = db.get(Instrument, iid)
    assert inst and not inst.meta.get("candidate") and inst.meta["ms_secid"] == "0P0000MEHZ"
    assert c.delete(f"/api/etfs/{WORLD}/candidate").status_code == 404
    assert c.get("/api/etfs/candidates").json() == [] and held_instruments(db) == []
    # marking it again reuses the instrument
    again = c.post(f"/api/etfs/{WORLD}/candidate", json={"listing_secid": "0P0000MEHZ-SE"}).json()
    assert again["instrument_id"] == iid
    db.expire_all()
    inst = db.get(Instrument, iid)
    assert inst and inst.meta["candidate"] is True and inst.meta["ms_secid"] == "0P0000MEHZ"


def test_held_etf_is_flagged_and_adopted_as_candidate(auth_client: TestClient, db: Session) -> None:
    load(db)
    c = auth_client
    held = hold(c, name="iShares Core MSCI World", type="etf", isin=WORLD)
    row = c.get("/api/etfs", params={"q": WORLD}).json()["rows"][0]
    assert row["held"] is True and row["instrument_id"] == held["id"] and row["candidate_instrument_id"] is None
    r = c.post(f"/api/etfs/{WORLD}/candidate", json={}).json()
    assert r["instrument_id"] == held["id"]  # the existing instrument, not a duplicate
    assert db.query(Instrument).filter(Instrument.isin == WORLD).count() == 1
    assert len(held_instruments(db)) == 1


def test_refresh_endpoint_queues_the_job(auth_client: TestClient, db: Session) -> None:
    rid = auth_client.post("/api/etfs/refresh").json()["run_id"]
    run = db.get(JobRun, rid)
    assert run and run.job == "etfs.universe" and run.status == "queued"


def test_prepare_job_fetches_profile_and_prices(auth_client: TestClient, db: Session) -> None:
    load(db)
    iid = auth_client.post(f"/api/etfs/{WORLD}/candidate", json={}).json()["instrument_id"]
    hit = {
        "SecId": "0P0000MEHZ",
        "Name": "iShares Core MSCI World",
        "Isin": WORLD,
        "ExchangeId": "EX$$$$XETR",
        "PriceCurrency": "EUR",
        "holdingTypeId": 22,
    }
    series = {"TimeSeries": {"Security": [{"HistoryDetail": [{"EndDate": "2026-10-09", "Value": "131.5"}]}]}}
    client = FakeClient(
        {
            f"{morningstar.BASE}/security_details/0P0000MEHZ": snap("world_etf"),
            morningstar.SCREENER: {"rows": [hit]},
            morningstar.SERIES: series,
        }
    )
    ctx = JobContext(db=db)
    stats = jobs.prepare(ctx, client)  # type: ignore[arg-type]
    assert stats == {"prepared": 1, "failed": 0}
    sources = {p.source for p in db.scalars(select(InstrumentProfile).where(InstrumentProfile.instrument_id == iid))}
    assert "morningstar" in sources
    assert [str(p.date) for p in db.scalars(select(Price).where(Price.instrument_id == iid))] == ["2026-10-09"]
    # nothing left to do the second time
    assert jobs.prepare(JobContext(db=db), client) == {"prepared": 0, "failed": 0}  # type: ignore[arg-type]


# ---- Nordnet availability ----------------------------------------------------------------------------------------


class NordnetClient:
    def __init__(self, body: dict[str, Any] | None = None, status: int = 200) -> None:
        self.body = body if body is not None else json.loads(Path("/srv/fixtures/nordnet/etflist.json").read_text())
        self.status = status
        self.headers: list[dict[str, str]] = []

    def get(self, url: str, **kw: Any) -> httpx.Response:
        self.headers.append(kw.get("headers") or {})
        return httpx.Response(self.status, json=self.body, request=httpx.Request("GET", url))


def test_nordnet_flags_the_etfs_it_sells(db: Session) -> None:
    load(db)
    c = NordnetClient()
    stats = nordnet.refresh(db, c)  # type: ignore[arg-type]
    db.commit()
    assert c.headers[0]["client-id"] == "NEXT"  # without it the list answers "invalid session"
    assert stats == {"nordnet": 3, "matched": 1}  # the fixture's other two ETFs are not in the screener fixture
    world = db.scalar(select(EtfListing).where(EtfListing.isin == WORLD))
    assert world and world.brokers["nordnet"] == {
        "symbol": "EUNL",
        "exchange": "XETR",
        "currency": "EUR",
        "monthly_saving": True,
        "owners": 101389,
    }
    # an empty answer changes nothing; a later list without the ETF clears its flag
    assert nordnet.refresh(db, NordnetClient({"results": [], "total_hits": 0})) == {"nordnet": 0, "matched": 0}  # type: ignore[arg-type]
    assert "nordnet" in world.brokers
    other = json.loads(Path("/srv/fixtures/nordnet/etflist.json").read_text())
    other["results"] = [r for r in other["results"] if r["instrument_info"]["isin"] != WORLD]
    nordnet.refresh(db, NordnetClient(other))  # type: ignore[arg-type]
    assert "nordnet" not in world.brokers
    with pytest.raises(httpx.HTTPStatusError):
        nordnet.refresh(db, NordnetClient({}, status=503))  # type: ignore[arg-type]


def test_broker_filter_and_owner_sort(auth_client: TestClient, db: Session) -> None:
    load(db)
    nordnet.refresh(db, NordnetClient())  # type: ignore[arg-type]
    db.commit()
    d = auth_client.get("/api/etfs?broker=nordnet").json()
    assert d["total"] == 1 and d["nordnet_total"] == 1 and d["rows"][0]["isin"] == WORLD
    assert d["rows"][0]["brokers"]["nordnet"]["owners"] == 101389
    assert auth_client.get("/api/etfs?broker=nordnet_monthly").json()["total"] == 1
    assert auth_client.get("/api/etfs?broker=nordea").status_code == 422
    rows = auth_client.get("/api/etfs?sort=owners").json()["rows"]
    assert rows[0]["isin"] == WORLD  # the only one with owners; the rest sort after it
