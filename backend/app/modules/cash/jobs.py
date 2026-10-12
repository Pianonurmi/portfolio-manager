"""Weekly refresh of deposit and market interest rates."""

from app.modules.cash import service
from app.platform.jobs import JobContext, job
from app.platform.outbound import OutboundClient


@job("rates.refresh", "Fetch deposit and market interest rates (ECB)", cron="0 7 * * 1")
def rates_refresh(ctx: JobContext) -> None:
    with OutboundClient() as client:
        s = service.refresh(ctx.db, client, ctx.warn)
    ctx.summary = f"{s['series']} series, {s['rows']} observations" + (f", {s['failed']} failed" if s["failed"] else "")
