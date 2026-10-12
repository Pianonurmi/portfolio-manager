import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.ledger.models import Transaction
from app.modules.pricing.models import Distribution, FxRate, Price
from app.modules.pricing.providers import ecb, morningstar, yahoo
from app.modules.pricing.service import PriceBook, last_business_day, stale_text
from app.platform.db import today


class FakeClient:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                text = body if isinstance(body, str) else json.dumps(body)
                return httpx.Response(200, text=text, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


def ts(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp())


def test_yahoo_history_undoes_split_adjustment() -> None:
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD", "gmtoffset": 0},
                    "timestamp": [ts("2024-06-06T13:30:00"), ts("2024-06-07T13:30:00"), ts("2024-06-10T13:30:00")],
                    "indicators": {"quote": [{"close": [120.0, 121.0, 122.0]}]},
                    "events": {"splits": {"1": {"date": ts("2024-06-10T08:00:00"), "numerator": 10, "denominator": 1}}},
                }
            ]
        }
    }
    ccy, hist = yahoo.history("NVDA", date(2024, 6, 1), FakeClient({yahoo.CHART.format(symbol="NVDA"): body}))  # type: ignore[arg-type]
    assert ccy == "USD"
    assert [str(p) for _, p in hist] == ["1200.00000000", "1210.00000000", "122.00000000"]


def test_yahoo_resolve_prefers_home_exchange() -> None:
    body = {"quotes": [{"symbol": "SMPNY", "exchange": "PNK"}, {"symbol": "SAMPO.HE", "exchange": "HEL"}]}
    assert yahoo.resolve_isin("FI4000552500", "EUR", FakeClient({yahoo.SEARCH: body})) == "SAMPO.HE"  # type: ignore[arg-type]


def test_ecb_csv() -> None:
    csv = (
        "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-09,1.1206\n"
    )
    rows = ecb.fetch(["USD"], date(2026, 10, 1), FakeClient({ecb.BASE: csv}))  # type: ignore[arg-type]
    assert rows == [("USD", date(2026, 10, 9), Decimal("1.1206"))]


def test_pricebook_priority_and_fx(db: Session) -> None:
    i = Instrument(type="stock", name="NVIDIA", currency="USD")
    db.add(i)
    db.flush()
    db.add_all(
        [
            Price(instrument_id=i.id, date=date(2026, 10, 9), close=Decimal("182.40"), currency="USD", source="yahoo"),
            Price(instrument_id=i.id, date=date(2026, 10, 9), close=Decimal("180"), currency="USD", source="trade"),
            FxRate(quote="USD", date=date(2026, 10, 9), rate=Decimal("1.0900")),
        ]
    )
    db.flush()
    book = PriceBook(db)
    v, p, fx = book.unit_value_eur(i.id, date(2026, 10, 11))  # weekend: carries Friday
    assert p is not None and p.source == "yahoo" and p.date == date(2026, 10, 9)
    assert round(v, 2) == Decimal("167.34")


def test_stale_rule() -> None:
    from app.modules.pricing.service import PricePoint

    assert last_business_day(date(2026, 10, 12)) == date(2026, 10, 9)  # Monday -> Friday
    assert stale_text(PricePoint(date(2026, 10, 9), Decimal(1), "EUR", "yahoo"), date(2026, 10, 12)) is None
    assert stale_text(PricePoint(date(2026, 10, 7), Decimal(1), "EUR", "nav"), date(2026, 10, 12)) == "NAV 7.10."
    assert stale_text(None, date(2026, 10, 12)) == "no price"


def test_manual_price_and_freshness(auth_client: TestClient) -> None:
    c = auth_client
    inst = c.post("/api/instruments", json={"type": "fund", "name": "Seligson Global", "currency": "EUR"}).json()
    assert (
        c.post("/api/prices", json={"instrument_id": inst["id"], "date": "2026-10-09", "close": "12.34"}).status_code
        == 201
    )
    s = c.get(f"/api/prices/{inst['id']}").json()
    assert s[0]["source"] == "manual" and s[0]["close"] == "12.34000000"
    f = c.get("/api/freshness").json()
    assert f["stale_count"] == 0  # nothing held yet


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


def test_resolve_prefers_listing_in_instrument_currency() -> None:
    search = {"quotes": [{"symbol": "IWDA.L", "exchange": "LSE"}, {"symbol": "IWDA.AS", "exchange": "AMS"}]}
    routes = {
        yahoo.SEARCH: search,
        yahoo.CHART.format(symbol="IWDA.AS"): {"chart": {"result": [{"meta": {"currency": "EUR"}}]}},
        yahoo.CHART.format(symbol="IWDA.L"): {"chart": {"result": [{"meta": {"currency": "USD"}}]}},
    }
    assert yahoo.resolve_matching("IE00B4L5Y983", "EUR", FakeClient(routes)) == "IWDA.AS"  # type: ignore[arg-type]


def test_resolve_tries_other_venues_of_same_ticker() -> None:
    routes = {
        yahoo.SEARCH: {"quotes": [{"symbol": "IWDA.L", "exchange": "LSE"}]},
        yahoo.CHART.format(symbol="IWDA.L"): {"chart": {"result": [{"meta": {"currency": "USD"}}]}},
        yahoo.CHART.format(symbol="IWDA.AS"): {"chart": {"result": [{"meta": {"currency": "EUR"}}]}},
    }
    assert yahoo.resolve_matching("IE00B4L5Y983", "EUR", FakeClient(routes)) == "IWDA.AS"  # type: ignore[arg-type]


