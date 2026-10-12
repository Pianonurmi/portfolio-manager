"""Portfolio analytics endpoints (PORT-01..04, KIDS-01) with provenance for every figure."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import ACCOUNT_KINDS, INVESTMENT_KINDS, Account, Entity, Instrument, Person
from app.modules.ledger.service import positions
from app.modules.planning import montecarlo
from app.modules.portfolio import factors, optimize
from app.modules.portfolio import lots as lotmod
from app.modules.portfolio.performance import PERIODS, compute, period_start
from app.modules.portfolio.risk import risk_report
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing import distributions
from app.modules.pricing.service import PriceBook, stale_text
from app.platform.auth import CurrentUser, Db
from app.platform.db import pairs, today
from app.platform.explain import ExplainNode, Figure, explainer
from app.platform.models import AppUser
from app.platform.rules import rules_for

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
ZERO = Decimal(0)
CENT = Decimal("0.01")


def eur(v: Decimal) -> Decimal:
    return v.quantize(CENT)


def fmt_eur(v: Decimal) -> str:
    s = f"{abs(v):,.2f}".replace(",", " ").replace(".", ",")
    return ("−" if v < 0 else "") + s + " €"


def fmt_n(v: Decimal, places: int = 4) -> str:
    v = v.normalize()
    q = f"{v:,.{places}f}".rstrip("0").rstrip(".") if v != v.to_integral() else f"{v:,.0f}"
    return q.replace(",", " ").replace(".", ",")


def _scope(scope: str | None) -> Scope:
    try:
        return Scope.parse(scope)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _on(on: date | None) -> date:
    return on or today()


# ---- holdings ---------------------------------------------------------------------------------


@router.get("/holdings")
def holdings(_: CurrentUser, db: Db, scope: str | None = None, on: date | None = None) -> dict[str, Any]:
    sc, d = _scope(scope), _on(on)
    v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
    hs = v.holdings(d)
    book = lotmod.build(db, list(v.accounts), until=d)
    names = {
        i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_({h.instrument_id for h in hs} or {-1})))
    }
    total = sum((h.value for h in hs), ZERO)
    by_acct: dict[int, list[dict[str, Any]]] = defaultdict(list)
    tot_cost = tot_gain = ZERO
    t = today()
    for h in hs:
        inst = names[h.instrument_id]
        is_cash = h.instrument_id in v.book.cash_ccy
        cost = (book.cost_basis(h.account_id, h.instrument_id) * h.share) if not is_cash else h.value
        gain = h.value - cost
        if not is_cash:
            tot_cost += cost
            tot_gain += gain
        prev = v.book.price(h.instrument_id, h.price.date - timedelta(days=1)) if h.price else None
        day = (
            float(h.price.close / prev.close - 1)
            if h.price and prev and prev.close and h.price.date >= d - timedelta(days=4)
            else None
        )
        ref_tail = f"{sc.token}:{d.isoformat()}"
        by_acct[h.account_id].append(
            {
                "instrument_id": inst.id,
                "name": inst.name,
                "symbol": inst.symbol or inst.price_symbol,
                "isin": inst.isin,
                "type": inst.type,
                "currency": inst.currency,
                "is_cash": is_cash,
                "quantity": str(h.quantity * h.share),
                "avg_cost": None
                if is_cash or h.quantity == 0
                else str((cost / (h.quantity * h.share)).quantize(Decimal("0.0001"))),
                "price": None if h.price is None else str(h.price.close),
                "price_currency": None if h.price is None else h.price.currency,
                "price_date": None if h.price is None else h.price.date,
                "price_source": None if h.price is None else h.price.source,
                "value": Figure(
                    value=eur(h.value),
                    ref=f"hold:{h.account_id}:{inst.id}:{ref_tail}",
                    stale=None if is_cash else stale_text(h.price, t),
                ),
                "cost": Figure(value=eur(cost), ref=f"cost:{h.account_id}:{inst.id}:{ref_tail}"),
                "unrealized": Figure(value=eur(gain), ref=f"cost:{h.account_id}:{inst.id}:{ref_tail}"),
                "unrealized_pct": float(gain / cost) if cost and not is_cash else None,
                "day_pct": day,
                "weight": float(h.value / total) if total else 0.0,
            }
        )
    accounts: list[dict[str, Any]] = []
    for aid, rows in by_acct.items():
        a = v.accounts[aid]
        rows.sort(key=lambda r: (r["is_cash"], -float(r["value"].value)))
        val = sum((r["value"].value for r in rows), ZERO)
        accounts.append(
            {
                "account_id": aid,
                "name": a.name,
                "kind": a.kind,
                "kind_label": ACCOUNT_KINDS.get(a.kind, a.kind),
                "value": Figure(value=eur(val), ref=f"acctval:{aid}:{sc.token}:{d.isoformat()}"),
                "holdings": rows,
            }
        )
    accounts.sort(key=lambda a: -float(a["value"].value))
    return {
        "as_of": d,
        "scope": sc.token,
        "accounts": accounts,
        "total": Figure(value=eur(total), ref=f"pv:{sc.token}:{d.isoformat()}"),
        "cost": Figure(value=eur(tot_cost), ref=None),
        "unrealized": Figure(value=eur(tot_gain), ref=f"pv:{sc.token}:{d.isoformat()}"),
        "warnings": book.warnings,
    }


# ---- price coverage ---------------------------------------------------------------------------


@router.get("/price-coverage")
def price_coverage(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    """Every instrument held now with its latest price and why it has none (missing first)."""
    held = {p.instrument_id for p in positions(db)}
    insts = db.scalars(select(Instrument).where(Instrument.id.in_(held or {-1}), Instrument.type != "cash")).all()
    book = PriceBook(db, {i.id for i in insts})
    now = today()
    rows = []
    for i in insts:
        p = book.price(i.id, now)
        stale = stale_text(p, now)
        # a statement or trade price is only a snapshot; a fresh one must not hide a feed that delivers nothing
        no_feed = p is not None and (
            (not i.meta.get("price_source") and not i.price_symbol) or bool(i.meta.get("price_problem"))
        )
        rows.append(
            {
                "id": i.id,
                "name": i.name,
                "isin": i.isin,
                "type": i.type,
                "currency": i.currency,
                "price_source": i.meta.get("price_source"),
                "symbol": i.price_symbol or i.meta.get("ms_secid"),
                "last_date": p.date if p else None,
                "last_close": p.close if p else None,
                "last_kind": p.source if p else None,
                "status": "missing" if p is None else "no_feed" if no_feed else "stale" if stale else "ok",
                "reason": i.meta.get("price_problem")
                or i.meta.get("price_error")
                or i.meta.get("isin_conflict")
                or ("only statement/trade prices; no price feed" if no_feed else None),
            }
        )
    order = {"missing": 0, "no_feed": 1, "stale": 2, "ok": 3}
    return sorted(rows, key=lambda r: (order[r["status"]], r["name"].casefold()))


# ---- performance ------------------------------------------------------------------------------


def _benchmark_id(db: Session) -> int | None:
    u = db.scalar(select(AppUser).limit(1))
    bid = (u.settings or {}).get("benchmark_instrument_id") if u else None
    return int(bid) if bid else None


def _bench_return(book: PriceBook, inst: int, start: date, end: date, annual: bool) -> float | None:
    a, b = book.unit_value_eur(inst, start)[0], book.unit_value_eur(inst, end)[0]
    if not a or not b:
        return None
    cum = float(b / a) - 1
    days = (end - start).days
    return (1 + cum) ** (365 / days) - 1 if annual and days > 0 else cum


@router.get("/performance")
def performance(
    _: CurrentUser, db: Db, scope: str | None = None, period: str = "1Y", kinds: str | None = None
) -> dict[str, Any]:
    sc = _scope(scope)
    k = set(kinds.split(",")) if kinds else INVESTMENT_KINDS
    v = Valuer(db, sc, kinds=k)
    end = today()
    if v.first_date is None:
        return {"scope": sc.token, "periods": {}, "series": [], "benchmark": None}
    dates, values, flows = v.daily(v.first_date, end)
    bid = _benchmark_id(db)
    bbook = PriceBook(db, {bid}) if bid else None
    out: dict[str, Any] = {}
    for p in PERIODS:
        r = compute(p, dates, values, flows)
        if r is None:
            continue
        out[p] = {
            "start": r.start,
            "end": r.end,
            "days": r.days,
            "annualised": r.annualised,
            "twr": None
            if r.twr is None
            else Figure(value=Decimal(str(round(r.twr, 6))), currency=None, ref=f"twr:{sc.token}:{p}"),
            "xirr": None
            if r.xirr is None
            else Figure(value=Decimal(str(round(r.xirr, 6))), currency=None, ref=f"xirr:{sc.token}:{p}"),
            "start_value": str(eur(r.start_value)),
            "end_value": str(eur(r.end_value)),
            "net_flows": Figure(value=eur(r.net_flows), ref=f"flows:{sc.token}:{p}"),
            "gain": Figure(value=eur(r.gain), ref=f"gain:{sc.token}:{p}"),
            "benchmark": _bench_return(bbook, bid, r.start, r.end, r.annualised) if bbook and bid else None,
        }
    # value vs net contributions series for the chosen period (daily ≤ 1Y, weekly beyond)
    start = period_start(period, end, dates[0])
    i0 = next((i for i, d in enumerate(dates) if d >= start), 0)
    step = 1 if (end - start).days <= 400 else 7
    cum = Decimal(0)
    cum_by_i: list[Decimal] = []
    for f in flows:
        cum += f
        cum_by_i.append(cum)
    base = cum_by_i[i0] - values[i0]  # contributions line starts at the period's start value
    idx = list(range(i0, len(dates), step))
    if idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    series = [
        {"date": dates[i], "value": str(eur(values[i])), "contributions": str(eur(cum_by_i[i] - base))} for i in idx
    ]
    bench = None
    if bid:
        inst = db.get(Instrument, bid)
        bench = {"instrument_id": bid, "name": inst.name if inst else "?"}
    return {"scope": sc.token, "periods": out, "series": series, "benchmark": bench}


@router.get("/risk")
def risk(
    _: CurrentUser, db: Db, scope: str | None = None, period: str = "5Y", rf: float = 0.02, proxies: bool = True
) -> dict[str, Any]:
    """Risk and correlations of today's portfolio over past monthly returns (see portfolio/risk.py)."""
    try:
        return risk_report(db, _scope(scope), period, rf, today(), _benchmark_id(db), proxies=proxies)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class BenchmarkIn(BaseModel):
    instrument_id: int | None


