"""Cash: ECB rate parsing, default derivation, own offers, and the interest ladder maths."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.cash import jobs, ladder, service
from app.platform.jobs import JobContext
from tests.test_pricing import FakeClient

FIX = Path("/srv/fixtures/ecb")


def csv(rows: list[tuple[str, float]]) -> str:
    return "KEY,TIME_PERIOD,OBS_VALUE\n" + "".join(f"X,{t},{v}\n" for t, v in rows)


def routes(**override: str | None) -> dict[str, str]:
    base = {
        service.OVERNIGHT: csv([("2026-07", 0.36), ("2026-08", 0.38)]),
        service.UP_TO_1Y: csv([("2026-07", 2.54), ("2026-08", 2.53)]),
        service.OVER_1Y_UP_TO_2Y: csv([("2026-08", 2.92)]),
        service.EURIBOR[3]: csv([("2026-08", 2.64)]),
        service.EURIBOR[6]: csv([("2026-08", 2.92)]),
        service.EURIBOR[12]: csv([("2026-08", 3.25)]),
        service.AAA_2Y: csv([("2026-10-08", 3.0), ("2026-10-09", 3.05)]),
        service.ESTR: csv([("2026-10-08", 2.43), ("2026-10-09", 2.44)]),
    }
    return {service.BASE + k: v for k, v in {**base, **override}.items() if v is not None}


class FailingClient(FakeClient):
    def get(self, url: str, **kw: Any) -> httpx.Response:
        if service.EURIBOR[6] in url:
            raise httpx.ConnectError("boom")
        return super().get(url, **kw)


def load(db: Session, **override: str | None) -> dict[str, int]:
    stats = service.refresh(db, FakeClient(routes(**override)))  # type: ignore[arg-type]
    db.commit()
    return stats


def test_parse_real_ecb_csv() -> None:
    rows = service.parse((FIX / "mir_l22f.csv").read_text())
    assert rows == [(date(2026, 6, 1), 0.0249), (date(2026, 7, 1), 0.0254), (date(2026, 8, 1), 0.0253)]
    assert service.parse(csv([("2026-10-09", 2.44)])) == [(date(2026, 10, 9), 0.0244)]


def test_defaults_use_euribor_curve_minus_finnish_margin(db: Session) -> None:
    assert load(db) == {"series": 8, "rows": 12, "failed": 0}
    d = service.defaults(service.latest(db))
    mean = (0.0264 + 0.0292 + 0.0325) / 3
    margin = mean - 0.0253
    assert d["savings"]["rate"] == pytest.approx(0.0038)
    assert d["3m"]["rate"] == pytest.approx(0.0264 - margin)
    assert d["6m"]["rate"] == pytest.approx(0.0292 - margin)
    assert d["12m"]["rate"] == pytest.approx(0.0325 - margin)
    assert d["24m"]["rate"] == pytest.approx(0.0292)
    assert d["3m"]["as_of"] == date(2026, 8, 1)
    assert d["3m"]["rate"] < d["6m"]["rate"] < d["12m"]["rate"]


def test_refresh_is_upsert_and_survives_a_failing_series(db: Session) -> None:
    load(db)
    load(db, **{service.ESTR: csv([("2026-10-09", 2.50)])})
    assert service.latest(db)[service.ESTR]["value"] == pytest.approx(0.025)
    assert len(service.latest(db)) == 8
    warns: list[str] = []
    stats = service.refresh(db, FailingClient(routes()), warns.append)  # type: ignore[arg-type]
    assert stats["failed"] == 1 and stats["series"] == 7
    assert len(warns) == 1 and "Euribor 6 months" in warns[0]


def test_job_warns_and_continues(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "OutboundClient", lambda: _Ctx(FailingClient(routes())))
    ctx = JobContext(db=db)
    jobs.rates_refresh(ctx)
    assert ctx.status == "partial" and "1 failed" in (ctx.summary or "")
    assert len(service.latest(db)) == 7


class _Ctx:
    def __init__(self, c: Any) -> None:
        self.c = c

    def __enter__(self) -> Any:
        return self.c

    def __exit__(self, *a: object) -> None:
        return None


def test_fallbacks_never_crash(db: Session) -> None:
    assert service.defaults({}) == {}
    load(
        db,
        **{service.OVERNIGHT: None, service.UP_TO_1Y: None, service.OVER_1Y_UP_TO_2Y: None, service.EURIBOR[6]: None},
    )
    d = service.defaults(service.latest(db))
    assert d["savings"]["rate"] == pytest.approx(0.0244) and "€STR" in d["savings"]["label"]
    assert d["12m"]["rate"] == pytest.approx(0.0325 - 0.005)
    assert "unavailable" in d["3m"]["label"] and "6m Euribor missing" in d["6m"]["label"]
    assert d["24m"]["rate"] == pytest.approx(0.0305 - 0.005)


def test_rates_api_and_overrides(auth_client: TestClient, db: Session) -> None:
    empty = auth_client.get("/api/cash/rates").json()
    assert empty["products"]["savings"]["rate"] == 0 and empty["updated_at"] is None
    load(db)
    r = auth_client.get("/api/cash/rates").json()
    assert r["products"]["savings"]["source"] == "market" and r["products"]["24m"]["as_of"] == "2026-08-01"
    assert len(r["series"]) == 8 and r["updated_at"]
    r = auth_client.put(
        "/api/cash/rates", json={"products": {"savings": {"rate": 0.0275, "bank": "Aktia"}, "12m": {"rate": 0.031}}}
    ).json()
    s = r["products"]["savings"]
    assert (s["rate"], s["source"], s["bank"], s["market_rate"]) == (0.0275, "yours", "Aktia", pytest.approx(0.0038))
    assert r["products"]["12m"]["source"] == "yours" and r["products"]["6m"]["source"] == "market"
    r = auth_client.put("/api/cash/rates", json={"products": {"savings": {"rate": None}}}).json()
    assert r["products"]["savings"]["source"] == "market" and r["products"]["12m"]["rate"] == 0.031
    assert auth_client.put("/api/cash/rates", json={"products": {"12m": {"rate": 0.3}}}).status_code == 422
    assert auth_client.put("/api/cash/rates", json={"products": {"5y": {"rate": 0.01}}}).status_code == 422


def test_cash_return_prefers_own_savings_rate(auth_client: TestClient, db: Session) -> None:
    from sqlalchemy import select

    from app.platform.models import AppUser

    load(db)
    user = db.scalars(select(AppUser)).first()
    assert user is not None
    assert service.cash_return(db, user) == pytest.approx(0.0038)
    auth_client.put("/api/cash/rates", json={"products": {"savings": {"rate": 0.02}}})
    db.expire_all()
    assert service.cash_return(db, user) == 0.02


def test_refresh_endpoint_enqueues(auth_client: TestClient) -> None:
    assert auth_client.post("/api/cash/rates/refresh").json()["run_id"] > 0


RATES = {"savings": 0.01, "3m": 0.02, "6m": 0.025, "12m": 0.03, "24m": 0.035}
START = date(2026, 1, 31)


def build(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "total": 10000, "liquid": 0, "rungs": [{"tenor": 12}], "horizon": 12, "rollover": "same",
        "rates": RATES, "tax": 0.30, "start": START,
    }  # fmt: skip
    return ladder.build_ladder(**{**args, **kw})


def test_twelve_month_deposit_interest_and_tax() -> None:
    r = build()
    assert r["rungs"][0]["interest_gross"] == 300.0 and r["rungs"][0]["interest_net"] == 210.0
    last = r["months"][-1]
    assert last["interest_gross"] == 300.0 and last["interest_net"] == 210.0 and last["maturing"] == 10210.0
    t = r["totals"]
    assert (t["gross_interest"], t["tax"], t["net_interest"], t["final_value"]) == (300.0, 90.0, 210.0, 10210.0)
    assert t["effective_net_yield"] == pytest.approx(0.021)
    assert r["rungs"][0]["maturity"] == "2027-01-31"


def test_equal_ladder_dates_and_rollover_into_longest() -> None:
    r = build(rungs=[{"tenor": t} for t in (3, 6, 12, 24)], rollover="longest", horizon=36, total=10000)
    assert [(x["months"], x["amount"]) for x in r["rungs"]] == [(3, 2500.0), (6, 2500.0), (12, 2500.0), (24, 2500.0)]
    assert [x["maturity"] for x in r["rungs"]] == ["2026-04-30", "2026-07-31", "2027-01-31", "2028-01-31"]
    by = {m["month"]: m for m in r["months"]}
    maturing = [m for m, row in by.items() if row["maturing"] > 0]
    # 3m, 6m, 12m rungs mature once and are re-deposited for 24 months: months 3, 6, 12, then 24, 27, 30, 36
    assert maturing == [3, 6, 12, 24, 27, 30, 36]
    assert by[3]["maturing"] == pytest.approx(2500 + 2500 * 0.02 * 3 / 12 * 0.7, abs=0.01)
    assert r["available"]["next"][0]["date"] == "2026-04-30" and len(r["available"]["next"]) == 6


def test_same_and_savings_rollover() -> None:
    same = build(rungs=[{"tenor": 6}], horizon=24, rollover="same")
    assert [m["month"] for m in same["months"] if m["maturing"]] == [6, 12, 18, 24]
    sav = build(rungs=[{"tenor": 6}], horizon=24, rollover="savings")
    assert [m["month"] for m in sav["months"] if m["maturing"]] == [6]
    assert sav["months"][6]["deposits"] == 0 and sav["months"][-1]["savings"] > 10000


def test_liquid_part_stays_in_savings() -> None:
    r = build(liquid=4000, rungs=[{"tenor": 12}], rollover="longest")
    assert r["rungs"][0]["amount"] == 6000.0 and r["available"]["now"] == 4000.0
    first = r["months"][0]
    assert first["savings"] == pytest.approx(4000 + 4000 * 0.01 / 12 * 0.7, abs=0.01)
    assert r["totals"]["net_interest"] == pytest.approx(
        6000 * 0.03 * 0.7 + 4000 * ((1 + 0.01 / 12 * 0.7) ** 12 - 1), abs=0.02
    )
    only = build(liquid=10000, rungs=None)
    assert only["rungs"] == [] and only["months"][-1]["deposits"] == 0


def test_shares_are_normalised_and_cents_add_up() -> None:
    r = build(total=100, rungs=[{"tenor": 3, "share": 1}, {"tenor": 6, "share": 1}, {"tenor": 12, "share": 1}])
    assert [x["amount"] for x in r["rungs"]] == [33.33, 33.33, 33.34]
    r = build(rungs=[{"tenor": 3, "share": 30}, {"tenor": 12, "share": 70}])
    assert [x["amount"] for x in r["rungs"]] == [3000.0, 7000.0]


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"total": -5}, "above zero"),
        ({"liquid": -1}, "negative"),
        ({"liquid": 20000}, "larger than the total"),
        ({"rungs": [{"tenor": 7}]}, "Unknown term 7"),
        ({"rungs": [{"tenor": 3}, {"tenor": 3}]}, "only once"),
        ({"rungs": [{"tenor": 3, "share": 0}]}, "more than zero"),
        ({"rungs": []}, "at least one"),
        ({"horizon": 121}, "horizon"),
        ({"rollover": "x"}, "rollover"),
    ],
)
def test_ladder_validation(kw: dict[str, Any], msg: str) -> None:
    with pytest.raises(ladder.LadderError, match=msg):
        build(**kw)


def test_comparison_ordering_and_api(auth_client: TestClient, db: Session) -> None:
    load(db)
    body = {"amount": 50000, "liquid": 5000, "horizon": 36, "rollover": "longest"}
    r = auth_client.post("/api/cash/ladder", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["inputs"]["tax_rate"] == 0.3 and out["inputs"]["rates"]["24m"] == pytest.approx(0.0292)
    labels = [c["label"] for c in out["comparison"]]
    assert labels[0] == "This ladder" and len(labels) == 4
    sav, y1, y2 = (c["net_interest"] for c in out["comparison"][1:])
    assert sav < y1 < y2  # market curve rises with the term
    assert sav < out["totals"]["net_interest"] < y2
    # explicit rates win
    flat = auth_client.post("/api/cash/ladder", json={**body, "rates": {"savings": 0.05}}).json()
    assert flat["comparison"][1]["net_interest"] > y2
    for bad in ({"amount": -1}, {"amount": 100, "liquid": 200}, {"amount": 100, "rungs": [{"tenor": 5}]}):
        assert auth_client.post("/api/cash/ladder", json=bad).status_code == 422
