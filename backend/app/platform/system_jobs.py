"""Platform-level jobs."""

from datetime import timedelta

from sqlalchemy import delete

from app.platform.config import get_settings
from app.platform.db import utcnow
from app.platform.jobs import JobContext, job
from app.platform.models import AuthSession, OutboundLog, UndoEntry
from app.platform.rules import load_rules


@job("rules.load", "Load rule tables", cron="15 3 * * *")
def load_rule_tables(ctx: JobContext) -> None:
    loaded = load_rules(ctx.db, get_settings().rules_dir)
    for item in loaded:
        ctx.log(f"loaded {item}")
    ctx.summary = f"{len(loaded)} rule tables loaded or updated" if loaded else "rule tables up to date"


@job("system.cleanup", "Clean up expired sessions and logs", cron="40 3 * * *")
def cleanup(ctx: JobContext) -> None:
    now = utcnow()
    s = ctx.db.execute(delete(AuthSession).where(AuthSession.expires_at < now)).rowcount
    u = ctx.db.execute(delete(UndoEntry).where(UndoEntry.expires_at < now - timedelta(days=7))).rowcount
    o = ctx.db.execute(delete(OutboundLog).where(OutboundLog.created_at < now - timedelta(days=180))).rowcount
    ctx.summary = f"removed {s} sessions, {u} undo entries, {o} outbound log rows"