def test_split_day_bar_is_not_unadjusted() -> None:
    # Helsinki: bar timestamped 07:00 UTC, split event at 08:00 UTC the same day
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "EUR", "gmtoffset": 10800},
                    "timestamp": [ts("2024-11-01T07:00:00"), ts("2024-11-04T07:00:00")],
                    "indicators": {"quote": [{"close": [8.5, 8.6]}]},
                    "events": {"splits": {"1": {"date": ts("2024-11-04T08:00:00"), "numerator": 5, "denominator": 1}}},
                }
            ]
        }
    }
    _, hist = yahoo.history("SAMPO.HE", date(2024, 10, 1), FakeClient({yahoo.CHART.format(symbol="SAMPO.HE"): body}))  # type: ignore[arg-type]
    assert [str(p) for _, p in hist] == ["42.50000000", "8.60000000"]


def test_missing_split_detected_and_booked(auth_client: TestClient) -> None:
    from app.modules.pricing.models import CorporateAction
    from app.platform.db import session_factory

    c = auth_client
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts", json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]}
    ).json()
    i = c.post("/api/instruments", json={"type": "stock", "name": "NVIDIA", "currency": "USD"}).json()
    c.post(
        "/api/ledger/transactions",
        json={"account_id": a["id"], "date": "2024-01-02", "kind": "deposit", "amount": "5000"},
    )
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": a["id"],
            "date": "2024-01-03",
            "kind": "buy",
            "amount": "-4000",
            "instrument_id": i["id"],
            "quantity": "8",
        },
    )
    with session_factory()() as s:
        s.add(CorporateAction(instrument_id=i["id"], date=date(2024, 6, 10), ratio=Decimal(10), source="yahoo"))
        s.commit()
    missing = c.get("/api/corporate-actions/missing").json()
    assert len(missing) == 1 and missing[0]["accounts"][0]["quantity"] == "8.00000000"
    att = [x for x in c.get("/api/attention").json() if x["title"].startswith("Split missing")]
    assert att and att[0]["action"]["path"].endswith("/book")
    r = c.post(att[0]["action"]["path"]).json()
    pos = {p["instrument_id"]: p["quantity"] for p in c.get("/api/ledger/positions").json()}
    assert Decimal(pos[i["id"]]) == 80
    assert c.get("/api/corporate-actions/missing").json() == []
    assert c.post(att[0]["action"]["path"]).status_code == 409
    c.post(f"/api/undo/{r['undo_id']}")
    assert len(c.get("/api/corporate-actions/missing").json()) == 1


# ---- Morningstar ----------------------------------------------------------------------------------


def ms_row(secid: str, name: str, isin: str, htype: int, ccy: str = "EUR", ex: str | None = "XHEL") -> dict[str, Any]:
    row: dict[str, Any] = {"SecId": secid, "Name": name, "Isin": isin, "PriceCurrency": ccy, "holdingTypeId": htype}
    if ex:
        row.update(ExchangeId=f"EX$$$${ex}", Ticker=name[:5].upper())
    return row


class MsClient:
    """Answers the screener by (universe, term) and the time series by SecId; Yahoo by prefix."""

    def __init__(
        self,
        screener: dict[tuple[str, str], list[dict[str, Any]]],
        series: dict[str, list[tuple[str, str]]] | None = None,
        yahoo_routes: dict[str, Any] | None = None,
    ) -> None:
        self.screener, self.series, self.yahoo_routes = screener, series or {}, yahoo_routes or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kw: Any) -> httpx.Response:
        params = kw.get("params") or {}
        self.calls.append((url, params))
        req = httpx.Request("GET", url)
        if url == morningstar.SCREENER:
            rows = self.screener.get((params["universeIds"], params["term"]), [])
            return httpx.Response(200, json={"total": len(rows), "rows": rows}, request=req)
        if url == morningstar.SERIES:
            secid = params["id"].split("]")[0]
            detail = [{"EndDate": d, "Value": v} for d, v in self.series.get(secid, [])]
            return httpx.Response(200, json={"TimeSeries": {"Security": [{"HistoryDetail": detail}]}}, request=req)
        if url == yahoo.SEARCH:
            quotes = self.yahoo_routes.get(yahoo.SEARCH, {}).get(params["q"], [])
            return httpx.Response(200, json={"quotes": quotes}, request=req)
        for prefix, body in self.yahoo_routes.items():
            if url.startswith(prefix):
                return httpx.Response(200, json=body, request=req)
        return httpx.Response(404, request=req)


def ms_find(client: MsClient, name: str, isin: str | None = None, type: str = "stock") -> morningstar.Match | None:
    return morningstar.find(name, isin, type, "EUR", client)  # type: ignore[arg-type]


def test_morningstar_search_and_history_parse() -> None:
    kesko = ms_row("0P0000A5W7", "Kesko Oyj Class B", "FI0009000202", 3)
    c = MsClient(
        {(morningstar.HELSINKI_UNIVERSE, "x"): [kesko]}, {"0P0000A5W7": [("2026-10-08", "23.4"), ("2026-10-09", "23")]}
    )
    m = morningstar.search("x", morningstar.HELSINKI_UNIVERSE, c)[0]  # type: ignore[arg-type]
    assert (m.secid, m.isin, m.exchange, m.currency, m.holding_type) == ("0P0000A5W7", "FI0009000202", "XHEL", "EUR", 3)
    hist = morningstar.history(m, date(2026, 10, 1), "EUR", c)  # type: ignore[arg-type]
    assert hist == [(date(2026, 10, 8), Decimal("23.4")), (date(2026, 10, 9), Decimal("23"))]
    assert str(hist[0][1]) == "23.40000000"
    assert c.calls[-1][1]["id"] == "0P0000A5W7]3]0]E0EXG$XHEL"


