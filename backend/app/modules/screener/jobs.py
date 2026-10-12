"""ETF screener jobs: the weekly universe refresh and the look-through/price fetch for new candidates."""

from datetime import timedelta

from sqlalchemy import select

from app.modules.core.models import Instrument, InstrumentProfile
from app.modules.portfolio.jobs import _refresh
from app.modules.pricing import service as pricing
from app.modules.pricing.models import Price
from app.modules.screener import nordnet, service
from app.platform.db import today
from app.platform.jobs import JobContext, job
from app.platform.outbound import OutboundClient


@job("etfs.universe", "Refresh the ETF screener list from Morningstar and Nordnet", cron="0 4 * * 6")
def etfs_universe(ctx: JobContext) -> None:
    with OutboundClient() as client:
        s = service.refresh_universe(ctx.db, client)
        try:  # the Morningstar list stands on its own; Nordnet only adds where it can be bought
            n = nordnet.refresh(ctx.db, client)
            sold = f", {n['matched']} sold at Nordnet"
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"Nordnet ETF list: {type(exc).__name__}: {exc}")
            sold = ", Nordnet list unavailable"
        try:
            c = nordnet.refresh_collateral(ctx.db, client, today().isoformat())
            sold += f", collateral rates for {c['matched']} instruments"
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"Nordnet collateral rates: {type(exc).__name__}: {exc}")
    ctx.summary = f"{s['etfs']} ETFs ({s['added']} new, {s['removed']} removed){sold}"


@job("nordnet.collateral", "Fetch Nordnet collateral rates (lainoitusaste) for your instruments")
def nordnet_collateral(ctx: JobContext) -> None:
    with OutboundClient() as client:
        c = nordnet.refresh_collateral(ctx.db, client, today().isoformat())
    ctx.summary = f"{c['matched']} instruments with a Nordnet collateral rate, {c['unlisted']} not listed"


def prepare(ctx: JobContext, client: OutboundClient) -> dict[str, int]:
    """Candidates without a Morningstar profile or without any price get both, with full history."""
    db = ctx.db
    has_profile = select(InstrumentProfile.instrument_id).where(InstrumentProfile.source == "morningstar")
    has_price = select(Price.instrument_id)
    todo = list(
        db.scalars(
            select(Instrument)
            .where(
                Instrument.meta["candidate"].as_boolean().is_(True),
                ~Instrument.id.in_(has_profile) | ~Instrument.id.in_(has_price),
            )
            .order_by(Instrument.id)
        )
    )
    stats = {"prepared": 0, "failed": 0}
    for inst in todo:
        try:
            with db.begin_nested():
                _refresh(ctx, inst, client)
            if inst.currency != "EUR":
                pricing.ensure_fx(db, {inst.currency}, today() - timedelta(days=3650), client)
            if pricing.fetch_instruments(db, [inst], client, ctx, full=True)["failed"]:
                stats["failed"] += 1
            else:
                stats["prepared"] += 1
        except Exception as exc:  # noqa: BLE001 - one candidate must not stop the others
            stats["failed"] += 1
            ctx.warn(f"{inst.name}: {type(exc).__name__}: {exc}")
    return stats


@job("etfs.prepare", "Fetch profiles and prices for candidate ETFs")
def etfs_prepare(ctx: JobContext) -> None:
    with OutboundClient() as client:
        s = prepare(ctx, client)
    ctx.summary = f"{s['prepared']} candidates prepared" + (f", {s['failed']} failed" if s["failed"] else "")
