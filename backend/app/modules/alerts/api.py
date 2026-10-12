from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.modules.alerts.models import AlertLog
from app.modules.alerts.notify import email, gotify
from app.platform.auth import CurrentUser, Db

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertSettings(BaseModel):
    attention: bool = True
    dividends: bool = True
    large_move_pct: float = 3.0


@router.get("/settings")
def get_settings_(user: CurrentUser) -> dict[str, Any]:
    return AlertSettings(**(user.settings or {}).get("alerts", {})).model_dump()


@router.put("/settings")
def put_settings(body: AlertSettings, user: CurrentUser) -> dict[str, Any]:
    user.settings = {**(user.settings or {}), "alerts": body.model_dump()}
    return body.model_dump()


@router.post("/test")
def test(_: CurrentUser, db: Db) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def _g() -> bool:
        return bool(gotify(db, "Test from Portfolio Manager", "Notifications work."))

    def _e() -> bool:
        return bool(email(db, "Test", "Notifications work."))

    for name, fn in (("gotify", _g), ("email", _e)):
        try:
            out[name] = "sent" if fn() else "not configured"
        except Exception as exc:  # noqa: BLE001
            out[name] = f"error: {exc}"
    return out


@router.get("/log")
def log(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {"title": a.title, "body": a.body, "channels": a.channels, "sent_at": a.sent_at, "error": a.error}
        for a in db.scalars(select(AlertLog).order_by(AlertLog.sent_at.desc()).limit(50))
    ]
