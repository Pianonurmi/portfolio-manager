"""Net worth with joint property and debt (NW-01/02, RE-01/02/04, CORE-06)."""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app.modules.networth.loans import schedule
from app.platform.db import today


def _household(c: TestClient) -> dict[str, int]:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    wife = c.post("/api/persons", json={"name": "Wife", "tracking": "shared"}).json()
    half = [{"entity_id": me["entity_id"], "share": "0.5"}, {"entity_id": wife["entity_id"], "share": "0.5"}]
    mine = [{"entity_id": me["entity_id"], "share": "1"}]
    t = today()
    home = c.post(
        "/api/accounts", json={"name": "Home", "kind": "property", "owners": half, "opened_on": "2019-06-01"}
    ).json()
    loan = c.post(
        "/api/accounts", json={"name": "Asuntolaina", "kind": "mortgage", "owners": half, "opened_on": "2019-06-01"}
    ).json()
    yl = c.post("/api/accounts", json={"name": "Yhtiölaina", "kind": "housing_loan", "owners": half}).json()
    svea = c.post("/api/accounts", json={"name": "Svea", "kind": "savings", "owners": mine}).json()
    aot = c.post("/api/accounts", json={"name": "AOT", "kind": "aot", "owners": mine}).json()
    c.post(
        "/api/valuations",
        json={"account_id": home["id"], "date": (t - timedelta(days=100)).isoformat(), "value": "420000"},
    )
    c.post(
        "/api/valuations",
        json={
            "account_id": loan["id"],
            "date": (t - timedelta(days=5)).isoformat(),
            "value": "196800",
            "method": "statement",
        },
    )
    c.post(
        "/api/valuations",
        json={
            "account_id": yl["id"],
            "date": (t - timedelta(days=5)).isoformat(),
            "value": "38400",
            "method": "statement",
        },
    )
    c.post(
        "/api/valuations",
        json={
            "account_id": svea["id"],
            "date": (t - timedelta(days=2)).isoformat(),
            "value": "9400",
            "method": "statement",
        },
    )
    c.post(
        "/api/ledger/transactions",
        json={
            "account_id": aot["id"],
            "date": (t - timedelta(days=50)).isoformat(),
            "kind": "deposit",
            "amount": "10000",
        },
    )
    return {"me": me["entity_id"], "wife": wife["entity_id"], "home": home["id"], "loan": loan["id"], "aot": aot["id"]}


def test_household_and_personal_views(auth_client: TestClient) -> None:
    c = auth_client
    ids = _household(c)
    hh = c.get("/api/networth").json()
    # 420 000 + 9 400 + 10 000 − 196 800 − 38 400
    assert hh["net_worth"]["value"] == "204200.00"
    me = c.get("/api/networth", params={"scope": f"entity:{ids['me']}"}).json()
    # 210 000 + 9 400 + 10 000 − 98 400 − 19 200
    assert me["net_worth"]["value"] == "111800.00"
    groups = {g["id"]: g for g in me["groups"]}
    assert groups["liabilities"]["total"]["value"] == "-117600.00"
    home_item = groups["property"]["items"][0]
    assert home_item["share"] == "0.500000" and home_item["gross"] == "420000.00"
    # explain: net worth -> group -> account -> valuations with ownership share
    tree = c.get(f"/api/explain/{me['net_worth']['ref']}").json()
    assert {n["label"] for n in tree["children"]} == {"Investments", "Cash and savings", "Property", "Liabilities"}
    acct = c.get(f"/api/explain/acct:{ids['home']}:e{ids['me']}:{today().isoformat()}").json()
    assert acct["formula"].endswith("× ownership share")


def test_wife_shared_only_view(auth_client: TestClient) -> None:
    c = auth_client
    ids = _household(c)
    w = c.get("/api/networth", params={"scope": f"entity:{ids['wife']}"}).json()
    assert w["net_worth"]["value"] == "92400.00"  # 210 000 − 98 400 − 19 200


