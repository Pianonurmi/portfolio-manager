"""Alert rules (ALERT-02) and the weekly digest (ALERT-03)."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.modules.alerts.notify import send
from app.modules.ledger.models import Transaction
from app.modules.networth.api import attention
from app.modules.networth.service import Sheet
from app.modules.portfolio.valuation import Scope, Valuer
from app.platform.db import session_factory, today
from app.platform.jobs import ON_FAILURE, JobContext, job
from app.platform.models import AppUser


def _settings(ctx: JobContext) -> dict[str, object]:
    u = ctx.db.scalar(select(AppUser).limit(1))
    return dict((u.settings or {}).get("alerts", {})) if u else {}


def _fmt(v: Decimal) -> str:
    return ("−" if v < 0 else "") + f"{abs(v):,.0f}".replace(",", " ") + " €"


@job("alerts.check", "Check alert rules", cron="*/30 * * * *")
def check(ctx: JobContext) -> None:
    cfg = _settings(ctx)
    sent = 0
    if cfg.get("attention", True):
        for a in attention(None, ctx.db):  # type: ignore[arg-type]
            if a["severity"] in ("warn", "bad") and not a.get("private"):  # health etc. stays in the app
                sent += send(
                    ctx.db,
                    f"attention:{a['title']}",
                    a["title"],
                    a["detail"],
                    priority=7 if a["severity"] == "bad" else 4,
                )
    if cfg.get("dividends", True):
        for t in ctx.db.scalars(
            select(Transaction).where(
                Transaction.kind == "dividend",
                Transaction.voided_at.is_(None),
                Transaction.date >= today() - timedelta(days=3),
            )
        ):
            net = sum((p.amount for p in t.postings if p.account_id is not None), Decimal(0))
            sent += send(
                ctx.db,
                f"dividend:{t.id}",
                "Dividend received",
                f"{t.description or 'Dividend'}: {_fmt(net)} on {t.date:%-d.%-m.%Y}",
                quiet_hours=24 * 365,
                priority=2,
            )
    threshold = Decimal(str(cfg.get("large_move_pct", "3")))
    v = Valuer(ctx.db, Scope(None))
    if v.first_date:
        d, vals, flows = v.daily(today() - timedelta(days=4), today())
        if len(vals) >= 2 and vals[-2] > 0:
            change = (vals[-1] - flows[-1] - vals[-2]) / vals[-2] * 100
            if abs(change) >= threshold:
                sent += send(
                    ctx.db,
                    f"move:{d[-1]}",
                    "Large portfolio move",
                    f"Investments {change:+.1f} % today ({_fmt(vals[-1] - vals[-2] - flows[-1])})",
                    quiet_hours=20,
                    priority=6,
                )
    ctx.summary = f"{sent} notifications sent"


@job("alerts.digest", "Weekly digest", cron="0 7 * * 1")
def digest(ctx: JobContext) -> None:
    t = today()
    sheet = Sheet(ctx.db, Scope(None))
    now = sum((i.value for i in sheet.items(t)), Decimal(0))
    week = sum((i.value for i in sheet.items(t - timedelta(days=7))), Decimal(0))
    ch = sheet.change(t - timedelta(days=7), t)
    lines = [
        f"Net worth {_fmt(now)} ({_fmt(now - week)} this week)",
        f"Markets {_fmt(ch['market'])} · contributions {_fmt(ch['contributions'])}"
        f" · debt paid {_fmt(ch['debt_paydown'])}",
    ]
    items = [a for a in attention(None, ctx.db) if not a.get("private")]  # type: ignore[arg-type]
    if items:
        lines.append("Needs attention:")
        lines += [f"• {a['title']}" for a in items[:8]]
    send(
        ctx.db, f"digest:{t.isocalendar().year}-{t.isocalendar().week}", "Weekly summary", "\n".join(lines), priority=3
    )
    ctx.summary = "digest sent" if items is not None else "digest skipped"


def _on_failure(name: str, summary: str) -> None:
    with session_factory()() as db:
        send(
            db,
            f"jobfail:{name}:{today()}",
            f"Job failed: {name}",
            summary or "See Settings → Jobs for the log",
            quiet_hours=12,
            priority=7,
        )
        db.commit()


ON_FAILURE.append(_on_failure)