@router.put("/benchmark")
def set_benchmark(body: BenchmarkIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    if body.instrument_id and db.get(Instrument, body.instrument_id) is None:
        raise HTTPException(404, "Instrument not found")
    user.settings = {**(user.settings or {}), "benchmark_instrument_id": body.instrument_id}
    return {"benchmark_instrument_id": body.instrument_id}


# ---- optimisation ------------------------------------------------------------------------------


class OptimizeIn(BaseModel):
    scope: str | None = None
    universe: str = "classes"
    objective: str = "max_sharpe"
    target: float | None = None
    period: str = "5Y"
    rf: float | None = None  # None: cash's expected return for the classes, 2 % for the holdings
    max_weight: float | None = None
    assumptions: dict[str, Any] | None = None  # classes: {"mean": {...}, "vol": {...}, "corr": 4x4}
    bounds: dict[str, list[float | None]] | None = None  # asset key -> [min, max]
    candidates: bool = False  # holdings: also candidate funds from the ETF screener


def _optimize(db: Session, p: OptimizeIn) -> dict[str, Any]:
    try:
        return _json(  # type: ignore[no-any-return]
            optimize.optimize(
                db,
                _scope(p.scope),
                today(),
                p.universe,
                p.objective,
                p.target,
                p.period,
                p.rf,
                p.max_weight,
                p.assumptions,
                p.bounds,
                p.candidates,
            )
        )
    except ValueError as exc:  # OptimizeError is one
        raise HTTPException(422, str(exc)) from exc


@router.get("/optimize")
def get_optimize(
    _: CurrentUser,
    db: Db,
    scope: str | None = None,
    universe: str = "classes",
    objective: str = "max_sharpe",
    target: float | None = None,
    period: str = "5Y",
    rf: float | None = None,
    max_weight: float | None = None,
    candidates: bool = False,
) -> dict[str, Any]:
    """Mean-variance optimum and efficient frontier of the classes or the holdings (see portfolio/optimize.py)."""
    return _optimize(
        db,
        OptimizeIn(
            scope=scope,
            universe=universe,
            objective=objective,
            target=target,
            period=period,
            rf=rf,
            max_weight=max_weight,
            candidates=candidates,
        ),
    )


@router.post("/optimize")
def post_optimize(body: OptimizeIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Same, with your own class assumptions and per-asset weight limits."""
    return _optimize(db, body)


@router.get("/optimize/defaults")
def optimize_defaults(_: CurrentUser) -> dict[str, Any]:
    """The default class assumptions (the Monte Carlo's) the editor starts from."""
    return {
        "classes": list(montecarlo.CLASSES),
        "mean": montecarlo.DEFAULT_MEAN,
        "vol": montecarlo.DEFAULT_VOL,
        "corr": montecarlo.DEFAULT_CORR,
        "max_weight": optimize.MAX_WEIGHT,
        "periods": list(optimize.PERIODS),
        "objectives": list(optimize.OBJECTIVES),
    }


# ---- factors -----------------------------------------------------------------------------------


@router.get("/factors")
def get_factors(_: CurrentUser, db: Db, scope: str | None = None, period: str = "MAX") -> dict[str, Any]:
    """Fama-French factor loadings of today's portfolio and its largest holdings (see portfolio/factors.py)."""
    try:
        return factors.factor_report(db, _scope(scope), period, today())
    except factors.FactorDataError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---- income -----------------------------------------------------------------------------------


@router.get("/income")
def income(_: CurrentUser, db: Db, scope: str | None = None, period: str = "1Y") -> dict[str, Any]:
    sc = _scope(scope)
    v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
    end = today()
    first = v.first_date or end
    start = period_start(period, end, first)
    rows = v.income(start + timedelta(days=1), end)
    gross = sum((Decimal(str(r["eur"])) for r in rows if r["role"] == "income" and r["kind"] == "dividend"), ZERO)
    interest = sum((Decimal(str(r["eur"])) for r in rows if r["role"] == "income" and r["kind"] == "interest"), ZERO)
    tax = sum((Decimal(str(r["eur"])) for r in rows if r["role"] == "tax"), ZERO)
    # forecast: trailing-12-month distributions per unit (Yahoo, else the ledger's own) × units held today
    now_q: dict[int, Decimal] = defaultdict(Decimal)
    for (a, i), q in v.quantities(end).items():
        now_q[i] += q * v.shares.on(a, end)
    held = [i for i, q in now_q.items() if q > 0]
    series = distributions.distribution_series(db, held)
    forecast = ZERO
    sources = {"yahoo": 0, "ledger": 0}
    for i, sr in series.items():
        per_unit = distributions.trailing_per_unit_eur(sr, v.book, end)
        if per_unit > 0:
            forecast += per_unit * now_q[i]
            sources[sr.source] += 1
    # yield on what pays: securities, not the cash waiting in the account
    value_now = sum((h.value for h in v.holdings(end) if h.instrument_id not in v.book.cash_ccy), ZERO)
    book = lotmod.build(db, list(v.accounts))
    cost = sum((lot.cost for ls in book.open.values() for lot in ls), ZERO)
    names = pairs(db, select(Instrument.id, Instrument.name))
    by_inst: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        if r["role"] == "income" and r["kind"] == "dividend":
            by_inst[names.get(r["instrument_id"], "Other")] += Decimal(str(r["eur"]))
    return {
        "period": period,
        "start": start,
        "end": end,
        "dividends": Figure(value=eur(gross), ref=f"income:{sc.token}:{period}"),
        "withholding": Figure(value=eur(tax), ref=f"income:{sc.token}:{period}"),
        "interest": Figure(value=eur(interest), ref=f"income:{sc.token}:{period}"),
        "forecast_12m": Figure(value=eur(forecast), ref=None, estimated=True),
        "yield_on_cost": float(forecast / cost) if cost else None,
        "forecast_yield": float(forecast / value_now) if value_now > 0 else None,
        "forecast_sources": sources,
        "by_instrument": [{"name": k, "eur": str(eur(x))} for k, x in sorted(by_inst.items(), key=lambda kv: -kv[1])],
    }


# ---- kids -------------------------------------------------------------------------------------


@router.get("/kids")
def kids(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    out = []
    for p, e in db.execute(
        select(Person, Entity)
        .join(Entity, Entity.person_id == Person.id)
        .where(Person.role == "child")
        .order_by(Person.birth_date)
    ).all():
        sc = Scope(e.id)
        v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
        end = today()
        if v.first_date is None:
            out.append(
                {"person_id": p.id, "entity_id": e.id, "name": p.name, "birth_date": p.birth_date, "value": None}
            )
            continue
        dates, values, flows = v.daily(v.first_date, end)
        mx = compute("Max", dates, values, flows)
        y1 = compute("1Y", dates, values, flows)
        contrib = sum(flows, Decimal(0))
        out.append(
            {
                "person_id": p.id,
                "entity_id": e.id,
                "name": p.name,
                "birth_date": p.birth_date,
                "value": Figure(value=eur(values[-1]), ref=f"pv:{sc.token}:{end.isoformat()}"),
                "contributions": Figure(value=eur(contrib), ref=f"flows:{sc.token}:Max"),
                "gain": Figure(value=eur(values[-1] - contrib), ref=f"gain:{sc.token}:Max"),
                "twr_1y": None
                if not y1 or y1.twr is None
                else Figure(value=Decimal(str(round(y1.twr, 6))), currency=None, ref=f"twr:{sc.token}:1Y"),
                "twr_max": None
                if not mx or mx.twr is None
                else Figure(value=Decimal(str(round(mx.twr, 6))), currency=None, ref=f"twr:{sc.token}:Max"),
                "since": v.first_date,
                "series": [{"date": dates[i], "value": str(eur(values[i]))} for i in range(0, len(dates), 7)]
                + [{"date": dates[-1], "value": str(eur(values[-1]))}],
            }
        )
    return out


# ---- provenance ---------------------------------------------------------------------------------


def _scope_label(db: Session, sc: Scope) -> str:
    if sc.entity_id is None:
        return "household"
    e = db.get(Entity, sc.entity_id)
    return e.name if e else "?"


@explainer("pv")
def explain_pv(db: Session, scope: str, on: str) -> ExplainNode:
    sc, d = Scope.parse(scope), date.fromisoformat(on)
    v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
    by: dict[int, Decimal] = defaultdict(Decimal)
    for h in v.holdings(d):
        by[h.account_id] += h.value
    kids = [
        ExplainNode(label=v.accounts[a].name, value=fmt_eur(x), ref=f"acctval:{a}:{sc.token}:{on}", source="derived")
        for a, x in sorted(by.items(), key=lambda kv: -kv[1])
    ]
    return ExplainNode(
        label=f"Investments · {_scope_label(db, sc)}",
        value=fmt_eur(sum(by.values(), ZERO)),
        formula=f"Σ account values on {d:%-d.%-m.%Y}",
        children=kids,
    )


@explainer("acctval")
def explain_acct(db: Session, account_id: str, scope: str, on: str) -> ExplainNode:
    sc, d = Scope.parse(scope), date.fromisoformat(on)
    v = Valuer(db, sc, account_ids=[int(account_id)])
    hs = sorted(v.holdings(d), key=lambda h: -h.value)
    names = pairs(db, select(Instrument.id, Instrument.name))
    a = db.get(Account, int(account_id))
    kids = [
        ExplainNode(
            label=names.get(h.instrument_id, "?"),
            value=fmt_eur(h.value),
            source="derived",
            ref=f"hold:{h.account_id}:{h.instrument_id}:{sc.token}:{on}",
        )
        for h in hs
    ]
    return ExplainNode(
        label=a.name if a else "Account",
        value=fmt_eur(sum((h.value for h in hs), ZERO)),
        formula="Σ holdings",
        children=kids,
    )


@explainer("hold")
def explain_hold(db: Session, account_id: str, instrument_id: str, scope: str, on: str) -> ExplainNode:
    sc, d = Scope.parse(scope), date.fromisoformat(on)
    aid, iid = int(account_id), int(instrument_id)
    v = Valuer(db, sc, account_ids=[aid])
    h = next((x for x in v.holdings(d) if x.instrument_id == iid), None)
    inst = db.get(Instrument, iid)
    if h is None or inst is None:
        raise LookupError("not held on that date")
    kids = [ExplainNode(label="Quantity", value=fmt_n(h.quantity, 8), source="ledger", ref=f"pos:{aid}:{iid}:{on}")]
    if iid in v.book.cash_ccy:
        if inst.currency != "EUR":
            kids.append(ExplainNode(label="FX", value=fmt_n(h.fx, 6), source="fx", ref=f"fx:{inst.currency}:{on}"))
        formula = "cash balance" + (" × FX" if inst.currency != "EUR" else "")
    else:
        kids.append(
            ExplainNode(
                label="Price",
                value=f"{fmt_n(h.price.close, 4)} {h.price.currency}" if h.price else "missing",
                source="price",
                ref=f"price:{iid}:{on}",
                stale=bool(stale_text(h.price, today())),
            )
        )
        if h.price and h.price.currency != "EUR":
            kids.append(
                ExplainNode(
                    label=f"FX {h.price.currency} → EUR",
                    value=fmt_n(h.fx, 6),
                    source="fx",
                    ref=f"fx:{h.price.currency}:{on}",
                )
            )
        formula = "quantity × price" + (" × FX" if h.price and h.price.currency != "EUR" else "")
    if h.share != 1:
        kids.append(
            ExplainNode(label="Ownership share", value=f"{h.share * 100:.0f} %", source="manual", ref=f"owners:{aid}")
        )
        formula += " × ownership share"
    return ExplainNode(label=f"{inst.name}", value=fmt_eur(h.value), formula=formula, children=kids)


@explainer("cost")
def explain_cost(db: Session, account_id: str, instrument_id: str, scope: str, on: str) -> ExplainNode:
    aid, iid, d = int(account_id), int(instrument_id), date.fromisoformat(on)
    book = lotmod.build(db, [aid], until=d)
    lots = book.lots(aid, iid)
    total = sum((lot.cost for lot in lots), ZERO)
    kids = [
        ExplainNode(
            label=f"Lot bought {lot.acquired:%-d.%-m.%Y}",
            value=fmt_eur(lot.cost),
            source="ledger",
            note=f"{fmt_n(lot.quantity, 8)} units · {fmt_n(lot.unit_cost, 4)} €/unit incl. purchase costs",
            ref=f"txn:{lot.transaction_id}",
        )
        for lot in lots
    ]
    return ExplainNode(
        label="Cost basis (FIFO)",
        value=fmt_eur(total),
        formula="Σ open lots; purchase fees included; sales consume oldest lots first",
        children=kids,
    )


def _perf(db: Session, scope: str, period: str) -> tuple[Scope, Any, Valuer]:
    sc = Scope.parse(scope)
    v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
    if v.first_date is None:
        raise LookupError("no data")
    dates, values, flows = v.daily(v.first_date, today())
    r = compute(period, dates, values, flows)
    if r is None:
        raise LookupError("no data")
    return sc, r, v


@explainer("twr")
def explain_twr(db: Session, scope: str, period: str) -> ExplainNode:
    sc, r, _ = _perf(db, scope, period)
    pct = "—" if r.twr is None else f"{r.twr * 100:+.2f} %".replace(".", ",")
    return ExplainNode(
        label=f"Time-weighted return · {period}",
        value=pct + (" p.a." if r.annualised else ""),
        formula="Π (1 + r_day) − 1 with r_day = (V_day − flows_day) / V_previous − 1"
        + (" · annualised" if r.annualised else ""),
        children=[
            ExplainNode(
                label="Period",
                value=f"{r.start:%-d.%-m.%Y} – {r.end:%-d.%-m.%Y}",
                note=f"{r.days} days of daily valuations",
            ),
            ExplainNode(
                label="Value at start",
                value=fmt_eur(r.start_value),
                ref=f"pv:{sc.token}:{r.start.isoformat()}",
                source="derived",
            ),
            ExplainNode(
                label="Value at end",
                value=fmt_eur(r.end_value),
                ref=f"pv:{sc.token}:{r.end.isoformat()}",
                source="derived",
            ),
            ExplainNode(
                label="External flows excluded",
                value=fmt_eur(r.net_flows),
                ref=f"flows:{sc.token}:{period}",
                source="ledger",
            ),
        ],
    )


@explainer("xirr")
def explain_xirr(db: Session, scope: str, period: str) -> ExplainNode:
    sc, r, _ = _perf(db, scope, period)
    pct = "—" if r.xirr is None else f"{r.xirr * 100:+.2f} %".replace(".", ",")
    return ExplainNode(
        label=f"Money-weighted return (XIRR) · {period}",
        value=pct + (" p.a." if r.annualised else ""),
        formula="rate r solving −V_start − Σ flow/(1+r)^t + V_end/(1+r)^T = 0",
        children=[
            ExplainNode(label="Value at start (invested)", value=fmt_eur(-r.start_value), source="derived"),
            ExplainNode(
                label="Your deposits and withdrawals",
                value=fmt_eur(-r.net_flows),
                ref=f"flows:{sc.token}:{period}",
                source="ledger",
            ),
            ExplainNode(label="Value at end", value=fmt_eur(r.end_value), source="derived"),
        ],
    )


@explainer("gain")
def explain_gain(db: Session, scope: str, period: str) -> ExplainNode:
    sc, r, _ = _perf(db, scope, period)
    return ExplainNode(
        label=f"Investment gain · {period}",
        value=fmt_eur(r.gain),
        formula="value at end − value at start − net deposits",
        children=[
            ExplainNode(label="End", value=fmt_eur(r.end_value)),
            ExplainNode(label="Start", value=fmt_eur(r.start_value)),
            ExplainNode(label="Net deposits", value=fmt_eur(r.net_flows), ref=f"flows:{sc.token}:{period}"),
        ],
    )


@explainer("flows")
def explain_flows(db: Session, scope: str, period: str) -> ExplainNode:
    sc, r, v = _perf(db, scope, period)
    kids = []
    for d, kind, acct, amount, ccy in v.ext:
        if r.start < d <= r.end:
            kids.append(
                ExplainNode(
                    label=f"{d:%-d.%-m.%Y} · {kind.replace('_', ' ')} · {v.accounts[acct].name}",
                    value=fmt_eur(-amount * v.book.eur_per(ccy, d)[0] * v.shares.on(acct, d)),
                    source="ledger",
                )
            )
    for d, acct, inst, q in v.sec_moves:
        if r.start < d <= r.end:
            kids.append(
                ExplainNode(
                    label=f"{d:%-d.%-m.%Y} · securities moved · {v.accounts[acct].name}",
                    value=fmt_eur(q * v.book.unit_value_eur(inst, d)[0]),
                    source="ledger",
                    note="valued at market price",
                )
            )
    return ExplainNode(
        label=f"Deposits and withdrawals · {period}",
        value=fmt_eur(r.net_flows),
        formula="money and securities moved in (+) or out (−)",
        children=kids[-300:],
    )


@explainer("income")
def explain_income(db: Session, scope: str, period: str) -> ExplainNode:
    sc = Scope.parse(scope)
    v = Valuer(db, sc, kinds=INVESTMENT_KINDS)
    end = today()
    start = period_start(period, end, v.first_date or end)
    names = pairs(db, select(Instrument.id, Instrument.name))
    rows = v.income(start + timedelta(days=1), end)
    kids = [
        ExplainNode(
            label=f"{r['date']:%-d.%-m.%Y} · {names.get(r['instrument_id'], r['kind'])}",
            value=fmt_eur(Decimal(str(r["eur"]))),
            source="ledger",
            note=r["role"],
            ref=f"txn:{r['txn']}",
        )
        for r in rows
    ]
    return ExplainNode(
        label=f"Dividends, interest and withholding · {period}",
        value=fmt_eur(sum((Decimal(str(r["eur"])) for r in rows), ZERO)),
        formula="Σ income − tax withheld",
        children=kids,
    )


# ---- allocation & rebalancing ----------------------------------------------------------------


@router.get("/allocation")
def get_allocation(_: CurrentUser, db: Db, scope: str | None = None, dim: str = "asset_class") -> dict[str, Any]:
    from app.modules.portfolio.allocation import allocation

    sc = _scope(scope)
    try:
        a = allocation(db, sc, dim, today())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    total = sum(a.values(), ZERO)
    return {
        "dim": dim,
        "total": str(eur(total)),
        "items": [
            {"key": k, "value": str(eur(x)), "weight": float(x / total) if total else 0.0}
            for k, x in sorted(a.items(), key=lambda kv: -kv[1])
        ],
    }


class GlidePointIn(BaseModel):
    date: date
    mix: dict[str, float]


class TargetsIn(BaseModel):
    targets: dict[str, Decimal]
    band: Decimal = Decimal("0.05")
    glide: list[GlidePointIn] | None = None  # target mix by date; today's targets follow it
    source: str | None = None  # "advisor" when taken from the allocation advisor
    leverage: float | None = Field(default=None, ge=1.0, le=10.0)  # exposure per euro of own money, from the advisor


def glide_of(t: dict[str, Any]) -> list[tuple[date, dict[str, float]]] | None:
    g = t.get("glide")
    return [(date.fromisoformat(p["date"]), {k: float(v) for k, v in p["mix"].items()}) for p in g] if g else None


@router.get("/targets")
def get_targets(user: CurrentUser) -> dict[str, Any]:
    """Saved targets; with a glide path, `targets` is the point on the path for today."""
    from app.modules.portfolio.newmoney import target_on

    t = (user.settings or {}).get("targets") or {
        "targets": {"Equity": "0.8", "Fixed income": "0.1", "Cash": "0.1"},
        "band": "0.05",
    }
    glide = glide_of(t)
    if glide:
        now = target_on(glide, today())
        total = sum(now.values()) or 1.0
        t = {**t, "targets": {k: str(round(v / total, 4)) for k, v in now.items() if v > 0}}
    return t


@router.put("/targets")
def put_targets(body: TargetsIn, user: CurrentUser) -> dict[str, Any]:
    if body.glide is not None:
        if len(body.glide) < 2:
            raise HTTPException(422, "A glide path needs at least two points")
        if any(sum(p.mix.values()) <= 0 or any(v < 0 for v in p.mix.values()) for p in body.glide):
            raise HTTPException(422, "Every glide path point needs a non-negative mix")
    t: dict[str, Any] = {"targets": {k: str(v) for k, v in body.targets.items()}, "band": str(body.band)}
    if body.glide:
        t["glide"] = [{"date": p.date.isoformat(), "mix": p.mix} for p in sorted(body.glide, key=lambda p: p.date)]
    if body.source:
        t["source"] = body.source
    if body.leverage and body.leverage > 1.0:
        t["leverage"] = body.leverage
    user.settings = {**(user.settings or {}), "targets": t}
    return get_targets(user)


@router.get("/rebalance")
def get_rebalance(
    user: CurrentUser, db: Db, scope: str | None = None, new_money: Decimal = Decimal(0)
) -> dict[str, Any]:
    from app.modules.portfolio.allocation import rebalance

    t = get_targets(user)
    r = rebalance(
        db, _scope(scope), {k: Decimal(v) for k, v in t["targets"].items()}, today(), new_money, Decimal(t["band"])
    )

    def conv(x: Any) -> Any:
        return str(eur(x)) if isinstance(x, Decimal) else x

    return {
        "total": conv(r["total"]),
        "band": str(r["band"]),
        "rows": [
            {k: (float(v) if k in ("weight", "target", "drift") else conv(v)) for k, v in row.items()}
            for row in r["rows"]
        ],
        "trades": [{k: conv(v) for k, v in tr.items()} for tr in r["trades"]],
    }


# ---- new-money plan -------------------------------------------------------------------------------


class VariantIn(BaseModel):
    name: str
    funds: list[str]


class NewMoneyIn(BaseModel):
    scope: str | None = None
    monthly: float = 0.0
    lump: float = 0.0
    months: int = 60
    growth: float = 0.0
    min_trade: float = 50.0
    targets: dict[str, float] | None = None  # None: the saved allocation targets
    band: float | None = None
    region_targets: dict[str, float] | None = None
    funds: list[str] | None = None  # instrument ids and "cash"; None: every held ETF/fund, candidates, cash if targeted
    prefer: str = "held"
    variants: list[VariantIn] = []
    mean: dict[str, float] | None = None
    vol: dict[str, float] | None = None
    fees: dict[str, list[float]] | None = None  # instrument type -> [€ per buy, fraction]; None: Nordnet defaults
    max_fee_share: float = 0.01
    follow_glide: bool = True  # with saved glide path and no `targets` in the request
    repay_first: bool = False  # new money pays the securities credit down first


def _credit(db: Session, sc: Scope, on: date, repay_first: bool) -> dict[str, Any]:
    """Today's securities credit and its after-tax rate (Nordnet's tier for the pledged account)."""
    from app.modules.portfolio import leverage as lv
    from app.modules.portfolio.newmoney import credit_for

    loan = credit_for(db, sc, on)
    if loan <= 0:
        return {}
    terms = lv.terms_for(db, on)
    rate = terms.rates["base"]
    acc = lv.pledged_account(db)
    if acc is not None:
        pledges, acc_loan = lv.pledges_for(db, acc, on)
        cv = lv.collateral(pledges, terms)["collateral_value"]
        rate = lv.tier(pledges, acc_loan, cv, terms)["rate"]
    tax = rules_for(db, "tax", on.year)["capital_income"]
    relief = float(tax["brackets"][0]["rate"]) if tax.get("investment_interest_deductible") else 0.0
    return {"loan": loan, "loan_rate": rate * (1 - relief), "repay_first": repay_first}


# Nordnet: ETF monthly saving costs 2.5 € per buy, funds and cash nothing
DEFAULT_FEES: dict[str, list[float]] = {"etf": [2.5, 0.0], "fund": [0.0, 0.0], "stock": [0.0, 0.0]}


def _saved_targets(user: AppUser) -> tuple[dict[str, float], float]:
    t = get_targets(user)
    return {k: float(v) for k, v in t["targets"].items()}, float(t["band"])


@router.get("/new-money")
def get_new_money(user: CurrentUser, db: Db, scope: str | None = None) -> dict[str, Any]:
    """Inputs for the new-money plan: the funds that can be bought, saved targets and the last plan's settings."""
    from app.modules.portfolio import newmoney

    targets, band = _saved_targets(user)
    pos = newmoney.positions_for(db, _scope(scope), today(), targets=targets)
    eq = sum(p.value * p.split.get("Equity", 0.0) for p in pos)
    regions: dict[str, float] = defaultdict(float)
    for p in pos:
        for r, x in p.regions.items():
            regions[r] += p.value * x
    return {
        "targets": targets,
        "band": band,
        "funds": [
            {
                "key": p.key,
                "name": p.name,
                "instrument_id": p.instrument_id,
                "value": p.value,
                "split": p.split,
                "ter": p.ter,
                "held": p.held,
                "buyable": p.buyable,
                "account": p.account,
                "type": p.type,
            }
            for p in pos
        ],
        "equity_regions": {r: x / eq for r, x in sorted(regions.items(), key=lambda kv: -kv[1])} if eq else {},
        "assumptions": {"mean": montecarlo.DEFAULT_MEAN, "vol": montecarlo.DEFAULT_VOL},
        "saved": (user.settings or {}).get("new_money"),
        "fees": DEFAULT_FEES,
        "glide": get_targets(user).get("glide"),
    }


@router.post("/new-money")
def post_new_money(body: NewMoneyIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Buy-only rebalancing: month by month, which funds to buy with new money to reach the target mix."""
    from app.modules.portfolio import newmoney

    saved_t, saved_band = _saved_targets(user)
    targets = body.targets or saved_t
    band = saved_band if body.band is None else body.band
    if body.prefer not in ("held", "equal"):
        raise HTTPException(422, "prefer must be held or equal")
    if not 0 < band < 1:
        raise HTTPException(422, "The band must be between 0 and 100 %")
    sc, on = _scope(body.scope), today()
    chosen = cash = None
    if body.funds is not None:
        cash = "cash" in body.funds
        try:
            chosen = {int(k) for k in body.funds if k != "cash"}
        except ValueError as exc:
            raise HTTPException(422, "funds are instrument ids or cash") from exc
    pos = newmoney.positions_for(db, sc, on, chosen, cash, targets)
    params = newmoney.Params(
        targets=targets,
        monthly=body.monthly,
        lump=body.lump,
        months=body.months,
        growth=body.growth,
        min_trade=max(body.min_trade, 0.0),
        band=band,
        region_targets=body.region_targets or None,
        prefer=body.prefer,
        mean={**class_means(db, user), **(body.mean or {})},
        vol={**montecarlo.DEFAULT_VOL, **(body.vol or {})},
        fees={k: (float(v[0]), float(v[1])) for k, v in (body.fees or DEFAULT_FEES).items()},
        max_fee_share=body.max_fee_share,
        glide=glide_of(get_targets(user)) if body.targets is None and body.follow_glide else None,
        **_credit(db, sc, on, body.repay_first),
    )
    try:
        out = newmoney.plan(pos, params, on.replace(day=1), [(v.name, set(v.funds)) for v in body.variants])
    except newmoney.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    out["sell_alternative"] = newmoney.sell_alternative(db, sc, out["targets"], band, on)
    user.settings = {**(user.settings or {}), "new_money": body.model_dump(exclude={"scope"})}
    return out


# ---- allocation advisor ---------------------------------------------------------------------------


class AdvisorIn(BaseModel):
    scope: str | None = None
    horizon_years: float = 15.0
    max_loss: float = 0.20
    max_shortfall: float = 0.10
    monthly: float | None = None  # None: the new-money plan's monthly amount
    mean: dict[str, float] | None = None
    vol: dict[str, float] | None = None
    corr: list[list[float]] | None = None
    bounds: dict[str, list[float]] | None = None  # class -> [min, max]; "other" defaults to [0, 0]
    leverage: bool = False
    max_leverage: float = 2.0
    max_margin_call: float = 0.02
    collateral_rate: float | None = None  # None: today's Nordnet collateral value per euro of the pledged account


def class_means(db: Session, user: AppUser) -> dict[str, float]:
    """The Monte Carlo class returns with cash at a real rate: your own savings account rate when you have entered
    one, else what a 12-month deposit pays (the overnight average is what idle money earns, not what cash can)."""
    from app.modules.cash import service as cash

    mean = dict(montecarlo.DEFAULT_MEAN)
    r = cash.rates(db, user)
    sav, year = r.get("savings") or {}, r.get("12m") or {}
    if sav.get("source") == "yours":
        mean["cash"] = float(sav["rate"])
    elif year.get("rate"):
        mean["cash"] = float(year["rate"])
    return mean


def _leverage_inputs(db: Session, body: AdvisorIn) -> dict[str, Any]:
    """The advisor's borrowing terms: Nordnet's base rate today and the pledged account's collateral rate."""
    if not body.leverage:
        return {}
    from app.modules.portfolio import leverage as lv

    on = today()
    terms = lv.terms_for(db, on)
    rate = body.collateral_rate
    if rate is None:
        rate = 0.80
        acc = lv.pledged_account(db)
        if acc is not None:
            pledges, _ = lv.pledges_for(db, acc, on)
            rate = lv.collateral(pledges, terms)["effective_rate"] or rate
    tax = rules_for(db, "tax", on.year)["capital_income"]
    return {
        "leverage": True,
        "max_leverage": body.max_leverage,
        "max_margin_call": body.max_margin_call,
        "borrow_rate": terms.rates["base"],
        "deductible": float(tax["brackets"][0]["rate"]) if tax.get("investment_interest_deductible") else 0.0,
        "collateral_rate": min(max(rate, 0.05), 0.9),
    }


@router.post("/advisor")
def post_advisor(body: AdvisorIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """The highest-return class mix inside your loss limits, its projection and a glide path to the horizon."""
    from app.modules.portfolio import advisor

    sc = _scope(body.scope)
    rows = optimize._class_rows(db, sc, today())
    total = sum((r["value"] for r in rows), Decimal(0))
    saved = (user.settings or {}).get("new_money") or {}
    bounds = {"other": (0.0, 0.0)}
    for k, v in (body.bounds or {}).items():
        if k not in montecarlo.CLASSES or len(v) != 2:
            raise HTTPException(422, f"Limits are [min, max] for {', '.join(montecarlo.CLASSES)}")
        bounds[k] = (float(v[0]), float(v[1]))
    a = advisor.Inputs(
        horizon_years=body.horizon_years,
        max_loss=body.max_loss,
        max_shortfall=body.max_shortfall,
        start_value=float(total),
        monthly=float(saved.get("monthly") or 0.0) if body.monthly is None else body.monthly,
        mean={**class_means(db, user), **(body.mean or {})},
        vol={**montecarlo.DEFAULT_VOL, **(body.vol or {})},
        corr=body.corr or [list(r) for r in montecarlo.DEFAULT_CORR],
        bounds=bounds,
        current={r["key"]: float(r["value"] / total) for r in rows} if total > 0 else None,
        **_leverage_inputs(db, body),
    )
    try:
        out = advisor.advise(a, today())
    except ValueError as exc:  # AdvisorError and bad assumptions
        raise HTTPException(422, str(exc)) from exc
    user.settings = {**(user.settings or {}), "advisor": body.model_dump(exclude={"scope"})}
    return out


@router.get("/advisor")
def get_advisor(user: CurrentUser, db: Db) -> dict[str, Any]:
    """The last advisor inputs and today's class assumptions (cash at your savings rate when known)."""
    return {
        "saved": (user.settings or {}).get("advisor"),
        "mean": class_means(db, user),
        "vol": montecarlo.DEFAULT_VOL,
        "corr": montecarlo.DEFAULT_CORR,
        "targets": get_targets(user),
    }


# ---- leverage (Nordnet securities credit) ------------------------------------------------------------


class CollateralIn(BaseModel):
    rate: float | None  # None: back to Nordnet's published rate


def unrealised_gain_share(db: Session, scope: Scope, on: date) -> float:
    """Share of the taxable accounts' value that is unrealised gain (what a forced sale would be taxed on)."""
    v = Valuer(db, scope, kinds={"aot"})
    book = lotmod.build(db, list(v.accounts), until=on)
    value = cost = Decimal(0)
    for h in v.holdings(on):
        if h.value <= 0:
            continue
        value += h.value
        cost += book.cost_basis(h.account_id, h.instrument_id) * h.share
    return float(max(value - cost, Decimal(0)) / value) if value > 0 else 0.0


def leverage_defaults(db: Session, scope: Scope) -> dict[str, Any]:
    """Monte Carlo leverage inputs from today's terms: tier rates, collateral rate, tax and the gain share."""
    from app.modules.portfolio import leverage as lv

    on = today()
    terms = lv.terms_for(db, on)
    tax = rules_for(db, "tax", on.year)["capital_income"]
    rate, diversified = 0.80, True
    acc = lv.pledged_account(db)
    if acc is not None:
        pledges, loan = lv.pledges_for(db, acc, on)
        col = lv.collateral(pledges, terms)
        rate = col["effective_rate"] or rate
        t = lv.tier(pledges, 0.0, col["collateral_value"], terms)
        diversified = all(c["ok"] for c in t["checks"]["hopea"][:2])
    lower = float(tax["brackets"][0]["rate"])
    return {
        "tier_rates": terms.rates,
        "tier_limits": {k: v["max_loan_to_collateral"] for k, v in terms.tiers.items()},
        "diversified": diversified,
        "collateral_rate": round(min(max(rate, 0.05), 0.9), 4),
        "deductible": lower if tax.get("investment_interest_deductible") else 0.0,
        "gain_tax": lower,
        "gain_share": round(unrealised_gain_share(db, scope, on), 4),
    }


@router.get("/leverage")
def get_leverage(
    user: CurrentUser,
    db: Db,
    account_id: int | None = None,
    extra: float = 0.0,
    into: str | None = None,
) -> dict[str, Any]:
    """Nordnet credit on the pledged account: collateral value, tier, interest, margin-call distance and a what-if."""
    from app.modules.portfolio import leverage as lv

    on = today()
    try:
        acc = lv.pledged_account(db, account_id)
        if acc is None:
            raise HTTPException(404, "No book-entry account at Nordnet to borrow against")
        terms = lv.terms_for(db, on)
        tax = rules_for(db, "tax", on.year)["capital_income"]
        deductible = float(tax["brackets"][0]["rate"]) if tax.get("investment_interest_deductible") else 0.0
        pledges, loan = lv.pledges_for(db, acc, on)
        now = lv.report(pledges, loan, terms, deductible)
        after = None
        if extra:
            if extra < 0 or extra > 10_000_000:
                raise HTTPException(422, "Borrow between 0 and 10 million euros")
            after = lv.report(lv.what_if(pledges, loan, extra, terms, into), loan + extra, terms, deductible)
    except lv.LeverageError as exc:
        raise HTTPException(422, str(exc)) from exc
    insts = {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_([int(p.key) for p in pledges])))}
    as_of = max(((insts[int(p.key)].meta or {}).get("nordnet", {}).get("as_of") or "" for p in pledges), default="")
    return {
        "account": {"id": acc.id, "name": acc.name},
        "now": now,
        "after": after,
        "extra": extra,
        "terms": {"rates": terms.rates, "tiers": terms.tiers, "max_rate": terms.max_rate, "source": terms.source},
        "deductible": deductible,
        "collateral_as_of": as_of or None,
        "manual": [p.key for p in pledges if (insts[int(p.key)].meta or {}).get("collateral_manual") is not None],
        "notes": [
            "Collateral rates are Nordnet's published minimums for each holding, raised by its diversification table "
            "when no holding dominates; Nordnet may change them at any time.",
            "Interest is deducted from capital income (or credited against earned-income tax, at most 1 400 € a year).",
            "Over the collateral value Nordnet charges penalty interest on the excess and may sell holdings; a sale "
            "realises gains and the tax on them.",
        ],
    }


@router.post("/leverage/refresh")
def refresh_leverage(_: CurrentUser, db: Db) -> dict[str, int]:
    from app.platform.jobs import enqueue

    return {"run_id": enqueue(db, "nordnet.collateral").id}


@router.put("/leverage/collateral/{instrument_id}")
def put_collateral(instrument_id: int, body: CollateralIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Your own collateral rate for a holding (e.g. what Nordnet confirmed); None returns to the published one."""
    inst = db.get(Instrument, instrument_id)
    if inst is None:
        raise HTTPException(404, "Instrument not found")
    if body.rate is not None and not 0 <= body.rate <= 0.9:
        raise HTTPException(422, "A collateral rate is 0–90 %")
    meta = {k: v for k, v in (inst.meta or {}).items() if k != "collateral_manual"}
    if body.rate is not None:
        meta["collateral_manual"] = body.rate
    inst.meta = meta
    return {"instrument_id": inst.id, "collateral_manual": body.rate}


class HomeEquityIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000)
    mortgage_margin: float = Field(default=0.008, ge=0, le=0.1)
    years: float = Field(default=20, ge=1, le=40)
    bank_ltv: float = Field(default=0.75, gt=0, le=1)
    home_value: float | None = Field(default=None, ge=0)  # None: the properties in net worth
    mortgage_now: float | None = Field(default=None, ge=0)  # None: the mortgages in net worth


@router.post("/financing/home-equity")
def home_equity(body: HomeEquityIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Borrowing to invest: a mortgage top-up against Nordnet credit, after tax and with each one's risks."""
    from app.modules.cash import service as cash
    from app.modules.networth.service import Sheet
    from app.modules.portfolio import financing
    from app.modules.portfolio import leverage as lv

    on = today()
    home = mort = 0.0
    for it in Sheet(db, Scope(None)).items(on):
        if it.account.kind == "property":
            home += float(it.value)
        elif it.account.kind == "mortgage":
            mort += float(-it.value)
    euribor = (cash.latest(db).get(cash.EURIBOR[12]) or {}).get("value") or 0.03
    terms = lv.terms_for(db, on)
    rate, drop = terms.rates["base"], None
    acc = lv.pledged_account(db)
    if acc is not None:
        pledges, loan = lv.pledges_for(db, acc, on)
        rep = lv.report(lv.what_if(pledges, loan, body.amount, terms), loan + body.amount, terms, 0.0)
        rate, drop = rep["tier"]["rate"], rep["margin_call_drop"]["all"]
    tax = rules_for(db, "tax", on.year)["capital_income"]
    deductible = float(tax["brackets"][0]["rate"]) if tax.get("investment_interest_deductible") else 0.0
    out = financing.home_equity_compare(
        body.amount,
        rate,
        drop,
        float(euribor),
        body.mortgage_margin,
        body.years,
        body.home_value if body.home_value is not None else home,
        body.mortgage_now if body.mortgage_now is not None else mort,
        body.bank_ltv,
        deductible,
    )
    out["euribor_12m"] = float(euribor)
    out["home_from_net_worth"] = {"home_value": home, "mortgage": mort}
    return out


# ---- benchmarks -----------------------------------------------------------------------------------


@router.get("/benchmarks")
def get_benchmarks(user: CurrentUser, db: Db, scope: str | None = None, period: str = "5Y") -> dict[str, Any]:
    """Your time-weighted return against common indices (tracked by accumulating EUR ETFs), month by month."""
    from app.modules.portfolio import benchmarks

    rf = class_means(db, user).get("cash", 0.02)
    try:
        return benchmarks.compare(db, _scope(scope), today(), period.upper(), rf)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/benchmarks/refresh")
def refresh_benchmarks(_: CurrentUser, db: Db) -> dict[str, int]:
    from app.platform.jobs import enqueue

    return {"run_id": enqueue(db, "benchmarks.refresh").id}


# ---- X-ray ----------------------------------------------------------------------------------------


def _json(x: Any) -> Any:
    """Money (Decimal) as cent strings; ratios stay floats."""
    if isinstance(x, Decimal):
        return str(eur(x))
    if isinstance(x, dict):
        return {k: _json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_json(v) for v in x]
    return x


@router.get("/xray")
def get_xray(_: CurrentUser, db: Db, scope: str | None = None, on: date | None = None) -> dict[str, Any]:
    """What the investments really hold once every ETF and fund is looked through."""
    from app.modules.portfolio.allocation import xray

    return _json(xray(db, _scope(scope), _on(on)))  # type: ignore[no-any-return]
