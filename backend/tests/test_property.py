"""Property index revaluation (RE-03, Statistics Finland) and housing costs from YNAB categories (RE-07)."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.cashflow.models import BudgetCategory, BudgetTransaction
from app.modules.pricing.providers import statfin


def _meta(variables: dict[str, list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "title": "t",
        "variables": [
            {"code": k, "text": k, "values": [v for v, _ in vals], "valueTexts": [t for _, t in vals]}
            for k, vals in variables.items()
        ],
    }


class FakeStatFin:
    """Serves table metadata and json-stat2 cubes the way pxdata.stat.fi does (dimension codes dated)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append(url)
        if url.endswith("/15it.px"):
            body = _meta(
                {
                    "alue_43_20260625": [("SSS", "KOKO MAA"), ("091-2", "Helsinki 2")],
                    "talotyyppi_5_20111209": [("0", "Yhteensä"), ("3", "Kerrostalot")],
                    "huoneluku_1_20111212": [("00", "Yhteensä"), ("02", "Kaksiot")],
                    "timeperiod_q": [("2019Q4", "2019Q4"), ("2020Q1", "2020Q1"), ("2026Q2", "2026Q2")],
                    "contentscode": [("ashivq_indeksi_2015", "2015=100"), ("ashivq_indeksi_2020", "2020=100")],
                }
            )
        else:
            body = _meta(
                {
                    "timeperiod_q": [(f"2025Q{q}", f"2025Q{q}") for q in (1, 2, 3, 4)],
                    "postinumeroalue_4_20220101": [("00500", "00500  Sörnäinen (Helsinki)")],
                    "talotyyppi_6_20131021": [("2", "Kerrostalo kaksiot")],
                    "contentscode": [("keskihinta_aritm_nw", "€/m2"), ("lkm_julk20", "kaupat")],
                }
            )
        return httpx.Response(200, text=json.dumps(body), request=httpx.Request("GET", url))

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append(url)
        if url.endswith("/15it.px"):
            # dims: alue, talotyyppi, huoneluku, timeperiod (3), contentscode (2) → value[t*2 + c]
            cube = {
                "id": [
                    "alue_43_20260625",
                    "talotyyppi_5_20111209",
                    "huoneluku_1_20111212",
                    "timeperiod_q",
                    "contentscode",
                ],
                "size": [1, 1, 1, 3, 2],
                "dimension": {
                    "alue_43_20260625": {"category": {"index": {"091-2": 0}}},
                    "talotyyppi_5_20111209": {"category": {"index": {"3": 0}}},
                    "huoneluku_1_20111212": {"category": {"index": {"02": 0}}},
                    "timeperiod_q": {"category": {"index": {"2019Q4": 0, "2020Q1": 1, "2026Q2": 2}}},
                    "contentscode": {"category": {"index": {"ashivq_indeksi_2015": 0, "ashivq_indeksi_2020": 1}}},
                },
                "value": [110.0, None, 112.0, 100.0, 123.2, 110.0],
            }
        else:
            cube = {
                "id": ["timeperiod_q", "postinumeroalue_4_20220101", "talotyyppi_6_20131021", "contentscode"],
                "size": [4, 1, 1, 2],
                "dimension": {
                    "timeperiod_q": {"category": {"index": {f"2025Q{q}": q - 1 for q in (1, 2, 3, 4)}}},
                    "postinumeroalue_4_20220101": {"category": {"index": {"00500": 0}}},
                    "talotyyppi_6_20131021": {"category": {"index": {"2": 0}}},
                    "contentscode": {"category": {"index": {"keskihinta_aritm_nw": 0, "lkm_julk20": 1}}},
                },
                "value": [5000, 10, 5200, 30, 4800, 10, 5100, 50],
            }
        return httpx.Response(200, text=json.dumps(cube), request=httpx.Request(method, url))


@pytest.fixture
def fake_statfin(monkeypatch: pytest.MonkeyPatch) -> FakeStatFin:
    fake = FakeStatFin()
    monkeypatch.setattr(statfin, "_client", lambda client: client or fake)
    return fake


