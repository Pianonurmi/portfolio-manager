import json
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.documents.api import search


class FakePaperless:
    def get(self, url: str, params: Any = None, headers: Any = None) -> httpx.Response:
        assert headers["Authorization"] == "Token ptok" and params["query"] == "isännöitsijä"
        body = {"results": [{"id": 1842, "title": "Isännöitsijätodistus 2026", "created_date": "2026-03-02"}]}
        return httpx.Response(200, text=json.dumps(body), request=httpx.Request("GET", url))


def test_paperless_search_links_tags_notes(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    c.put("/api/connections/paperless", json={"config": {"url": "http://paperless.lan"}, "secret": "ptok"})
    me = c.post("/api/persons", json={"name": "Alex", "is_self": True}).json()
    a = c.post(
        "/api/accounts",
        json={"name": "Home", "kind": "property", "owners": [{"entity_id": me["entity_id"], "share": "1"}]},
    ).json()
    res = search(None, db, "isännöitsijä", client=FakePaperless())  # type: ignore[arg-type]
    assert res[0]["url"] == "http://paperless.lan/documents/1842/details"
    c.post(
        "/api/documents/links",
        json={"subject_type": "account", "subject_id": a["id"], "paperless_id": 1842, "title": res[0]["title"]},
    )
    c.post(
        "/api/documents/links",
        json={"subject_type": "account", "subject_id": a["id"], "paperless_id": 1842, "title": res[0]["title"]},
    )
    links = c.get("/api/documents/links", params={"subject_type": "account", "subject_id": a["id"]}).json()
    assert len(links) == 1 and links[0]["url"].endswith("/1842/details")
    c.post("/api/documents/tags", json={"subject_type": "account", "subject_id": a["id"], "name": "Koti"})
    assert c.get("/api/documents/tags", params={"subject_type": "account", "subject_id": a["id"]}).json() == ["koti"]
    c.post(
        "/api/documents/notes", json={"subject_type": "account", "subject_id": a["id"], "text": "Putkiremontti 2031"}
    )
    hits = c.get("/api/search", params={"q": "putki"}).json()
    assert any(h["kind"] == "note" for h in hits)
    assert c.post("/api/documents/tags", json={"subject_type": "nope", "subject_id": 1, "name": "x"}).status_code == 422
