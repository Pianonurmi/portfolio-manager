"""Price and FX fetching, and the PriceBook used for valuation."""

import bisect
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.core.models import Account, Instrument
from app.modules.ledger.models import Posting, Transaction
from app.modules.pricing.models import SOURCE_PRIORITY, CorporateAction, Distribution, FxRate, Price
from app.modules.pricing.providers import ecb, morningstar, yahoo
from app.platform.db import pairs, today
from app.platform.jobs import JobContext
from app.platform.outbound import OutboundBlocked, OutboundClient

ONE = Decimal(1)


def held_instruments(db: Session) -> list[Instrument]:
    """Non-cash instruments that appear in any posting (held now or in the past), and screener candidates."""
    ids = (
        select(Posting.instrument_id)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Posting.account_id.is_not(None), Posting.instrument_id.is_not(None), Transaction.voided_at.is_(None))
        .distinct()
    )
    candidate = Instrument.meta["candidate"].as_boolean().is_(True)  # ETF screener candidates are priced too
    index = Instrument.meta["index"].as_string().is_not(None)  # market indices used for hedging
    bench = Instrument.meta["benchmark"].as_string().is_not(None)  # benchmark ETFs
    return list(
        db.scalars(
            select(Instrument).where(Instrument.id.in_(ids) | candidate | index | bench, Instrument.type != "cash")
        )
    )


def settled_positions(db: Session) -> set[int]:
    """Closed positions that need no more prices: their history reaches the last trade, or no source was found.

    Routine runs skip them; a full run (prices.backfill) still covers them. Candidates, benchmarks and indices
    are always priced.
    """
    # market prices only: the sale's own trade price says nothing about the history before it
    last_price = (
        select(func.max(Price.date))
        .where(Price.instrument_id == Posting.instrument_id, Price.source != "trade")
        .correlate(Posting)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Posting.instrument_id, func.max(Transaction.date), last_price)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Posting.account_id.is_not(None), Posting.instrument_id.is_not(None), Transaction.voided_at.is_(None))
        .group_by(Posting.instrument_id)
        .having(func.sum(Posting.quantity) == 0)
    ).all()
    closed = {iid: (last_trade, priced) for iid, last_trade, priced in rows}
    out = set()
    for inst in db.scalars(select(Instrument).where(Instrument.id.in_(closed))):
        if inst.meta.get("candidate") or inst.meta.get("benchmark") or inst.meta.get("index"):
            continue
        last_trade, priced = closed[inst.id]
        if (priced is not None and priced >= last_trade) or inst.meta.get("price_problem"):
            out.add(inst.id)
    return out


def first_ledger_date(db: Session) -> date | None:
    return db.scalar(select(func.min(Transaction.date)).where(Transaction.voided_at.is_(None)))


def upsert_prices(db: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    stmt = insert(Price).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_id", "date", "source"],
        set_={"close": stmt.excluded.close, "currency": stmt.excluded.currency, "updated_at": func.clock_timestamp()},
    )
    db.execute(stmt)
    return len(rows)


def record_trade_prices(db: Session) -> int:
    """Use executed trade prices as price observations (fallback when no market feed exists)."""
    rows = db.execute(
        select(Posting.instrument_id, Transaction.date, Posting.price, Posting.price_currency, Instrument.currency)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .join(Instrument, Instrument.id == Posting.instrument_id)
        .where(
            Transaction.voided_at.is_(None),
            Posting.price.is_not(None),
            Posting.price > 0,
            Instrument.type != "cash",
            Transaction.kind.in_(["buy", "sell"]),
        )
    ).all()
    latest: dict[tuple[int, date], dict[str, object]] = {}
    for inst, d, price, pccy, iccy in rows:
        latest[(inst, d)] = {
            "instrument_id": inst,
            "date": d,
            "close": price,
            "currency": pccy or iccy,
            "source": "trade",
        }
    return upsert_prices(db, list(latest.values()))