def _property(c: TestClient, meta: dict[str, Any]) -> int:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts",
        json={"name": "Home", "kind": "property", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    c.put(f"/api/accounts/{a['id']}/meta", json={"meta": meta})
    return int(a["id"])


def test_region_index_uses_longest_chained_series(fake_statfin: FakeStatFin) -> None:
    r = statfin.region_index("091-2", "3", "02")
    # the 2015 base covers 2019Q4, the 2020 base does not: the longer one wins
    assert r["points"] == [("2019Q4", Decimal("110.0")), ("2020Q1", Decimal("112.0")), ("2026Q2", Decimal("123.2"))]
    assert r["label"].startswith("Helsinki 2") and "2015=100" in r["label"]


def test_postal_prices_smoothed_and_area_value(auth_client: TestClient, fake_statfin: FakeStatFin) -> None:
    c = auth_client
    pid = _property(c, {"index_source": "postal", "postal_code": "00500", "postal_house_type": "2", "area_m2": "50"})
    r = c.get(f"/api/property/{pid}/index").json()
    assert r["unit"] == "eur_m2"
    last = r["points"][-1]
    # trade-weighted 4-quarter average: (5000×10 + 5200×30 + 4800×10 + 5100×50) / 100 = 5090
    assert last["period"] == "2025Q4" and Decimal(last["value"]) == Decimal("5090.00") and last["trades"] == 50
    assert r["estimate"]["market_m2_value"] == "254500"
    assert r["estimate"]["missing"]  # no purchase price yet


def test_index_valuation_from_purchase(auth_client: TestClient, fake_statfin: FakeStatFin) -> None:
    c = auth_client
    pid = _property(
        c,
        {
            "index_source": "region",
            "index_region": "091-2",
            "index_house_type": "3",
            "index_rooms": "02",
            "purchase_price": "300000",
            "purchase_date": "2019-11-15",
        },
    )
    e = c.get(f"/api/property/{pid}/index", params={"refresh": True}).json()["estimate"]
    assert e["value"] == "336000" and e["base"]["period"] == "2019Q4"  # 300 000 × 123,2 / 110
    v = c.post(f"/api/property/{pid}/index/valuation").json()
    assert v["value"] == "336000" and v["date"] == "2026-06-30"
    # posting again for the same quarter updates instead of duplicating
    c.post(f"/api/property/{pid}/index/valuation")
    vals = c.get("/api/valuations", params={"account_id": pid}).json()
    assert [x["method"] for x in vals] == ["index"]


def test_housing_costs_from_ynab_categories(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    pid = _property(c, {"area_m2": "50"})
    for cid, name in [("c1", "Hoitovastike"), ("c2", "Rahoitusvastike"), ("c3", "Sähkö"), ("c4", "Vuokratulo")]:
        db.add(BudgetCategory(id=cid, group_name="Asunto", name=name))
    n = 0
    for m in range(1, 13):
        for cid, amt in [("c1", "-250"), ("c2", "-150"), ("c3", "-40"), ("c4", "900")]:
            n += 1
            db.add(
                BudgetTransaction(
                    id=f"t{n}", date=date(2025, m, 5), amount=Decimal(amt), account_id="a", category_id=cid
                )
            )
    db.commit()
    c.post("/api/valuations", json={"account_id": pid, "date": "2025-12-31", "value": "200000", "method": "manual"})
    r = c.put(
        f"/api/property/{pid}/costs",
        json={"cost_categories": {"c1": "vastike", "c2": "rahoitusvastike", "c3": "utilities"}, "rent_category": "c4"},
    )
    assert r.status_code == 200, r.text
    from app.modules.core.models import Account
    from app.modules.networth.property import costs

    db.expire_all()
    acct = db.get(Account, pid)
    assert acct is not None
    l12 = costs(db, acct, months=13, until=date(2026, 1, 15))["last12"]
    assert l12["by_type"] == {"vastike": Decimal(3000), "rahoitusvastike": Decimal(1800), "utilities": Decimal(480)}
    # rahoitusvastike repays the housing company loan: not a running cost
    assert l12["running"] == Decimal(3480) and l12["monthly_running"] == Decimal("290.00")
    assert l12["per_m2_month"] == Decimal("5.80") and l12["rent"] == Decimal(10800)
    assert l12["net_yield"] == pytest.approx((10800 - 3480) / 200000)
    assert c.put(f"/api/property/{pid}/costs", json={"cost_categories": {"c1": "bogus"}}).status_code == 422