def test_morningstar_name_match_share_class() -> None:
    rows = [ms_row("A", "Kesko Oyj Class A", "FI0009007900", 3), ms_row("B", "Kesko Oyj Class B", "FI0009000202", 3)]
    c = MsClient({(morningstar.HELSINKI_UNIVERSE, "kesko"): rows})
    m = ms_find(c, "Kesko Oyj B")
    assert m is not None and m.isin == "FI0009000202"
    assert ms_find(c, "Kesko Oyj") is None  # A or B: ambiguous
    only_a = MsClient({(morningstar.HELSINKI_UNIVERSE, "kesko"): rows[:1]})
    assert ms_find(only_a, "Kesko Oyj B") is None  # class letter mismatch
    assert ms_find(c, "Kesko Oyj C") is None


def test_morningstar_name_match_is_strict() -> None:
    rows = [ms_row("S1", "Sampo Oyj Class A", "FI4000552500", 3)]
    c = MsClient({(morningstar.HELSINKI_UNIVERSE, "sampo"): rows})
    assert ms_find(c, "Sampo Oyj A") is not None
    assert ms_find(c, "Sampo Oyj A", type="stock") is not None
    wrong = MsClient({(morningstar.HELSINKI_UNIVERSE, "sampo"): [ms_row("S2", "Sampo Bank Oyj A", "FI1", 3)]})
    assert ms_find(wrong, "Sampo Oyj A") is None
    usd = MsClient({(morningstar.HELSINKI_UNIVERSE, "sampo"): [ms_row("S3", "Sampo Oyj A", "FI2", 3, ccy="USD")]})
    assert ms_find(usd, "Sampo Oyj A") is None
    assert not [
        u for u, _ in c.calls if u == morningstar.SCREENER and _["universeIds"] != morningstar.HELSINKI_UNIVERSE
    ]


def test_morningstar_isin_prefers_currency_and_exchange() -> None:
    isin = "IE00B4L5Y983"
    etf = [
        ms_row("L", "iShares Core MSCI World", isin, 22, ccy="USD", ex="XLON"),
        ms_row("F", "iShares Core MSCI World", isin, 22, ex="XETR"),
        ms_row("A", "iShares Core MSCI World", isin, 22, ex="XAMS"),
        ms_row("O", "Other", "IE0000000000", 22, ex="XAMS"),
    ]
    c = MsClient({(morningstar.ETF_UNIVERSE, isin): etf})
    m = ms_find(c, "iShares", isin, "etf")
    assert m is not None and m.secid == "A"
    fund = MsClient(
        {(morningstar.FUND_UNIVERSE, "SE0006789715"): [ms_row("F00000VD4H", "HB Europa", "SE0006789715", 2, ex=None)]}
    )
    m = ms_find(fund, "HB", "SE0006789715", "fund")
    assert m is not None and m.holding_type == 2 and m.exchange is None
    assert ms_find(MsClient({}), "HB", "SE0006789715", "fund") is None


# ---- fetch_prices end to end ----------------------------------------------------------------------


def hold(c: TestClient, *, name: str, type: str = "stock", isin: str | None = None, **extra: Any) -> dict[str, Any]:
    persons = c.get("/api/persons").json()
    if persons:
        me, accts = persons[0], c.get("/api/accounts").json()
        acct = accts[0]
    else:
        me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
        acct = c.post(
            "/api/accounts",
            json={"name": "AOT", "kind": "aot", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
        ).json()
        c.post(
            "/api/ledger/transactions",
            json={"account_id": acct["id"], "date": "2024-01-02", "kind": "deposit", "amount": "50000"},
        )
    i = c.post("/api/instruments", json={"type": type, "name": name, "currency": "EUR", "isin": isin, **extra}).json()
    r = c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct["id"],
            "date": "2024-01-03",
            "kind": "buy",
            "amount": "-1000",
            "instrument_id": i["id"],
            "quantity": "10",
        },
    )
    assert r.status_code in (200, 201), r.text
    return i


def inst_row(i: dict[str, Any]) -> Instrument:
    from app.platform.db import session_factory

    with session_factory()() as s:
        row = s.get(Instrument, i["id"])
        assert row is not None
        s.expunge(row)
        return row


YAHOO_KESKO = {
    yahoo.SEARCH: {"FI0009000202": [{"symbol": "KESKOB.HE", "exchange": "HEL"}]},
    yahoo.CHART.format(symbol="KESKOB.HE"): {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "EUR", "gmtoffset": 0},
                    "timestamp": [ts("2026-10-09T08:00:00")],
                    "indicators": {"quote": [{"close": [23.5]}]},
                }
            ]
        }
    },
}


def run_fetch(client: MsClient, full: bool = False) -> dict[str, int]:
    from app.modules.pricing.service import fetch_prices
    from app.platform.db import session_factory

    with session_factory()() as s:
        stats = fetch_prices(s, client, None, full=full)  # type: ignore[arg-type]
        s.commit()
    return stats


