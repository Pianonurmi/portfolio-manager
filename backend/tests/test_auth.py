from fastapi.testclient import TestClient


def test_first_run_setup_then_sign_in(client: TestClient) -> None:
    assert client.get("/api/auth/status").json()["setup_required"] is True
    r = client.post("/api/auth/setup", json={"username": "owner", "password": "correct horse battery"})
    assert r.status_code == 201
    assert client.get("/api/auth/me").json()["username"] == "owner"
    # setup cannot run twice
    assert (
        client.post("/api/auth/setup", json={"username": "other", "password": "another long pass"}).status_code == 409
    )

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401
    bad = client.post("/api/auth/login", json={"username": "owner", "password": "wrong password!!"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
    assert ok.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_short_password_rejected(client: TestClient) -> None:
    assert client.post("/api/auth/setup", json={"username": "owner", "password": "short"}).status_code == 422


def test_mutations_require_csrf_header(auth_client: TestClient) -> None:
    r = auth_client.post("/api/persons", json={"name": "Me"}, headers={"X-PM-Request": ""})
    assert r.status_code == 403


def test_every_api_route_requires_auth(client: TestClient) -> None:
    from app.main import app

    open_paths = {
        "/healthz",
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/openapi.json",
        "/api/docs",
        "/api/docs/oauth2-redirect",
        "/api/redoc",
    }
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path in open_paths or not path.startswith("/api"):
            continue
        url = path.replace("{ref:path}", "x").replace("{", "").replace("}", "")
        for m in methods - {"HEAD", "OPTIONS"}:
            r = client.request(m, url, json={})
            assert r.status_code == 401, f"{m} {path} -> {r.status_code}"


def test_totp_flow(auth_client: TestClient) -> None:
    import pyotp

    s = auth_client.post("/api/auth/totp/setup").json()
    code = pyotp.TOTP(s["secret"]).now()
    assert auth_client.post("/api/auth/totp/enable", json={"secret": s["secret"], "code": code}).status_code == 204
    auth_client.post("/api/auth/logout")
    auth_client.cookies.clear()
    r = auth_client.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
    assert r.status_code == 401 and "authenticator" in r.json()["detail"]
    r = auth_client.post(
        "/api/auth/login",
        json={"username": "owner", "password": "correct horse battery", "totp": pyotp.TOTP(s["secret"]).now()},
    )
    assert r.status_code == 200
