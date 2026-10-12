from app.modules.crypto.service import sync_all
from app.platform.jobs import JobContext, job


@job("crypto.sync", "Sync crypto balances (Kraken, Binance, addresses)", cron="20 6,18 * * *")
def crypto_sync(ctx: JobContext) -> None:
    s = sync_all(ctx.db, ctx)
    ctx.summary = ", ".join(f"{k}: {v} changes" for k, v in s.items()) or "no crypto sources connected"