def test_fetch_prices_finnish_stock_without_isin_and_nav_fund(auth_client: TestClient) -> None:
    c = auth_client
    stock = hold(c, name="Kesko Oyj B")
    fund = hold(c, name="Handelsbanken Europa Index", type="stock", isin="SE0006789715")
    rows = [ms_row("A", "Kesko Oyj Class A", "FI0009007900", 3), ms_row("B", "Kesko Oyj Class B", "FI0009000202", 3)]
    client = MsClient(
        {
            (morningstar.HELSINKI_UNIVERSE, "kesko"): rows,
            (morningstar.FUND_UNIVERSE, "SE0006789715"): [
                ms_row("F00000VD4H", "HB Europa", "SE0006789715", 2, ex=None)
            ],
        },
        {"F00000VD4H": [("2026-10-08", "23.04"), ("2026-10-09", "23.09")]},
        YAHOO_KESKO,
    )
    run_fetch(client)
    s = inst_row(stock)
    assert s.isin == "FI0009000202" and s.price_symbol == "KESKOB.HE"
    assert s.meta["price_source"] == "yahoo" and s.meta["ms_secid"] == "B"
    f = inst_row(fund)
    assert f.type == "fund" and f.price_symbol is None
    assert f.meta["price_source"] == "morningstar" and f.meta["ms_type"] == 2 and f.meta["ms_secid"] == "F00000VD4H"
    nav = c.get(f"/api/prices/{fund['id']}").json()
    assert [(p["source"], p["close"]) for p in nav] == [("nav", "23.04000000"), ("nav", "23.09000000")]
    assert any(p["source"] == "yahoo" for p in c.get(f"/api/prices/{stock['id']}").json())


def test_fetch_prices_duplicate_isin_is_recorded_not_set(auth_client: TestClient) -> None:
    c = auth_client
    hold(c, name="Kesko Oyj Class B", isin="FI0009000202", price_symbol="KESKOB.HE")
    dup = hold(c, name="Kesko Oyj B")
    rows = [ms_row("B", "Kesko Oyj Class B", "FI0009000202", 3)]
    client = MsClient({(morningstar.HELSINKI_UNIVERSE, "kesko"): rows}, {"B": []}, YAHOO_KESKO)
    run_fetch(client)
    d = inst_row(dup)
    assert d.isin is None
    assert "same ISIN FI0009000202 as instrument #" in d.meta["isin_conflict"]
    assert "merge" in d.meta["price_problem"]


def test_full_run_retries_failed_instrument(auth_client: TestClient) -> None:
    c = auth_client
    fund = hold(c, name="Storebrand Global", type="fund", isin="SE0013801453")
    empty = MsClient({})
    run_fetch(empty)
    assert inst_row(fund).meta["price_problem"].startswith("not found")
    found = MsClient(
        {(morningstar.FUND_UNIVERSE, "SE0013801453"): [ms_row("F1", "Storebrand Global", "SE0013801453", 2, ex=None)]},
        {"F1": [("2026-10-09", "11.5")]},
    )
    run_fetch(found)  # daily run: still skipped
    assert found.calls == []
    run_fetch(found, full=True)
    meta = inst_row(fund).meta
    assert meta["price_source"] == "morningstar" and "price_problem" not in meta


def test_price_coverage_lists_missing_first_with_reason(auth_client: TestClient) -> None:
    c = auth_client
    ok = hold(c, name="Aaa Fund", type="fund", isin="SE0006800140")
    bad = hold(c, name="Zzz Oyj")
    client = MsClient(
        {(morningstar.FUND_UNIVERSE, "SE0006800140"): [ms_row("F2", "Aaa Fund", "SE0006800140", 2, ex=None)]},
        {"F2": [(last_business_day(today()).isoformat(), "10")]},
    )
    run_fetch(client)
    cov = c.get("/api/portfolio/price-coverage").json()
    assert [r["id"] for r in cov] == [bad["id"], ok["id"]]
    assert cov[0]["status"] == "missing" and "no unambiguous name match" in cov[0]["reason"]
    assert cov[1]["status"] == "ok" and cov[1]["price_source"] == "morningstar" and cov[1]["symbol"] == "F2"
    assert cov[1]["last_close"] == "10.00000000"


# ---- name matching: noise words, home venues, fund share classes -----------------------------------


def ms_name(
    client: MsClient,
    name: str,
    *,
    type: str = "stock",
    ccy: str = "EUR",
    hint: tuple[date, Decimal] | None = None,
) -> morningstar.Match | None:
    return morningstar.find(name, None, type, ccy, client, hint)  # type: ignore[arg-type]


def test_noise_words_and_share_class_on_their_side() -> None:
    hel = morningstar.HELSINKI_UNIVERSE
    c = MsClient(
        {
            (hel, "harvia"): [ms_row("0P0001CXUX", "Harvia PLC Ordinary Shares", "FI4000306873", 3)],
            (hel, "terveystalo"): [ms_row("0P0001BS02", "Terveystalo PLC Ordinary Shares", "FI4000252127", 3)],
            (hel, "kalmar"): [
                ms_row("0P0001T3IN", "Kalmar Corp Class B Shares", "FI4000571054", 3),
                ms_row("KA", "Kalmar Corp Class A Shares", "FI4000571047", 3),
            ],
        }
    )
    h = ms_name(c, "Harvia Oyj")
    assert h is not None and h.isin == "FI4000306873"
    t = ms_name(c, "Terveystalo Oyj")
    assert t is not None and t.secid == "0P0001BS02"
    k = ms_name(c, "Kalmar Oyj B")
    assert k is not None and k.secid == "0P0001T3IN"
    assert ms_name(c, "Kalmar Oyj") is None  # A or B


