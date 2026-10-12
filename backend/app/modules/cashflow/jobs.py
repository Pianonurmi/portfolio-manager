"""Budget sync (morning) and the opt-in value push (after evening prices), for YNAB or Actual."""

from sqlalchemy import select

from app.modules.cashflow import actual, backend, ynab
from app.modules.cashflow.models import BudgetAccount
from app.modules.networth.service import Sheet
from app.modules.portfolio.valuation import Scope
from app.platform.connections import get_connection
from app.platform.db import today
from app.platform.jobs import JobContext, job


@job("ynab.sync", "Sync budget (YNAB or Actual)", cron="0 6 * * *")
def ynab_sync(ctx: JobContext) -> None:
    kind = backend.active(ctx.db)
    if kind is None:
        ctx.summary = "no budget app connected"
        return
    try:
        s = backend.sync(ctx.db, ctx)
    except (ynab.YnabError, actual.ActualError) as exc:
        conn = get_connection(ctx.db, kind)
        if conn:
            conn.status, conn.error = "error", str(exc)
        raise
    ctx.summary = (
        f"{backend.NAMES[kind]}: {s['transactions']} transactions, {s['accounts']} accounts, "
        f"{s['balances']} balances updated"
    )


@job("ynab.push", "Push values to budget tracking accounts", cron="15 19 * * 1-5")
def ynab_push(ctx: JobContext) -> None:
    if not ctx.db.scalar(select(BudgetAccount.id).where(BudgetAccount.push_account_id.is_not(None)).limit(1)):
        ctx.summary = "nothing to push (off by default)"
        return
    items = Sheet(ctx.db, Scope(None)).items(today())
    values = {i.account.id: i.gross for i in items}
    n = backend.push_values(ctx.db, values, ctx=ctx)
    ctx.summary = f"{n} tracking accounts updated"
