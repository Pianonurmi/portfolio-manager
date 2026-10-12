from fastapi.testclient import TestClient


def _people(c: TestClient) -> dict[str, dict]:
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    wife = c.post("/api/persons", json={"name": "Wife", "tracking": "shared"}).json()
    kid = c.post("/api/persons", json={"name": "Child A", "role": "child", "birth_date": "2016-05-01"}).json()
    return {"me": me, "wife": wife, "kid": kid}


def test_people_get_entities(auth_client: TestClient) -> None:
    p = _people(auth_client)
    ents = auth_client.get("/api/entities").json()
    assert {e["id"] for e in ents} == {p["me"]["entity_id"], p["wife"]["entity_id"], p["kid"]["entity_id"]}


def test_joint_account_ownership(auth_client: TestClient) -> None:
    c = auth_client
    p = _people(c)
    inst = c.post("/api/institutions", json={"name": "Nordea", "kind": "bank"}).json()
    body = {
        "name": "Asuntolaina",
        "kind": "mortgage",
        "institution_id": inst["id"],
        "opened_on": "2019-06-01",
        "owners": [
            {"entity_id": p["me"]["entity_id"], "share": "0.5"},
            {"entity_id": p["wife"]["entity_id"], "share": "0.5"},
        ],
    }
    acct = c.post("/api/accounts", json=body).json()
    assert len(acct["owners"]) == 2 and acct["institution"] == "Nordea"

    bad = dict(body, owners=[{"entity_id": p["me"]["entity_id"], "share": "0.6"}])
    assert c.post("/api/accounts", json=bad).status_code == 422

    # change shares from a date: history is kept
    upd = dict(
        body,
        owners=[
            {"entity_id": p["me"]["entity_id"], "share": "0.6"},
            {"entity_id": p["wife"]["entity_id"], "share": "0.4"},
        ],
        owners_from="2024-01-01",
    )
    out = c.put(f"/api/accounts/{acct['id']}", json=upd).json()
    assert sorted(o["share"] for o in out["owners"]) == ["0.400000", "0.600000"]

    tree = c.get(f"/api/explain/owners:{acct['id']}").json()
    assert {ch["value"] for ch in tree["children"]} == {"60 %", "40 %"}


def test_instruments_and_search(auth_client: TestClient) -> None:
    c = auth_client
    r = c.post(
        "/api/instruments",
        json={
            "type": "stock",
            "name": "Sampo Oyj A",
            "isin": "FI4000552500",
            "symbol": "SAMPO",
            "mic": "XHEL",
            "price_symbol": "SAMPO.HE",
        },
    )
    assert r.status_code == 201
    assert c.post("/api/instruments", json={"type": "stock", "name": "dup", "isin": "FI4000552500"}).status_code == 409
    assert c.post("/api/instruments", json={"type": "stock", "name": "bad", "isin": "XX123"}).status_code == 422
    hits = c.get("/api/search", params={"q": "samp"}).json()
    assert hits[0]["kind"] == "instrument" and hits[0]["title"] == "Sampo Oyj A"


def test_unknown_account_kind(auth_client: TestClient) -> None:
    assert auth_client.post("/api/accounts", json={"name": "x", "kind": "nope"}).status_code == 422