def test_eur_stock_without_finnish_marker_searches_helsinki() -> None:
    hel = morningstar.HELSINKI_UNIVERSE
    c = MsClient({(hel, "mandatum"): [ms_row("0P0001QQYJ", "Mandatum PLC", "FI4000552526", 3)]})
    m = ms_name(c, "Mandatum")
    assert m is not None and m.isin == "FI4000552526"
    assert {p["universeIds"] for u, p in c.calls if u == morningstar.SCREENER} == {hel}
    assert ms_name(MsClient({}), "Mandatum", type="etf") is None  # funds never go to the stock universe


def test_foreign_stock_uses_global_universe_and_home_venue() -> None:
    world = morningstar.WORLD_UNIVERSE
    rows = [
        ms_row("JPN", "Coca-Cola Bottlers Japan Holdings Inc", "JP3293200006", 3, ccy="USD", ex="OTCM"),
        ms_row("SGO", "Coca-Cola Co", "US1912161007", 3, ccy="USD", ex="XSGO"),
        ms_row("DE", "Coca-Cola Co", "US1912161007", 3, ccy="EUR", ex="XETR"),
        ms_row("NAS", "Coca-Cola Co", "US1912161007", 3, ccy="USD", ex="XNAS"),
        ms_row("NYS", "Coca-Cola Co", "US1912161007", 3, ccy="USD", ex="XNYS"),
    ]
    c = MsClient({(world, "coca cola co"): rows})
    m = ms_name(c, "Coca-Cola Co", ccy="USD")
    assert m is not None and m.secid == "NYS" and m.isin == "US1912161007" and m.universe == world
    assert ms_name(c, "Coca-Cola Co", ccy="CAD") is None  # no known home venue for the currency
    other = MsClient({(world, "coca cola co"): [rows[1]]})
    assert ms_name(other, "Coca-Cola Co", ccy="USD") is None  # only a minor venue


ISH = "IE00BDBRDM35"
ISH_ROWS = [
    ms_row("I1", "iShares Core Global Aggt Bd ETF EUR HAcc", ISH, 22, ex="XETR"),
    ms_row("I2", "iShares Core Global Aggt Bd ETF EUR HAcc", ISH, 22, ex="XAMS"),
    ms_row("I3", "iShares Core Global Aggt Bd ETF EUR H Dist", "IE00BDBRDN59", 22, ex="XETR"),
    ms_row("I4", "iShares Core Global Aggt Bd ETF GBP HAcc", "IE00BDBRDP73", 22, ccy="GBP", ex="XLON"),
]
ON = date(2026, 10, 11)


def test_etf_share_classes_picked_by_statement_price() -> None:
    name = "iShares Global Aggregate Bond UCITS ETF"
    screener = {(morningstar.ETF_UNIVERSE, "ishares global aggregate bond"): ISH_ROWS}
    c = MsClient(screener, {"I2": [("2026-10-08", "4.80"), ("2026-10-09", "4.81")], "I3": [("2026-10-09", "5.20")]})
    assert ms_name(c, name, type="etf") is None  # accumulating and distributing classes both fit
    m = ms_name(c, name, type="etf", hint=(ON, Decimal("4.81")))
    assert m is not None and m.isin == ISH and m.secid == "I2"  # XAMS outranks XETR
    assert ms_name(c, name, type="etf", hint=(ON, Decimal("9.99"))) is None
    both = MsClient(screener, {"I2": [("2026-10-09", "4.81")], "I3": [("2026-10-09", "4.82")]})
    assert ms_name(both, name, type="etf", hint=(ON, Decimal("4.81"))) is None  # two survive: no guess
    # the close on or just before the statement date counts, a later one does not
    late = MsClient(screener, {"I2": [("2026-10-13", "4.81")], "I3": []})
    assert ms_name(late, name, type="etf", hint=(ON, Decimal("4.81"))) is None


def test_etf_markers_rule_out_the_opposite_class() -> None:
    name = "Vanguard Global Aggregate Bond UCITS EUR Hedged Accumulating ETF"
    rows = [
        ms_row("V1", "Vanguard Global Aggt Bd ETF EUR H Acc", "IE00BG47KH54", 22, ex="XAMS"),
        ms_row("V2", "Vanguard Global Aggt Bd ETF EUR H Inc", "IE00BG47KB92", 22, ex="XAMS"),
        ms_row("V3", "Vanguard Global Aggt Bd ETF CHF H Acc", "IE00BG47KJ78", 22, ccy="CHF", ex="XSWX"),
    ]
    c = MsClient({(morningstar.ETF_UNIVERSE, "vanguard global aggregate bond"): rows})
    m = ms_name(c, name, type="etf")
    assert m is not None and m.isin == "IE00BG47KH54"
    dist = ms_name(c, "Vanguard Global Aggregate Bond UCITS EUR Hedged Distributing ETF", type="etf")
    assert dist is not None and dist.isin == "IE00BG47KB92"
    # no markers of ours: both EUR classes fit, the statement price decides
    amb = "Vanguard Global Aggregate Bond UCITS ETF"
    c2 = MsClient(
        {(morningstar.ETF_UNIVERSE, "vanguard global aggregate bond"): rows}, {"V1": [("2026-10-09", "23.06")]}
    )
    assert ms_name(c2, amb, type="etf") is None
    picked = ms_name(c2, amb, type="etf", hint=(ON, Decimal("23.06")))
    assert picked is not None and picked.secid == "V1"


def test_subscription_rights_stay_unmatched() -> None:
    hel = morningstar.HELSINKI_UNIVERSE
    c = MsClient({(hel, "revenio subs"): [ms_row("R", "Revenio Group Oyj", "FI0009010912", 3)]})
    assert ms_name(c, "REVENIO SUBS") is None
    assert (
        ms_name(MsClient({(hel, "revenio"): [ms_row("R", "Revenio Group Oyj", "FI0009010912", 3)]}), "REVENIO SUBS")
        is None
    )


