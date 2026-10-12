"""Notification channels: Gotify (HTTP, LAN) and SMTP (local mail server) (ALERT-01)."""

import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.alerts.models import AlertLog
from app.platform.connections import get_connection, secret
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient


def gotify(db: Session, title: str, body: str, priority: int = 5, client: Any | None = None) -> bool:
    c = get_connection(db, "gotify")
    if c is None or not c.secret_enc or not (c.config or {}).get("url"):
        return False
    url = str(c.config["url"]).rstrip("/")
    cl = client or OutboundClient()
    r = cl.request(
        "POST",
        f"{url}/message",
        params={"token": secret(c)},
        json={"title": title, "message": body, "priority": priority},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Gotify answered {r.status_code}")
    return True


def email(db: Session, title: str, body: str, smtp_factory: Any = None) -> bool:
    c = get_connection(db, "smtp")
    cfg = (c.config or {}) if c else {}
    if c is None or not cfg.get("host") or not cfg.get("to"):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[Portfolio Manager] {title}"
    msg["From"] = cfg.get("from") or cfg["to"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    port = int(cfg.get("port") or 587)
    factory = smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)
    with factory(cfg["host"], port, timeout=15) as s:
        if port == 587 and hasattr(s, "starttls"):
            s.starttls(context=ssl.create_default_context())
        if cfg.get("username"):
            s.login(cfg["username"], secret(c) or "")
        s.send_message(msg)
    return True


def send(db: Session, key: str, title: str, body: str, quiet_hours: int = 24 * 7, priority: int = 5) -> bool:
    """Send once per key per quiet period, to every configured channel. Returns True if anything was sent."""
    since = utcnow() - timedelta(hours=quiet_hours)
    if db.scalar(
        select(AlertLog.id).where(AlertLog.key == key, AlertLog.sent_at >= since, AlertLog.error.is_(None)).limit(1)
    ):
        return False
    channels, errors = [], []

    def _g() -> bool:
        return bool(gotify(db, title, body, priority))

    def _e() -> bool:
        return bool(email(db, title, body))

    for name, fn in (("gotify", _g), ("email", _e)):
        try:
            if fn():
                channels.append(name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    if channels or errors:
        db.add(
            AlertLog(
                key=key,
                title=title,
                body=body,
                channels=",".join(channels),
                sent_at=utcnow(),
                error="; ".join(errors) or None,
            )
        )
    return bool(channels)