def test_mortgage_schedule_and_estimate(auth_client: TestClient) -> None:
    c = auth_client
    ids = _household(c)
    c.put(
        f"/api/accounts/{ids['loan']}/meta",
        json={
            "meta": {
                "reference_rate": "0.0225",
                "margin": "0.0055",
                "payment_type": "annuity",
                "end_date": "2044-06-01",
                "next_reset": (today() + timedelta(days=20)).isoformat(),
            }
        },
    )
    s = c.get(f"/api/loans/{ids['loan']}/schedule").json()
    assert s["ready"] and s["rate"] == "0.0280"
    first = s["rows"][0]
    assert Decimal(first["interest"]) == (Decimal("196800") * Decimal("0.028") / 12).quantize(Decimal("0.01"))
    att = c.get("/api/attention").json()
    assert any("Rate reset" in a["title"] for a in att)


def test_schedule_types_pay_off() -> None:
    from datetime import date

    for kind in ("annuity", "equal_principal"):
        rows = schedule(Decimal("100000"), Decimal("0.03"), date(2026, 1, 1), date(2036, 1, 1), kind)
        assert rows[-1].balance == 0 and len(rows) == 120
    fixed = schedule(Decimal("100000"), Decimal("0.03"), date(2026, 1, 1), date(2036, 1, 1), "fixed", Decimal("500"))
    assert len(fixed) > 120 and fixed[-1].balance == 0  # 500 €/month: the term stretches past 10 years


def test_history_and_change_decomposition(auth_client: TestClient) -> None:
    c = auth_client
    _household(c)
    h = c.get("/api/networth/history", params={"period": "1Y"}).json()["series"]
    assert h[-1]["net_worth"] == "204200.00"
    ch = c.get("/api/networth/change", params={"period": "1Y"}).json()
    parts = sum(Decimal(ch[k]["value"]) for k in ("contributions", "market", "debt_paydown", "revaluation"))
    assert parts == Decimal(ch["total"]["value"])
    assert Decimal(ch["contributions"]["value"]) == Decimal("10000.00")


def test_attention_flags_missing_owner(auth_client: TestClient) -> None:
    c = auth_client
    c.post("/api/accounts", json={"name": "Orphan", "kind": "bank"})
    assert any(a["title"] == "No owner set: Orphan" for a in c.get("/api/attention").json())


def test_valuation_undo(auth_client: TestClient) -> None:
    c = auth_client
    ids = _household(c)
    v = c.post(
        "/api/valuations", json={"account_id": ids["home"], "date": today().isoformat(), "value": "430000"}
    ).json()
    assert c.get("/api/networth").json()["net_worth"]["value"] == "214200.00"
    c.post(f"/api/undo/{v['undo_id']}")
    assert c.get("/api/networth").json()["net_worth"]["value"] == "204200.00"


def test_export_contains_tables_and_no_secrets(auth_client: TestClient) -> None:
    import io
    import json
    import zipfile

    _household(auth_client)
    r = auth_client.get("/api/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"tables/account.json", "tables/ledger_transaction.json", "tables/valuation.json", "README.txt"} <= names
    assert "tables/app_user.json" not in names and "tables/auth_session.json" not in names
    accounts = json.loads(z.read("tables/account.json"))
    assert {a["name"] for a in accounts} >= {"Home", "Asuntolaina"}


def test_loan_scenarios_and_index_estimate(auth_client: TestClient) -> None:
    c = auth_client
    ids = _household(c)
    c.put(
        f"/api/accounts/{ids['loan']}/meta",
        json={"meta": {"rate": "0.03", "payment_type": "annuity", "end_date": "2044-06-01"}},
    )
    r = c.post(f"/api/loans/{ids['loan']}/simulate", json={"rate_delta": "0", "extra_monthly": "200"}).json()
    assert r["scenario"]["months"] < r["base"]["months"] and Decimal(r["interest_saved"]) > 0
    shock = c.post(f"/api/loans/{ids['loan']}/simulate", json={"rate_delta": "0.02"}).json()
    assert Decimal(shock["scenario"]["total_interest"]) > Decimal(shock["base"]["total_interest"])
    v = c.post(
        "/api/valuations/index-estimate",
        json={
            "account_id": ids["home"],
            "base_date": "2019-06-01",
            "base_value": "395000",
            "index_base": "100",
            "index_now": "106.3",
        },
    ).json()
    assert v["value"] == "419885" and v["method"] == "index"