def _statement_price(inst: dict[str, Any], d: str, close: str) -> None:
    from app.platform.db import session_factory

    with session_factory()() as s:
        s.add(
            Price(
                instrument_id=inst["id"],
                date=date.fromisoformat(d),
                close=Decimal(close),
                currency="EUR",
                source="statement",
            )
        )
        s.commit()


def test_fetch_prices_uses_latest_statement_price_as_hint(auth_client: TestClient) -> None:
    c = auth_client
    etf = hold(c, name="iShares Global Aggregate Bond UCITS ETF", type="etf")
    _statement_price(etf, "2026-10-09", "4.20")
    _statement_price(etf, "2026-10-11", "4.81")
    client = MsClient(
        {(morningstar.ETF_UNIVERSE, "ishares global aggregate bond"): ISH_ROWS},
        {"I2": [("2026-10-09", "4.81")], "I3": [("2026-10-09", "5.20")]},
    )
    run_fetch(client)
    row = inst_row(etf)
    assert row.isin == ISH and row.meta["ms_secid"] == "I2" and row.meta["price_source"] == "morningstar"


def test_price_coverage_no_feed_between_missing_and_stale(auth_client: TestClient) -> None:
    c = auth_client
    snap = hold(c, name="Snapshot Fund", type="fund")
    missing = hold(c, name="Aaa Oyj")
    _statement_price(snap, last_business_day(today()).isoformat(), "10")
    cov = c.get("/api/portfolio/price-coverage").json()
    assert [r["id"] for r in cov] == [missing["id"], snap["id"]]
    assert cov[0]["status"] == "missing" and cov[1]["status"] == "no_feed"
    assert cov[1]["reason"] == "only statement/trade prices; no price feed"


def test_placeholder_and_thin_yahoo_listings_fall_back_to_morningstar(auth_client: TestClient) -> None:
    """Yahoo's ISIN-named listing (IE000BI8OT95.SG) has no prices; Morningstar's series is used instead."""
    c = auth_client
    etf = hold(c, name="Amundi Core MSCI World UCITS ETF Acc", type="etf", isin="IE000BI8OT95")
    client = MsClient(
        {
            (morningstar.ETF_UNIVERSE, "IE000BI8OT95"): [
                ms_row("0P0001U2EV", "Amundi Core MSCI World", "IE000BI8OT95", 22)
            ]
        },
        {"0P0001U2EV": [("2026-10-08", "120.10"), ("2026-10-09", "120.55")]},
        {yahoo.SEARCH: {"IE000BI8OT95": [{"symbol": "IE000BI8OT95.SG", "exchange": "STU"}]}},
    )
    run_fetch(client)
    e = inst_row(etf)
    assert e.price_symbol is None and e.meta["price_source"] == "morningstar" and "price_problem" not in e.meta
    assert [p["source"] for p in c.get(f"/api/prices/{etf['id']}").json()] == ["nav", "nav"]
    cov = {r["id"]: r for r in c.get("/api/portfolio/price-coverage").json()}
    assert cov[etf["id"]]["status"] in ("ok", "stale")


def test_price_snapshot_is_shared_and_sees_changes_in_place(db: Session) -> None:
    """PriceBook reuses one snapshot until a price is added or rewritten (a backfill changes closes in place)."""
    from app.modules.pricing import service

    inst = Instrument(type="etf", name="Snap ETF", currency="EUR")
    db.add(inst)
    db.flush()
    service.upsert_prices(
        db,
        [
            {
                "instrument_id": inst.id,
                "date": date(2026, 1, 2),
                "close": Decimal("10"),
                "currency": "EUR",
                "source": "yahoo",
            }
        ],
    )
    a, b = PriceBook(db, {inst.id}), PriceBook(db, None)
    assert a.prices[inst.id] is b.prices[inst.id]  # one snapshot, no second read of every price
    service.upsert_prices(
        db,
        [
            {
                "instrument_id": inst.id,
                "date": date(2026, 1, 2),
                "close": Decimal("11"),
                "currency": "EUR",
                "source": "yahoo",
            }
        ],
    )
    p = PriceBook(db, {inst.id}).price(inst.id, date(2026, 1, 2))
    assert p is not None and p.close == Decimal("11")


# ---- distributions (dividends) --------------------------------------------------------------------


def _chart(currency: str = "USD", **events: Any) -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": currency, "gmtoffset": 0},
                    "timestamp": [ts("2024-05-15T13:30:00"), ts("2024-06-10T13:30:00")],
                    "indicators": {"quote": [{"close": [12.0, 12.2]}]},
                    "events": events,
                }
            ]
        }
    }


def test_yahoo_dividends_are_unadjusted_for_later_splits() -> None:
    body = _chart(
        splits={"1": {"date": ts("2024-06-10T08:00:00"), "numerator": 10, "denominator": 1}},
        dividends={
            "a": {"amount": 0.04, "date": ts("2024-05-15T08:00:00")},  # before the split: back-adjusted by Yahoo
            "b": {"amount": 0.5, "date": ts("2024-06-20T08:00:00")},  # after it: as quoted
            "c": {"amount": 0, "date": ts("2024-06-21T08:00:00")},
        },
    )
    client = FakeClient({yahoo.CHART.format(symbol="X"): body})
    ccy, hist, splits, divs = yahoo.history_with_events("X", date(2024, 5, 1), client)  # type: ignore[arg-type]
    assert ccy == "USD" and len(splits) == 1
    assert divs == [(date(2024, 5, 15), Decimal("0.40000000")), (date(2024, 6, 20), Decimal("0.50000000"))]
    assert [str(p) for _, p in hist] == ["120.00000000", "12.20000000"]
    # the older entry point still returns three values
    assert len(yahoo.history_with_splits("X", date(2024, 5, 1), client)) == 3  # type: ignore[arg-type]


