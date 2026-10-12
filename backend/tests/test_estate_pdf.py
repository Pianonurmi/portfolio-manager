"""Encrypted emergency package (EST-02): PDF is AES-encrypted, scheduled copies are pruned, no secrets inside."""

from pathlib import Path

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.life import jobs as estate_jobs
from app.platform.config import get_settings
from app.platform.jobs import JobContext
from app.platform.models import AppUser

PASSWORD = "a long enough passphrase"


def _seed(c: TestClient) -> None:
    me = c.post("/api/persons", json={"name": "Zeppelin Testhenkilo", "is_self": True}).json()
    c.post(
        "/api/accounts",
        json={
            "name": "Zorbo Savings",
            "kind": "savings",
            "external_ref": "FI4950009420028730",
            "owners": [{"entity_id": me["entity_id"], "share": "1"}],
        },
    )
    c.post("/api/policies", json={"kind": "life", "insurer": "Quuxvakuutus Oy", "beneficiaries": "Spouse"})
    c.post(
        "/api/estate",
        json={
            "section": "location",
            "title": "Qwertyvault",
            "body": "Passwords: in the fireproof box at Äidinkatu 5 €",
        },
    )


def _text(pdf: bytes, password: str) -> str:
    doc = pdfium.PdfDocument(pdf, password=password)
    return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


def test_pdf_is_encrypted_and_has_the_package(auth_client: TestClient) -> None:
    c = auth_client
    _seed(c)
    r = c.post("/api/estate/package.pdf", json={"password": PASSWORD})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    raw = r.content
    assert raw.startswith(b"%PDF") and b"/Encrypt" in raw and b"/AESV3" in raw
    for plain in (b"Qwertyvault", b"Quuxvakuutus", b"Zorbo", b"Zeppelin", b"FI4950009420028730"):
        assert plain not in raw
    with pytest.raises(pdfium.PdfiumError):
        pdfium.PdfDocument(raw, password="wrong password!!")
    text = _text(raw, PASSWORD)
    assert all(
        w in text for w in ("Qwertyvault", "Quuxvakuutus Oy", "Zorbo Savings", "FI4950009420028730", "Äidinkatu")
    )
    assert "€" in text  # a Unicode font, not Latin-1
    # same content as the HTML package
    html = c.get("/api/estate/package").text
    assert all(w in html for w in ("Qwertyvault", "Quuxvakuutus Oy", "Zorbo Savings"))


def test_password_minimum_and_audit_has_no_password(auth_client: TestClient) -> None:
    c = auth_client
    assert c.post("/api/estate/package.pdf", json={"password": "short"}).status_code == 422
    assert c.post("/api/estate/package.pdf", json={}).status_code == 422
    assert c.post("/api/estate/package.pdf", json={"password": PASSWORD}).status_code == 200
    audit = str(c.get("/api/audit").json()) if c.get("/api/audit").status_code == 200 else ""
    assert PASSWORD not in audit


def test_secrets_never_in_the_package(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    _seed(c)
    # stored credentials and the stored passphrase must stay out of both renderings
    c.put("/api/estate/package-settings", json={"enabled": False, "passphrase": "stored passphrase 123"})
    user = db.query(AppUser).one()
    user.totp_secret_enc = "JBSWY3DPEHPK3PXP"
    db.commit()
    pdf = c.post("/api/estate/package.pdf", json={"password": PASSWORD}).content
    text = _text(pdf, PASSWORD)
    html = c.get("/api/estate/package").text
    for secret in ("JBSWY3DPEHPK3PXP", "stored passphrase 123", "correct horse battery", user.password_hash, PASSWORD):
        assert secret not in text and secret not in html
    assert (
        c.post("/api/estate", json={"section": "instruction", "title": "x", "body": "salasana: 1"}).status_code == 422
    )


def test_passphrase_is_stored_encrypted_and_never_returned(auth_client: TestClient, db: Session) -> None:
    c = auth_client
    assert c.put("/api/estate/package-settings", json={"enabled": True}).status_code == 422  # no passphrase yet
    r = c.put("/api/estate/package-settings", json={"enabled": True, "passphrase": "stored passphrase 123", "keep": 2})
    assert r.status_code == 200 and r.json()["has_passphrase"] is True and "stored passphrase 123" not in str(r.json())
    db.expire_all()
    stored = db.query(AppUser).one().settings["estate_package"]
    assert "stored passphrase 123" not in str(stored) and stored["passphrase_enc"].startswith("gAAAA")
    assert c.put("/api/estate/package-settings", json={"enabled": True, "keep": 2}).json()["has_passphrase"] is True
    assert c.put("/api/estate/package-settings", json={"path_dir": "../x"}).status_code == 422
    assert c.get("/api/estate/package-settings").json()["keep"] == 2


def test_job_writes_prunes_and_records(
    auth_client: TestClient, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = auth_client
    _seed(c)
    ctx = JobContext(db=db)
    estate_jobs.refresh(ctx)  # nothing configured: does nothing
    assert "not enabled" in (ctx.summary or "")
    c.put(
        "/api/estate/package-settings",
        json={"enabled": True, "path_dir": str(tmp_path), "keep": 2, "passphrase": "stored passphrase 123"},
    )
    for old in ("2026-05", "2026-06", "2026-07"):
        (tmp_path / f"estate-package-{old}.pdf").write_bytes(b"old")
    (tmp_path / "notes.txt").write_text("keep me")
    db.expire_all()
    ctx = JobContext(db=db)
    estate_jobs.refresh(ctx)
    db.commit()
    names = sorted(p.name for p in tmp_path.iterdir())
    month = estate_jobs.today().strftime("%Y-%m")
    assert f"estate-package-{month}.pdf" in names and "notes.txt" in names
    assert len([n for n in names if n.startswith("estate-package-")]) == 2  # newest two of the monthly files
    pdf = (tmp_path / f"estate-package-{month}.pdf").read_bytes()
    assert b"/Encrypt" in pdf and b"Qwertyvault" not in pdf and "Qwertyvault" in _text(pdf, "stored passphrase 123")
    s = c.get("/api/estate/package-settings").json()
    assert s["last"]["file"] == f"estate-package-{month}.pdf" and s["effective_dir"] == str(tmp_path)
    monkeypatch.setattr(get_settings(), "imports_dir", str(tmp_path / "imp"))
    assert c.put("/api/estate/package-settings", json={"enabled": True, "keep": 3}).json()["effective_dir"] == str(
        tmp_path / "imp" / "estate"
    )


def test_without_a_unicode_font_the_package_still_renders(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.life import estate

    _seed(auth_client)
    monkeypatch.setattr(estate, "_font_file", lambda bold=False: None)
    pdf = auth_client.post("/api/estate/package.pdf", json={"password": PASSWORD}).content
    assert "Qwertyvault" in _text(pdf, PASSWORD)
