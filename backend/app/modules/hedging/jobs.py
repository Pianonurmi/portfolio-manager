"""Weekly: index levels for β and option prices, and the Nordnet Markets short products."""

from app.modules.hedging import products
from app.modules.pricing import service as pricing
from app.platform.db import today
from app.platform.jobs import JobContext, job
from app.platform.outbound import OutboundClient


@job("hedge.refresh", "Fetch index levels and Nordnet Markets hedge products", cron="30 5 * * 6")
def hedge_refresh(ctx: JobContext) -> None:
    insts = products.ensure_indices(ctx.db)
    with OutboundClient() as client:
        try:
            pricing.ensure_fx(
                ctx.db,
                {i.currency for i in insts.values()},
                today().replace(year=today().year - 15),
                client,
            )
        except Exception as exc:  # noqa: BLE001 - USD/SEK levels can wait for the daily FX job
            ctx.warn(f"FX: {exc}")
        stats = pricing.fetch_instruments(ctx.db, list(insts.values()), client, ctx, full=False)
        try:
            p = products.refresh_products(ctx.db, client)
        except Exception as exc:  # noqa: BLE001 - the index levels alone are useful
            ctx.warn(f"Nordnet products: {type(exc).__name__}: {exc}")
            p = {"products": 0}
    ctx.summary = f"{stats['rows']} index prices, {p['products']} hedge products"