def fetch_fx(db: Session, client: OutboundClient, ctx: JobContext | None = None) -> int:
    ccys = {c for (c,) in db.execute(select(Instrument.currency).distinct()).all()}
    ccys |= {c for (c,) in db.execute(select(Account.currency).distinct()).all()}
    ccys -= {"EUR"}
    ccys = {c for c in ccys if len(c) == 3 and c not in ("BTC", "ETH")}
    if not ccys:
        return 0
    last = db.scalar(select(func.max(FxRate.date)).where(FxRate.quote.in_(ccys)))
    have = {c for (c,) in db.execute(select(FxRate.quote).distinct()).all()}
    first = first_ledger_date(db) or today() - timedelta(days=30)
    start = (last + timedelta(days=1)) if last and ccys <= have else first - timedelta(days=10)
    rows = ecb.fetch(sorted(ccys), start, client)
    if rows:
        stmt = insert(FxRate).values([{"quote": q, "date": d, "rate": r, "source": "ecb"} for q, d, r in rows])
        db.execute(stmt.on_conflict_do_nothing(index_elements=["quote", "date", "source"]))
    if ctx:
        ctx.log(f"ECB: {len(rows)} rates for {', '.join(sorted(ccys))} since {start}")
    return len(rows)


def ensure_fx(db: Session, currencies: set[str], start: date, client: OutboundClient | None = None) -> int:
    """ECB rates for currencies we hold none of yet (e.g. a statement's first USD holding). Best effort."""
    have = {c for (c,) in db.execute(select(FxRate.quote).distinct()).all()}
    need = sorted(c for c in currencies - have - {"EUR"} if len(c) == 3 and c.isalpha())
    if not need:
        return 0
    try:
        rows = ecb.fetch(need, start - timedelta(days=10), client or OutboundClient())
    except Exception:  # noqa: BLE001 - checks fall back to "cannot convert"
        return 0
    if rows:
        stmt = insert(FxRate).values([{"quote": q, "date": d, "rate": r, "source": "ecb"} for q, d, r in rows])
        db.execute(stmt.on_conflict_do_nothing(index_elements=["quote", "date", "source"]))
    return len(rows)


def _set_meta(inst: Instrument, **kw: object) -> None:
    """Merge into inst.meta (reassigned so the JSON column registers the change); None removes a key."""
    meta = {k: v for k, v in inst.meta.items() if k != "no_symbol"}
    for k, v in kw.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    if meta != inst.meta:
        inst.meta = meta


def _stored_match(inst: Instrument) -> morningstar.Match | None:
    m = inst.meta
    if not (m.get("ms_secid") and m.get("ms_type") and m.get("ms_universe")):
        return None
    return morningstar.Match(
        str(m["ms_secid"]), inst.name, inst.isin, None, None, inst.currency, int(m["ms_type"]), str(m["ms_universe"])
    )


def _statement_hint(db: Session, inst: Instrument) -> tuple[date, Decimal] | None:
    """The latest price a statement printed for this holding: it tells share classes of one fund apart."""
    row = db.execute(
        select(Price.date, Price.close)
        .where(Price.instrument_id == inst.id, Price.source == "statement", Price.currency == inst.currency)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _adopt_morningstar(db: Session, inst: Instrument, ms: morningstar.Match) -> None:
    """Take what Morningstar knows: a missing ISIN (never one another instrument has) and fund/ETF typing."""
    if ms.isin and not inst.isin:
        other = db.scalar(select(Instrument.id).where(Instrument.isin == ms.isin, Instrument.id != inst.id))
        if other:
            _set_meta(inst, isin_conflict=f"same ISIN {ms.isin} as instrument #{other} — merge them")
        else:
            inst.isin = ms.isin
            _set_meta(inst, isin_conflict=None)
    if inst.type == "stock" and ms.holding_type in (morningstar.FUND, morningstar.ETF):
        inst.type = "fund" if ms.holding_type == morningstar.FUND else "etf"
    _set_meta(inst, ms_secid=ms.secid, ms_type=ms.holding_type, ms_universe=ms.universe)


def _yahoo_prices(
    db: Session,
    inst: Instrument,
    client: OutboundClient,
    ctx: JobContext | None,
    start: date,
    stats: dict[str, int],
    keep_prices: bool = True,
) -> str | None:
    """Store the Yahoo listing's events (and its closes unless `keep_prices` is off); returns its quote currency."""
    assert inst.price_symbol
    ccy, hist, splits, divs = yahoo.history_with_events(inst.price_symbol, start, client)
    if ccy and ccy != inst.currency and ctx and keep_prices:
        ctx.warn(f"{inst.name}: provider quotes in {ccy}, instrument currency is {inst.currency}")
    rows = [
        {"instrument_id": inst.id, "date": d, "close": c, "currency": ccy or inst.currency, "source": "yahoo"}
        for d, c in hist
    ]
    if keep_prices:
        stats["rows"] += upsert_prices(db, rows)
    if divs:
        stmt = insert(Distribution).values(
            [
                {
                    "instrument_id": inst.id,
                    "ex_date": d,
                    "amount": a,
                    "currency": ccy or inst.currency,
                    "source": "yahoo",
                }
                for d, a in divs
            ]
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["instrument_id", "ex_date", "source"],
                set_={"amount": stmt.excluded.amount, "currency": stmt.excluded.currency},
            )
        )
    if splits:
        stmt = insert(CorporateAction).values(
            [{"instrument_id": inst.id, "date": d, "kind": "split", "ratio": r, "source": "yahoo"} for d, r in splits]
        )
        db.execute(stmt.on_conflict_do_nothing(index_elements=["instrument_id", "date", "kind", "source"]))
    return ccy


