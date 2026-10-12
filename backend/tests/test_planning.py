from fastapi.testclient import TestClient

from app.modules.planning import engine

BASE = {
    "age_now": 38,
    "start_wealth": 186000,
    "monthly_savings": 1500,
    "retire_age": 55,
    "real_return": 0.045,
    "volatility": 0.14,
    "monthly_spending": 3200,
    "pension_monthly": 2350,
    "pension_age": 65,
}


def test_reproducible_and_monotonic() -> None:
    p = engine.Params.from_dict(BASE)
    a, b = engine.simulate(p, 800, seed=7), engine.simulate(p, 800, seed=7)
    assert a == b  # same inputs + seed -> same result
    more_spend = engine.simulate(engine.Params.from_dict({**BASE, "monthly_spending": 5000}), 800, seed=7)
    assert more_spend["success"] < a["success"]
    inherit = engine.simulate(
        engine.Params.from_dict(
            {
                **BASE,
                "monthly_spending": 5000,
                "inheritances": [{"age": 58, "amount_after_tax": 300000, "probability": 1}],
            }
        ),
        800,
        seed=7,
    )
    assert inherit["success"] > more_spend["success"]
    later = engine.earliest_retirement(p, paths=300)
    assert later is None or 40 <= later <= 70


def test_zero_volatility_matches_closed_form() -> None:
    p = engine.Params.from_dict(
        {**BASE, "volatility": 0.0, "real_return": 0.0, "retire_age": 39, "monthly_spending": 0, "pension_monthly": 0}
    )
    path = engine.deterministic(p)
    assert path[1] == 186000 + 18000  # one year of saving, no growth


def test_pension_estimate_adjustments() -> None:
    at_target = engine.pension_monthly(1000, 50000, 0.015, 38, 67, 67, 0.94, 0.0)
    assert round(at_target) == round((1000 + 29 * 50000 * 0.015 / 12) * 0.94)
    early = engine.pension_monthly(1000, 50000, 0.015, 38, 65, 67, 0.94, 0.0)
    assert early < at_target  # fewer accrual years and −0,4 %/month


def test_api_base_simulate_and_scenarios(auth_client: TestClient) -> None:
    c = auth_client
    c.post("/api/persons", json={"name": "Alex", "is_self": True, "birth_date": "1988-04-12"})
    b = c.get("/api/plan/base").json()
    assert b["params"]["age_now"] >= 38
    r = c.post("/api/plan/simulate", json={"params": {**b["params"], "start_wealth": 200000}, "paths": 500}).json()
    assert 0 <= r["success"] <= 1 and len(r["p50"]) == len(r["ages"])
    sid = c.post("/api/plan/scenarios", json={"name": "Baseline", "params": b["params"], "is_base": True}).json()["id"]
    assert c.get("/api/plan/scenarios").json()[0]["id"] == sid
    c.put(
        "/api/plan/pension",
        json={
            "accrued_monthly": 900,
            "statement_year": 2025,
            "annual_earnings": 60000,
            "accrual_rate": 0.015,
            "target_age": 67,
            "life_coef": 0.94,
            "tax_rate": 0.25,
        },
    )
    curve = c.get("/api/plan/pension/curve").json()
    assert curve[0]["monthly_after_tax"] < curve[-1]["monthly_after_tax"]
