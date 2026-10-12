"""Weekly refresh of instrument profiles (look-through data for the X-ray)."""

from sqlalchemy import select

from app.modules.core.models import Instrument
from app.modules.ledger.service import positions
from app.modules.portfolio import profiles
from app.modules.portfolio.factors import refresh_factors
from app.modules.pricing import service as pricing
from app.modules.pricing.providers import morningstar
from app.platform.jobs import JobContext, job
from app.platform.outbound import OutboundClient


def _secid(ctx: JobContext, inst: Instrument, client: OutboundClient) -> str | None:
    """The stored Morningstar id, else look it up the way the price job does and remember it."""
    if inst.meta.get("ms_secid"):
        return str(inst.meta["ms_secid"])
    ms = morningstar.find(inst.name, inst.isin, inst.type, inst.currency, client, pricing._statement_hint(ctx.db, inst))
    if ms is None:
        return None
    pricing._adopt_morningstar(ctx.db, inst, ms)
    return ms.secid


def _refresh(ctx: JobContext, inst: Instrument, client: OutboundClient) -> str:
    """Refresh one instrument; returns "morningstar" when look-through data was fetched, else "derived" or "missing"."""
    db = ctx.db
    if inst.type == "stock" and inst.isin and not inst.meta.get("ms_type"):
        # statements create funds as shares (and Yahoo prices them anyway); Morningstar knows which it is
        _secid(ctx, inst, client)
    profiles.save(db, inst.id, "derived", None, profiles.derived(inst))
    if inst.type in ("etf", "fund"):
        secid = _secid(ctx, inst, client)
        if secid is None:
            ctx.warn(f"{inst.name}: not found at Morningstar, no look-through data")
            return "missing"
        as_of, data = profiles.fetch_morningstar(secid, client)
        profiles.save(db, inst.id, "morningstar", as_of, data)
        if "asset_classes" in data:
            inst.asset_class = profiles.asset_class_label(data["asset_classes"])
        return "morningstar"
    if inst.type == "stock" and inst.isin:
        facts = morningstar.stock_facts(inst.isin, client)
        if facts and (data := profiles.parse_stock_facts(facts)):
            profiles.save(db, inst.id, "morningstar", None, data)
            return "morningstar"
    return "derived"


def refresh_profiles(ctx: JobContext, client: OutboundClient) -> dict[str, int]:
    """Held non-cash instruments and screener candidates get a profile; one failing never stops the rest."""
    ids = {p.instrument_id for p in positions(ctx.db)}
    insts = list(
        ctx.db.scalars(
            select(Instrument)
            .where(
                Instrument.id.in_(ids) | Instrument.meta["candidate"].as_boolean().is_(True), Instrument.type != "cash"
            )
            .order_by(Instrument.id)
        )
    )
    stats = {"morningstar": 0, "derived": 0, "missing": 0, "failed": 0}
    for inst in insts:
        try:
            with ctx.db.begin_nested():
                stats[_refresh(ctx, inst, client)] += 1
        except Exception as exc:  # noqa: BLE001 - one bad provider answer must not stop the other holdings
            stats["failed"] += 1
            ctx.warn(f"{inst.name}: {type(exc).__name__}: {exc}")
    return stats


@job("instruments.profile", "Refresh instrument profiles from Morningstar", cron="0 5 * * 0")
def instruments_profile(ctx: JobContext) -> None:
    with OutboundClient() as client:
        s = refresh_profiles(ctx, client)
    ctx.summary = (
        f"{s['morningstar']} from Morningstar, {s['derived']} derived"
        + (f", {s['missing']} not found" if s["missing"] else "")
        + (f", {s['failed']} failed" if s["failed"] else "")
    )


@job("factors.refresh", "Refresh Fama-French factor data", cron="0 6 3 * *")
def factors_refresh(ctx: JobContext) -> None:
    """Kenneth French publishes the developed-market factors monthly, a few weeks after month end."""
    with OutboundClient() as client:
        counts = refresh_factors(ctx.db, client)
    ctx.summary = f"{len(counts)} series, up to {max(counts.values())} months"


@job("benchmarks.refresh", "Add the benchmark ETFs and fetch their full price history", cron="0 6 * * 0")
def benchmarks_refresh(ctx: JobContext) -> None:
    from app.modules.portfolio import benchmarks

    insts = benchmarks.ensure(ctx.db)
    with OutboundClient() as client:
        stats = pricing.fetch_instruments(ctx.db, list(insts.values()), client, ctx, full=True)
    ctx.summary = f"{len(insts)} benchmarks, {stats['rows']} prices" + (
        f", {stats['failed']} failed" if stats["failed"] else ""
    )