def _home_listing(
    db: Session, inst: Instrument, client: OutboundClient, ctx: JobContext | None, first: date, stats: dict[str, int]
) -> bool:
    """Replace a foreign-currency Yahoo series by Morningstar's listing of the same ISIN in the instrument currency.

    Converting a SEK or USD line to EUR month by month puts FX noise into every risk statistic, so the home-currency
    NAV series becomes the price feed. Only Yahoo's closes are dropped: its dividends and splits stay (they are
    converted with the ECB rates), and the symbol stays so those keep arriving. `home_listing` remembers the outcome
    ("morningstar" or "none") so a daily run does not search again; a full run (prices.backfill) retries.
    """
    if not inst.isin:
        return False
    m = morningstar.find(inst.name, inst.isin, inst.type, inst.currency, client)
    hist = morningstar.history(m, first - timedelta(days=10), inst.currency, client) if m else []
    if m is None or m.currency != inst.currency or not hist:
        _set_meta(inst, home_listing="none")
        return False
    _adopt_morningstar(db, inst, m)
    db.execute(delete(Price).where(Price.instrument_id == inst.id, Price.source == "yahoo"))
    stats["rows"] += upsert_prices(
        db,
        [
            {"instrument_id": inst.id, "date": d, "close": c, "currency": inst.currency, "source": "nav"}
            for d, c in hist
        ],
    )
    _set_meta(inst, price_source="morningstar", home_listing="morningstar", price_problem=None, price_error=None)
    stats["resolved"] += 1
    if ctx:
        ctx.log(f"{inst.name}: priced from the {inst.currency} Morningstar listing instead of {inst.price_symbol}")
    return True


DENSE_QUOTES = 20  # Yahoo closes in the last 90 days below which a listing counts as too thin to rely on


def _placeholder(inst: Instrument, symbol: str | None) -> bool:
    """Yahoo lists some ISINs under the ISIN itself (IE000BI8OT95.SG): such a symbol carries no prices."""
    return bool(symbol and inst.isin and symbol.upper().startswith(inst.isin.upper()))