def test_yahoo_dividends_in_pence_become_pounds() -> None:
    body = _chart("GBp", dividends={"a": {"amount": 25.0, "date": ts("2024-05-15T08:00:00")}})
    _, _, _, divs = yahoo.history_with_events("X", date(2024, 5, 1), FakeClient({yahoo.CHART.format(symbol="X"): body}))  # type: ignore[arg-type]
    assert divs == [(date(2024, 5, 15), Decimal("0.25000000"))]


def test_price_job_stores_yahoo_distributions_idempotently(auth_client: TestClient) -> None:
    from sqlalchemy import select

    from app.modules.pricing.models import Distribution
    from app.platform.db import session_factory

    c = auth_client
    i = hold(c, name="Kesko Oyj B", isin="FI0009000202", price_symbol="KESKOB.HE")
    body = _chart(
        "EUR",
        dividends={
            "a": {"amount": 0.88, "date": ts("2025-04-10T07:00:00")},
            "b": {"amount": 0.9, "date": ts("2026-04-10T07:00:00")},
        },
    )
    client = MsClient({}, {}, {yahoo.CHART.format(symbol="KESKOB.HE"): body})
    run_fetch(client)
    body["chart"]["result"][0]["events"]["dividends"]["b"]["amount"] = 0.95  # corrected by the provider
    run_fetch(client, full=True)
    with session_factory()() as s:
        rows = s.scalars(
            select(Distribution).where(Distribution.instrument_id == i["id"]).order_by(Distribution.ex_date)
        ).all()
        assert [(r.ex_date, r.amount, r.currency, r.source) for r in rows] == [
            (date(2025, 4, 10), Decimal("0.88000000"), "EUR", "yahoo"),
            (date(2026, 4, 10), Decimal("0.95000000"), "EUR", "yahoo"),
        ]


def test_ledger_distributions_are_gross_per_unit_held(auth_client: TestClient) -> None:
    from app.modules.ledger.service import TxnSpec, record
    from app.modules.pricing.distributions import ledger_distributions, ledger_dividend_instruments
    from app.platform.db import session_factory

    c = auth_client
    i = hold(c, name="Sampo Oyj A")  # 10 units on 2024-01-03
    acct = c.get("/api/accounts").json()[0]["id"]
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": acct,
            "date": "2024-06-01",
            "kind": "buy",
            "amount": "-40",
            "instrument_id": i["id"],
            "quantity": "10",
        },
    )
    early = c.post("/api/instruments", json={"type": "stock", "name": "Never held", "currency": "EUR"}).json()
    with session_factory()() as s:
        for d, inst, net, tax in (
            (date(2024, 5, 1), i["id"], "8.00", "2.00"),  # 10 units held: 1.00 gross → 0.10 each
            (date(2024, 7, 1), i["id"], "32.00", "8.00"),  # 20 units: 40.00 gross → 2.00 each
            (date(2024, 7, 2), early["id"], "5.00", "0"),  # nothing held: no per-unit amount
        ):
            record(
                s,
                TxnSpec(
                    account_id=acct,
                    date=d,
                    kind="dividend",
                    currency="EUR",
                    amount=Decimal(net),
                    tax=Decimal(tax),
                    instrument_id=inst,
                ),
            )
        s.commit()
        got = ledger_distributions(s, [i["id"], early["id"]])
        booked = ledger_dividend_instruments(s, [i["id"], early["id"]])
    assert got == {i["id"]: [(date(2024, 5, 1), Decimal("1.00000000")), (date(2024, 7, 1), Decimal("2.00000000"))]}
    assert booked == {i["id"], early["id"]}


def test_company_listed_as_corp_matches_our_oyj_and_is_retried_daily(auth_client: TestClient) -> None:
    """Morningstar lists "Rapala VMC Oyj" as "Rapala VMC Corp"; an earlier miss must not stick for a name-only stock."""
    c = auth_client
    rapala = hold(c, name="Rapala VMC Oyj")
    row = ms_row("0P0000A5XG", "Rapala VMC Corp", "FI0009007355", 3)
    ms = MsClient({(morningstar.HELSINKI_UNIVERSE, "rapala vmc"): [row]}, {"0P0000A5XG": [("2026-10-09", "3.2")]})
    assert ms_find(ms, "Rapala VMC Oyj") is not None
    from app.platform.db import session_factory

    with session_factory()() as s:
        inst = s.get(Instrument, rapala["id"])
        assert inst is not None
        inst.meta = {"price_problem": "no ISIN, and no unambiguous name match"}  # recorded by an older run
        s.commit()
    run_fetch(ms)  # a daily, not full, run
    got = inst_row(rapala)
    assert got.isin == "FI0009007355" and got.meta["price_source"] == "morningstar" and "price_problem" not in got.meta


NDA = "FI4000297767"


