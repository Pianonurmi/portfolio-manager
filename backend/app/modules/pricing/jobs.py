"""Scheduled price and FX fetching (Europe/Helsinki): after the Helsinki close and after the US close."""

from app.modules.pricing.service import fetch_fx, fetch_prices, record_trade_prices
from app.platform.jobs import JobContext, job
from app.platform.outbound import OutboundClient


@job("prices.daily", "Fetch prices and FX rates", cron="45 18 * * 1-5")
def prices_daily(ctx: JobContext) -> None:
    _run(ctx, full=False)


@job("prices.morning", "Fetch overnight closes (US markets, crypto)", cron="30 6 * * *")
def prices_morning(ctx: JobContext) -> None:
    _run(ctx, full=False)


@job("prices.backfill", "Re-download full price history")
def prices_backfill(ctx: JobContext) -> None:
    _run(ctx, full=True)


def _run(ctx: JobContext, full: bool) -> None:
    n_trade = record_trade_prices(ctx.db)
    ctx.log(f"trade prices recorded: {n_trade}")
    with OutboundClient() as client:
        try:
            n_fx = fetch_fx(ctx.db, client, ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"ECB FX failed: {exc}")
            n_fx = 0
        stats = fetch_prices(ctx.db, client, ctx, full=full)
    ctx.summary = f"{stats['rows']} prices for {stats['instruments']} instruments, {n_fx} FX rates" + (
        f", {stats['failed']} failed" if stats["failed"] else ""
    )