def _fetch_instrument(
    db: Session,
    inst: Instrument,
    client: OutboundClient,
    ctx: JobContext | None,
    first: date,
    full: bool,
    stats: dict[str, int],
) -> None:
    """Price one instrument: manual/Yahoo symbol first, Morningstar (NAV) for funds and what Yahoo lacks."""
    if inst.meta.get("index") and inst.price_symbol:  # a market index (hedging): Yahoo only, never a name search
        last = None if full else db.scalar(select(func.max(Price.date)).where(Price.instrument_id == inst.id))
        _yahoo_prices(
            db, inst, client, ctx, (last - timedelta(days=5)) if last else today() - timedelta(days=15 * 366), stats
        )
        return
    problem = inst.meta.get("price_problem")
    if problem and not full and not inst.price_symbol and inst.isin:
        return  # looked up by ISIN before without luck; a full run (prices.backfill) retries
    # without an ISIN the name search is one cheap call, so it is repeated daily: matcher fixes and new listings count
    ms = None if full else _stored_match(inst)
    yahoo_recent = 0
    if ms is None and not inst.isin:
        ms = morningstar.find(inst.name, None, inst.type, inst.currency, client, _statement_hint(db, inst))
        if ms:
            _adopt_morningstar(db, inst, ms)
    if _placeholder(inst, inst.price_symbol) and inst.meta.get("price_source") == "yahoo":
        inst.price_symbol = None  # resolved earlier to Yahoo's empty ISIN-named listing; look again
    if not inst.price_symbol and inst.isin and inst.type in ("stock", "etf"):
        sym = yahoo.resolve_matching(inst.isin, inst.currency, client)
        if sym and not _placeholder(inst, sym):
            inst.price_symbol = sym
            _set_meta(inst, price_source="yahoo")
            stats["resolved"] += 1
            if ctx:
                ctx.log(f"{inst.name}: price symbol {sym}")
    if inst.price_symbol:
        last = (
            None
            if full
            else db.scalar(select(func.max(Price.date)).where(Price.instrument_id == inst.id, Price.source == "yahoo"))
        )
        home = inst.meta.get("home_listing")
        ycy = _yahoo_prices(
            db,
            inst,
            client,
            ctx,
            (last - timedelta(days=5)) if last else first - timedelta(days=10),
            stats,
            keep_prices=home != "morningstar",
        )
        if (
            ycy
            and ycy != inst.currency
            and (home is None or full)
            and _home_listing(db, inst, client, ctx, first, stats)
        ):
            return
        recent = db.scalar(
            select(func.count()).where(
                Price.instrument_id == inst.id, Price.source == "yahoo", Price.date >= today() - timedelta(days=90)
            )
        )
        yahoo_recent = recent = recent or 0
        if recent >= DENSE_QUOTES:
            _set_meta(
                inst, price_source=inst.meta.get("price_source") or "manual", price_problem=None, price_error=None
            )
            return
        # no or only a few quotes (an illiquid line such as 0GGH.L): Morningstar's series fills the days
        if ms is None and inst.isin:
            ms = morningstar.find(inst.name, inst.isin, inst.type, inst.currency, client)
            if ms:
                _adopt_morningstar(db, inst, ms)
        if ms is None:
            if recent:  # thin, but the only feed there is
                _set_meta(inst, price_source=inst.meta.get("price_source") or "manual", price_problem=None)
            else:
                _set_meta(inst, price_problem=f"Yahoo has no prices for {inst.price_symbol}")
            return
    if ms is None:
        ms = morningstar.find(
            inst.name, inst.isin, inst.type, inst.currency, client, None if inst.isin else _statement_hint(db, inst)
        )
        if ms:
            _adopt_morningstar(db, inst, ms)
            stats["resolved"] += 1
    if ms is None:
        why = inst.meta.get("isin_conflict")
        _set_meta(
            inst,
            price_problem=why
            or ("not found by ISIN at Yahoo or Morningstar" if inst.isin else "no ISIN, and no unambiguous name match"),
        )
        if ctx:
            ctx.warn(f"{inst.name}: no price source found; set one in Settings → Instruments")
        return
    last = (
        None
        if full
        else db.scalar(select(func.max(Price.date)).where(Price.instrument_id == inst.id, Price.source == "nav"))
    )
    start = (last - timedelta(days=5)) if last else first - timedelta(days=10)
    hist = morningstar.history(ms, start, inst.currency, client)
    stats["rows"] += upsert_prices(
        db,
        [
            {"instrument_id": inst.id, "date": d, "close": c, "currency": inst.currency, "source": "nav"}
            for d, c in hist
        ],
    )
    if hist or last:
        _set_meta(inst, price_source="morningstar", price_problem=None, price_error=None)
    elif yahoo_recent:
        _set_meta(inst, price_problem=None, price_error=None)  # a thin Yahoo line is still a feed
    else:
        _set_meta(inst, price_problem=inst.meta.get("isin_conflict") or "Morningstar has no price history")


def fetch_prices(
    db: Session, client: OutboundClient, ctx: JobContext | None = None, full: bool = False
) -> dict[str, int]:
    insts = held_instruments(db)
    if not full:
        settled = settled_positions(db)
        insts = [i for i in insts if i.id not in settled]
    return fetch_instruments(db, insts, client, ctx, full)


def fetch_instruments(
    db: Session, insts: list[Instrument], client: OutboundClient, ctx: JobContext | None = None, full: bool = False
) -> dict[str, int]:
    stats = {"instruments": 0, "rows": 0, "resolved": 0, "failed": 0}
    first = first_ledger_date(db) or today() - timedelta(days=30)
    for inst in insts:
        stats["instruments"] += 1
        try:
            with db.begin_nested():  # a failed write rolls back this instrument only
                _fetch_instrument(db, inst, client, ctx, first, full, stats)
        except (OutboundBlocked, Exception) as exc:  # noqa: BLE001 - one instrument must not stop the job
            stats["failed"] += 1
            _set_meta(inst, price_error=f"price fetch failed: {exc}"[:200])
            if ctx:
                ctx.warn(f"{inst.name}: {exc}")
    return stats