def _nordea_clients(ms_rows: list[dict[str, Any]], nav: list[tuple[str, str]]) -> MsClient:
    chart = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "SEK", "gmtoffset": 0},
                    "timestamp": [ts("2026-10-08T08:00:00"), ts("2026-10-09T08:00:00")],
                    "indicators": {"quote": [{"close": [130.0, 131.0]}]},
                    "events": {"dividends": {"1": {"date": ts("2026-10-09T08:00:00"), "amount": 0.9}}},
                }
            ]
        }
    }
    return MsClient(
        {(morningstar.HELSINKI_UNIVERSE, NDA): ms_rows},
        {"NDA1": nav},
        {yahoo.CHART.format(symbol="NDA-SE.ST"): chart},
    )


def test_foreign_yahoo_listing_is_replaced_by_home_currency_morningstar_series(auth_client: TestClient) -> None:
    """Nordea quoted in SEK at Yahoo: the EUR Morningstar listing becomes the feed, the SEK closes are dropped."""
    c = auth_client
    nda = hold(c, name="Nordea Bank Abp", isin=NDA, price_symbol="NDA-SE.ST")
    client = _nordea_clients(
        [ms_row("NDA1", "Nordea Bank Abp", NDA, 3)], [("2026-10-08", "11.60"), ("2026-10-09", "11.70")]
    )
    run_fetch(client)
    meta = inst_row(nda).meta
    assert (
        meta["price_source"] == "morningstar" and meta["home_listing"] == "morningstar" and meta["ms_secid"] == "NDA1"
    )
    assert [(p["source"], p["currency"]) for p in c.get(f"/api/prices/{nda['id']}").json()] == [("nav", "EUR")] * 2
    from app.platform.db import session_factory

    with session_factory()() as s:
        assert s.query(Distribution).filter_by(instrument_id=nda["id"]).count() == 1  # dividends are kept, in SEK

    # the next daily run keeps asking Yahoo for events but never stores its closes again
    run_fetch(client)
    assert {p["source"] for p in c.get(f"/api/prices/{nda['id']}").json()} == {"nav"}


def test_foreign_listing_stays_when_no_home_listing_exists(auth_client: TestClient) -> None:
    c = auth_client
    nda = hold(c, name="Nordea Bank Abp", isin=NDA, price_symbol="NDA-SE.ST")
    run_fetch(_nordea_clients([], []))
    row = inst_row(nda)
    assert row.meta["home_listing"] == "none" and row.meta["price_source"] != "morningstar"
    assert {p["source"] for p in c.get(f"/api/prices/{nda['id']}").json()} == {"yahoo"}


def test_split_adjusted_nav_is_scaled_back_before_a_booked_split() -> None:
    from datetime import date as _d
    from decimal import Decimal as _D

    from app.modules.pricing.service import PricePoint, _unadjust

    pts = [
        PricePoint(_d(2026, 2, 20), _D("10"), "EUR", "nav"),
        PricePoint(_d(2026, 2, 23), _D("10.3"), "EUR", "nav"),
        PricePoint(_d(2026, 2, 19), _D("250"), "EUR", "yahoo"),  # traded closes are not adjusted: left alone
    ]
    out = _unadjust(pts, [(_d(2026, 2, 23), _D(25))])
    assert [p.close for p in out] == [_D("250"), _D("10.3"), _D("250")]
    assert _unadjust(pts, []) is pts


def test_split_factor_scales_per_unit_values_across_a_split() -> None:
    from datetime import date as _d
    from decimal import Decimal as _D

    from app.modules.pricing.service import PriceBook

    b = PriceBook.__new__(PriceBook)
    b.splits = {7: [(_d(2026, 2, 23), _D(25))]}
    assert b.split_factor(7, _d(2026, 2, 20)) == 25 and b.split_factor(7, _d(2026, 2, 23)) == 1
    assert b.split_factor(8, _d(2020, 1, 1)) == 1


def test_routine_runs_skip_settled_positions(db: Session) -> None:
    from app.modules.core.models import Account
    from app.modules.ledger.service import TxnSpec, record, void
    from app.modules.pricing.service import held_instruments, settled_positions

    acc = Account(name="AOT", kind="aot", currency="EUR")
    db.add(acc)
    db.flush()

    def inst(name: str, **meta: Any) -> Instrument:
        i = Instrument(type="stock", name=name, currency="EUR", meta=meta)
        db.add(i)
        db.flush()
        return i

    def trade(i: Instrument, bought: str, sold: str | None = None) -> None:
        record(db, TxnSpec(acc.id, date.fromisoformat(bought), "buy", "EUR", Decimal(-100), i.id, Decimal(10)))
        if sold:
            record(db, TxnSpec(acc.id, date.fromisoformat(sold), "sell", "EUR", Decimal(120), i.id, Decimal(10)))

    def price(i: Instrument, on: str, source: str = "yahoo") -> None:
        db.add(Price(instrument_id=i.id, date=date.fromisoformat(on), close=Decimal(12), currency="EUR", source=source))

    covered, held, gap, lost, bench = (inst(n) for n in ("covered", "held", "gap", "lost", "bench"))
    lost.meta = {"price_problem": "not found"}
    bench.meta = {"benchmark": "WORLD"}
    for i in (covered, gap, lost, bench):
        trade(i, "2024-01-02", "2024-06-03")
    trade(held, "2024-01-02")
    price(covered, "2024-06-03")
    price(gap, "2024-03-01")
    price(gap, "2024-06-03", source="trade")  # the sale's own price does not cover the gap
    voided = inst("voided")
    trade(voided, "2024-01-02")
    void(db, db.scalars(select(Transaction).order_by(Transaction.id.desc())).first(), "test")  # type: ignore[arg-type]
    db.flush()

    assert settled_positions(db) == {covered.id, lost.id}
    assert voided.id not in {i.id for i in held_instruments(db)}