# ---- lookups --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PricePoint:
    date: date
    close: Decimal
    currency: str
    source: str


@dataclass(frozen=True)
class _Snapshot:
    prices: dict[int, list[PricePoint]]
    pdates: dict[int, list[date]]
    fx: dict[str, list[tuple[date, Decimal]]]
    fdates: dict[str, list[date]]
    cash_ccy: dict[int, str]
    splits: dict[int, list[tuple[date, Decimal]]]


_snapshot: tuple[tuple[Any, ...], _Snapshot] | None = None
_snapshot_lock = threading.Lock()


def _version(db: Session) -> tuple[Any, ...]:
    """Changes whenever a price, FX rate or cash instrument is added, removed or rewritten (as this session sees it)."""
    p = db.execute(select(func.count(), func.max(Price.id), func.max(Price.updated_at))).one()
    f = db.execute(select(func.count(), func.max(FxRate.id))).one()
    c = db.execute(select(func.count(), func.max(Instrument.id)).where(Instrument.type == "cash")).one()
    sp = db.execute(
        select(func.count(), func.max(Transaction.id)).where(
            Transaction.kind == "split", Transaction.voided_at.is_(None)
        )
    ).one()
    return (*p, *f, *c, *sp)


# sources whose history is already adjusted for splits (Morningstar NAV); Yahoo closes here are as traded
ADJUSTED_SOURCES = {"nav"}


def _ledger_splits(db: Session) -> dict[int, list[tuple[date, Decimal]]]:
    """Per instrument, (date, ratio) of the splits booked in the ledger: new units per old unit."""
    rows = db.execute(
        select(Transaction.id, Transaction.date, Posting.instrument_id, Posting.quantity)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(Transaction.kind == "split", Transaction.voided_at.is_(None), Posting.instrument_id.is_not(None))
    ).all()
    # the old and new units may be booked in one transaction or as two (an out- and an in-leg) on the same day
    legs: dict[tuple[date, int], list[Decimal]] = defaultdict(list)
    for _tid, d, inst, qty in rows:
        legs[(d, inst)].append(qty)
    out: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
    for (d, inst), qs in legs.items():
        old = -sum((q for q in qs if q < 0), Decimal(0))
        new = sum((q for q in qs if q > 0), Decimal(0))
        if old > 0 and new > 0 and new != old:
            out[inst].append((d, new / old))
    return dict(out)


def _unadjust(points: list["PricePoint"], splits: list[tuple[date, Decimal]]) -> list["PricePoint"]:
    """Split-adjusted prices before a booked split, scaled back to the units held then (the ledger keeps the old
    quantity until the split date, so adjusted prices would value the holding at a fraction)."""
    if not splits:
        return points
    out = []
    for p in points:
        if p.source in ADJUSTED_SOURCES:
            f = Decimal(1)
            for d, ratio in splits:
                if p.date < d:
                    f *= ratio
            if f != 1:
                p = PricePoint(p.date, p.close * f, p.currency, p.source)
        out.append(p)
    return out


def _build(db: Session) -> _Snapshot:
    best: dict[tuple[int, date], PricePoint] = {}
    q = select(Price.instrument_id, Price.date, Price.close, Price.currency, Price.source)
    for inst, d, c, ccy, src in db.execute(q).all():
        cur = best.get((inst, d))
        if cur is None or SOURCE_PRIORITY.get(src, 50) < SOURCE_PRIORITY.get(cur.source, 50):
            best[(inst, d)] = PricePoint(d, c, ccy, src)
    series: dict[int, list[PricePoint]] = defaultdict(list)
    for (inst, _), p in best.items():
        series[inst].append(p)
    splits = _ledger_splits(db)
    prices = {k: _unadjust(sorted(v, key=lambda p: p.date), splits.get(k, [])) for k, v in series.items()}
    fx: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for quote, d, rate in db.execute(select(FxRate.quote, FxRate.date, FxRate.rate).order_by(FxRate.date)).all():
        fx[quote].append((d, rate))
    cash = pairs(db, select(Instrument.id, Instrument.symbol).where(Instrument.type == "cash"))
    return _Snapshot(
        splits=splits,
        prices=prices,
        pdates={k: [p.date for p in v] for k, v in prices.items()},
        fx=dict(fx),
        fdates={k: [d for d, _ in v] for k, v in fx.items()},
        cash_ccy={k: v for k, v in cash.items() if v},
    )


def _current(db: Session) -> _Snapshot:
    """The price snapshot for this session's view; rebuilt only when the data behind it changed. Building it reads
    every price (about a second for 200 000 rows), and each portfolio request needs one or more."""
    global _snapshot
    key = _version(db)
    with _snapshot_lock:
        if _snapshot is None or _snapshot[0] != key:
            _snapshot = (key, _build(db))
        return _snapshot[1]


class PriceBook:
    """All prices and FX in memory, for fast valuation over many dates. Shares one read-only snapshot per process."""

    def __init__(self, db: Session, instrument_ids: set[int] | None = None) -> None:
        snap = _current(db)
        if instrument_ids is None:
            self.prices, self._pdates = snap.prices, snap.pdates
        else:
            self.prices = {k: snap.prices[k] for k in instrument_ids if k in snap.prices}
            self._pdates = {k: snap.pdates[k] for k in self.prices}
        self.fx, self._fdates = snap.fx, snap.fdates
        self.cash_ccy: dict[int, str] = snap.cash_ccy
        self.splits = snap.splits

    def split_factor(self, instrument_id: int, on: date) -> Decimal:
        """New units per unit held on `on`, from the splits booked after it: divide a price by it to compare across
        splits (per-unit returns), while valuation multiplies the units actually held then."""
        f = Decimal(1)
        for d, ratio in self.splits.get(instrument_id, []):
            if d > on:
                f *= ratio
        return f

    def price(self, instrument_id: int, on: date) -> PricePoint | None:
        ds = self._pdates.get(instrument_id)
        if not ds:
            return None
        i = bisect.bisect_right(ds, on) - 1
        return self.prices[instrument_id][i] if i >= 0 else None

    def eur_per(self, currency: str, on: date) -> tuple[Decimal, date | None]:
        """EUR value of one unit of `currency` on a date (latest ECB rate on or before)."""
        if currency == "EUR":
            return ONE, on
        ds = self._fdates.get(currency)
        if not ds:
            raise LookupError(f"no FX rate for {currency}")
        i = bisect.bisect_right(ds, on) - 1
        if i < 0:
            i = 0  # before the first rate: use the earliest available
        d, rate = self.fx[currency][i]
        return ONE / rate, d

    def unit_value_eur(self, instrument_id: int, on: date) -> tuple[Decimal, PricePoint | None, Decimal]:
        """(EUR value per unit, price used, fx used). Cash instruments are valued at FX only."""
        if instrument_id in self.cash_ccy:
            fx, _ = self.eur_per(self.cash_ccy[instrument_id], on)
            return fx, None, fx
        p = self.price(instrument_id, on)
        if p is None:
            return Decimal(0), None, ONE
        fx, _ = self.eur_per(p.currency, on)
        return p.close * fx, p, fx


def last_business_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def stale_text(p: PricePoint | None, today: date) -> str | None:
    """Human label when a price is older than the last business day (UX-05); None when fresh."""
    if p is None:
        return "no price"
    if p.date >= last_business_day(today):
        return None
    label = "NAV" if p.source in ("nav",) else "trade price" if p.source == "trade" else "price"
    return f"{label} {p.date.day}.{p.date.month}."


def missing_splits(db: Session) -> list[dict[str, Any]]:
    """Provider-reported splits on instruments held at the time, with no split booked in the ledger near that date."""
    from app.modules.ledger.service import positions

    out = []
    for ca in db.scalars(select(CorporateAction).where(CorporateAction.kind == "split").order_by(CorporateAction.date)):
        held = [
            p
            for p in positions(db, on=ca.date - timedelta(days=1))
            if p.instrument_id == ca.instrument_id and p.quantity > 0
        ]
        if not held:
            continue
        booked = db.scalar(
            select(func.count())
            .select_from(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Transaction.kind == "split",
                Transaction.voided_at.is_(None),
                Posting.instrument_id == ca.instrument_id,
                Transaction.date.between(ca.date - timedelta(days=10), ca.date + timedelta(days=10)),
            )
        )
        if not booked:
            out.append(
                {
                    "id": ca.id,
                    "instrument_id": ca.instrument_id,
                    "date": ca.date,
                    "ratio": ca.ratio,
                    "accounts": [(p.account_id, p.quantity) for p in held],
                }
            )
    return out
