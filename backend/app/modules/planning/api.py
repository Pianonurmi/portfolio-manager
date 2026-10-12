"""Planning studio API: base state from real data, live preview, scenarios, compare (UX-04, ADR-17)."""

import time
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cashflow.service import monthly
from app.modules.core.models import Entity, Person
from app.modules.networth.service import Sheet
from app.modules.planning import engine, montecarlo, withdrawals
from app.modules.planning.models import Scenario
from app.modules.portfolio.allocation import xray
from app.modules.portfolio.risk import portfolio_monthly_returns
from app.modules.portfolio.valuation import Scope
from app.modules.tax.service import inheritance_tax, ost_status
from app.platform.auth import CurrentUser, Db
from app.platform.db import today
from app.platform.models import AppUser
from app.platform.rules import rules_for

router = APIRouter(prefix="/api/plan", tags=["planning"])

PENSION_DEFAULTS = {
    "accrued_monthly": 0,
    "statement_year": None,
    "annual_earnings": 0,
    "accrual_rate": 0.015,
    "target_age": 67,
    "life_coef": 0.94,
    "tax_rate": 0.25,
}


def _self(db: Db) -> Person | None:
    return db.scalar(select(Person).where(Person.is_self.is_(True)).limit(1))


@router.get("/base")
def base(user: CurrentUser, db: Db, scope: str | None = None) -> dict[str, Any]:
    """Starting point from today's data: investable wealth, YNAB savings/spending, pension settings."""
    me = _self(db)
    ent = db.scalar(select(Entity).where(Entity.person_id == me.id)) if me else None
    sc = Scope.parse(scope) if scope else (Scope(ent.id) if ent else Scope(None))
    items = Sheet(db, sc).items(today())
    wealth = sum((i.value for i in items if i.group in ("investments", "cash")), Decimal(0))
    m = monthly(db, 13)
    age = (today().year - me.birth_date.year) if me and me.birth_date else 40
    pen = {**PENSION_DEFAULTS, **((user.settings or {}).get("pension") or {})}
    stmt_age = age - (today().year - int(pen["statement_year"])) if pen.get("statement_year") else age
    p_month = engine.pension_monthly(
        float(pen["accrued_monthly"]),
        float(pen["annual_earnings"]),
        float(pen["accrual_rate"]),
        stmt_age,
        int(pen["target_age"]),
        int(pen["target_age"]),
        float(pen["life_coef"]),
        float(pen["tax_rate"]),
    )
    spend = float(m["avg_spending"]) or 3000.0
    save = max(0.0, float(m["avg_income"] - m["avg_spending"]))
    return {
        "params": {
            "age_now": age,
            "start_wealth": float(wealth),
            "monthly_savings": round(save or 1000.0, -1),
            "retire_age": min(int(pen["target_age"]), 60),
            "real_return": 0.045,
            "volatility": 0.14,
            "monthly_spending": round(spend, -1),
            "pension_monthly": round(p_month, -1),
            "pension_age": int(pen["target_age"]),
            "withdrawal_tax": 0.10,
            "horizon_age": 95,
            "events": [],
            "inheritances": [],
        },
        "pension": pen,
        "scope": sc.token,
        "sources": {
            "wealth": "today's investments + cash",
            "savings": "YNAB 12-month average" if m["avg_income"] else "default",
            "spending": "YNAB 12-month average" if m["avg_spending"] else "default",
        },
    }


class SimIn(BaseModel):
    params: dict[str, Any]
    seed: int = 20261011
    paths: int = Field(default=1500, ge=100, le=10000)
    earliest: bool = True


@router.post("/simulate")
def simulate(body: SimIn, _: CurrentUser) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        p = engine.Params.from_dict(body.params)
    except TypeError as exc:
        raise HTTPException(422, f"Missing planning input: {exc}") from exc
    r = engine.simulate(p, paths=body.paths, seed=body.seed)
    r["deterministic"] = engine.deterministic(p)
    r["earliest_retirement"] = engine.earliest_retirement(p, seed=body.seed) if body.earliest else None
    r["ms"] = round((time.perf_counter() - t0) * 1000)
    return r


class InheritIn(BaseModel):
    amount: Decimal


@router.post("/inheritance-after-tax")
def inheritance_after_tax(body: InheritIn, _: CurrentUser, db: Db) -> dict[str, str]:
    tax = inheritance_tax(db, body.amount, today().year)
    return {"gross": str(body.amount), "tax": str(tax), "net": str(body.amount - tax)}


class PensionIn(BaseModel):
    accrued_monthly: float = Field(ge=0)
    statement_year: int | None = None
    annual_earnings: float = Field(ge=0)
    accrual_rate: float = 0.015
    target_age: int = Field(ge=60, le=72)
    life_coef: float = Field(gt=0.5, le=1.0)
    tax_rate: float = Field(ge=0, lt=0.7)


@router.put("/pension")
def put_pension(body: PensionIn, user: CurrentUser) -> dict[str, Any]:
    user.settings = {**(user.settings or {}), "pension": body.model_dump()}
    return body.model_dump()


@router.get("/pension/curve")
def pension_curve(user: CurrentUser, db: Db) -> list[dict[str, float]]:
    me = _self(db)
    pen = {**PENSION_DEFAULTS, **((user.settings or {}).get("pension") or {})}
    age = (today().year - me.birth_date.year) if me and me.birth_date else 40
    stmt_age = age - (today().year - int(pen["statement_year"])) if pen.get("statement_year") else age
    return [
        {
            "age": a,
            "monthly_after_tax": round(
                engine.pension_monthly(
                    float(pen["accrued_monthly"]),
                    float(pen["annual_earnings"]),
                    float(pen["accrual_rate"]),
                    stmt_age,
                    a,
                    int(pen["target_age"]),
                    float(pen["life_coef"]),
                    float(pen["tax_rate"]),
                ),
                0,
            ),
        }
        for a in range(max(63, int(pen["target_age"]) - 4), int(pen["target_age"]) + 6)
    ]


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    params: dict[str, Any]
    is_base: bool = False


@router.get("/scenarios")
def scenarios(_: CurrentUser, db: Db) -> list[dict[str, Any]]:
    return [
        {"id": s.id, "name": s.name, "params": s.params, "is_base": s.is_base, "created_at": s.created_at}
        for s in db.scalars(select(Scenario).order_by(Scenario.is_base.desc(), Scenario.created_at))
    ]


@router.post("/scenarios", status_code=201)
def save_scenario(body: ScenarioIn, _: CurrentUser, db: Db) -> dict[str, int]:
    if body.is_base:
        for s in db.scalars(select(Scenario).where(Scenario.is_base.is_(True))):
            s.is_base = False
    s = Scenario(name=body.name, params=body.params, is_base=body.is_base)
    db.add(s)
    db.flush()
    return {"id": s.id}


@router.delete("/scenarios/{sid}", status_code=204)
def delete_scenario(sid: int, _: CurrentUser, db: Db) -> None:
    s = db.get(Scenario, sid)
    if s:
        db.delete(s)


def _years(born: date | None) -> int:
    return today().year - born.year if born else 40


# ---- tax-aware withdrawals and new money (PLAN-09) ------------------------------------------------


def _personal_scope(db: Db, scope: str | None) -> Scope:
    if scope:
        sc = Scope.parse(scope)
        if sc.entity_id is not None:
            return sc
    me = _self(db)
    ent = db.scalar(select(Entity).where(Entity.person_id == me.id)) if me else None
    if ent is None:
        raise HTTPException(422, "Taxes are personal: add yourself under Settings → People")
    return Scope(ent.id)


class WithdrawIn(BaseModel):
    params: dict[str, Any]
    scope: str | None = None


@router.post("/withdrawals")
def withdrawal_order(body: WithdrawIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Which account to draw from first in retirement, with Finnish capital income tax (PLAN-09)."""
    try:
        plan = withdrawals.plan_from_params(body.params)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"Missing planning input: {exc}") from exc
    sc = _personal_scope(db, body.scope)
    try:
        rules = withdrawals.rules_from_db(db, today().year)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return withdrawals.compare(plan, withdrawals.buckets_from_data(db, sc, today()), rules)


class NewMoneyIn(BaseModel):
    monthly: float = Field(gt=0, le=1_000_000)
    years: int = Field(ge=1, le=60)
    real_return: float = Field(default=0.045, gt=-0.5, lt=0.5)
    dividend_yield: float = Field(default=0.0, ge=0, lt=0.2)
    loan_rate: float | None = Field(default=None, ge=0, lt=0.3)
    inflation: float = Field(default=0.02, ge=-0.05, lt=0.2)
    scope: str | None = None


@router.post("/new-money")
def new_money(body: NewMoneyIn, _: CurrentUser, db: Db) -> dict[str, Any]:
    """Where should the next euro go: OST, book-entry account, dividend stocks or the mortgage (PLAN-09)."""
    sc = _personal_scope(db, body.scope)
    try:
        rules = withdrawals.rules_from_db(db, today().year)
        cap = Decimal(str(rules_for(db, "tax", today().year)["ost"]["deposit_cap"]))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    ost = ost_status(db, sc, today())
    room = float(min((Decimal(str(o["cap_left"])) for o in ost), default=cap))
    return {
        "ost_room": room,
        "options": withdrawals.new_money(
            body.monthly, body.years, body.real_return, rules, room, body.dividend_yield, body.loan_rate, body.inflation
        ),
    }


# ---- Monte Carlo: contributions and withdrawals ---------------------------------------------------

CLASS_OF = {"Equity": "equity", "Fixed income": "bond", "Cash": "cash"}  # the rest (Crypto, Other, Unknown): other


def _history(db: Db, sc: Scope) -> montecarlo.History | None:
    dates, rets, _ = portfolio_monthly_returns(db, sc, today())
    return montecarlo.History(dates, rets) if len(rets) else None


def _leverage_or_none(db: Session, sc: Scope) -> dict[str, Any] | None:
    from app.modules.portfolio.api import leverage_defaults

    try:
        return leverage_defaults(db, sc)
    except LookupError:  # the credit or tax rule tables are not loaded yet
        return None


@router.get("/montecarlo/defaults")
def montecarlo_defaults(user: CurrentUser, db: Db, scope: str | None = None) -> dict[str, Any]:
    """Starting values for the simulator from today's data: value, asset mix, fees, age, pension, history."""
    from app.modules.portfolio.api import class_means

    me = _self(db)
    ent = db.scalar(select(Entity).where(Entity.person_id == me.id)) if me else None
    sc = Scope.parse(scope) if scope else (Scope(ent.id) if ent else Scope(None))
    x = xray(db, sc, today())
    mix = dict.fromkeys(montecarlo.CLASSES, 0.0)
    for row in x["dimensions"]["asset_class"]:
        mix[CLASS_OF.get(row["key"], "other")] += row["weight"]
    total = sum(mix.values())
    mix = {k: round(v / total, 4) for k, v in mix.items()} if total > 0 else {**mix, "equity": 1.0}
    b = base(user, db, scope)["params"]
    h = _history(db, sc)
    return {
        "scope": sc.token,
        "start_value": round(float(x["total"])),
        "mix": mix,
        "fees": x["ter"]["fraction"] or 0.0,
        "start_age": b["age_now"],
        "pension_monthly": b["pension_monthly"],
        "pension_age": b["pension_age"],
        "inflation": 0.02,
        "history": {"months": len(h.returns), "from": h.dates[0], "to": h.dates[-1]}
        if h
        else {"months": 0, "from": None, "to": None},
        "leverage": _leverage_or_none(db, sc),
        "assumptions": {
            "mean": class_means(db, user),
            "vol": montecarlo.DEFAULT_VOL,
            "corr": montecarlo.DEFAULT_CORR,
        },
    }


class AssumptionsIn(BaseModel):
    mean: dict[str, float] = Field(default_factory=lambda: dict(montecarlo.DEFAULT_MEAN))
    vol: dict[str, float] = Field(default_factory=lambda: dict(montecarlo.DEFAULT_VOL))
    corr: list[list[float]] = Field(default_factory=lambda: [list(r) for r in montecarlo.DEFAULT_CORR])


class LumpIn(BaseModel):
    age: float = Field(ge=0, le=120)
    amount: float = Field(ge=-1e9, le=1e9)


class ContributionsIn(BaseModel):
    monthly: float = Field(default=0.0, ge=0, le=1e7)
    yearly_increase: float | None = Field(default=None, ge=-0.1, le=0.2)
    until_age: float | None = Field(default=None, ge=0, le=120)
    lumps: list[LumpIn] = Field(default_factory=list, max_length=50)


class WithdrawalIn(BaseModel):
    start_age: int = Field(default=65, ge=0, le=120)
    strategy: Literal["fixed_real", "four_percent", "percent_of_portfolio", "guardrails", "vpw"] = "four_percent"
    monthly: float = Field(default=2000.0, ge=0, le=1e7)
    rate: float | None = Field(default=None, gt=0, le=0.5)
    floor: float = Field(default=0.0, ge=0, le=1e7)
    pension_monthly: float = Field(default=0.0, ge=0, le=1e7)
    pension_age: int = Field(default=65, ge=0, le=120)
    tax: float = Field(default=0.0, ge=0, le=0.6)


class TargetIn(BaseModel):
    age: float = Field(ge=0, le=120)
    value_real: float = Field(gt=0, le=1e11)


class GlideAgeIn(BaseModel):
    age: float = Field(ge=0, le=120)
    mix: dict[str, float]


class LeverageIn(BaseModel):
    ratio: float = Field(default=1.0, ge=1.0, le=10.0)
    policy: Literal["monthly", "yearly", "never"] = "yearly"
    rate: float | None = Field(default=None, ge=0, le=0.3)  # None: Nordnet's tiers
    collateral_rate: float | None = Field(default=None, gt=0, le=0.9)  # None: today's pledged account
    restore: float = Field(default=0.80, gt=0, lt=1)
    until_age: float | None = Field(default=None, ge=0, le=120)  # None: repay when withdrawals start
    margin_calls: bool = True  # False: borrowed against the home (no forced sales)


class HedgeIn(BaseModel):
    kind: Literal["put", "collar", "short"] = "put"
    ratio: float = Field(default=1.0, ge=0, le=2)
    tenor: int = Field(default=12, ge=1, le=24)
    strike: float = Field(default=0.90, gt=0.3, le=1.2)
    skew: float = Field(default=0.03, ge=0, le=0.3)
    fee: float = Field(default=0.002, ge=0, le=0.05)
    short_cost: float = Field(default=0.015, ge=0, le=0.2)


class MonteCarloIn(BaseModel):
    start_value: float = Field(default=0.0, ge=0, le=1e11)
    start_age: int = Field(default=40, ge=0, le=100)
    horizon_age: int = Field(default=95, ge=1, le=120)
    mix: dict[str, float] = Field(default_factory=lambda: {"equity": 1.0})
    fees: float = Field(default=0.0, ge=0, le=0.1)
    inflation: float = Field(default=0.02, ge=-0.05, le=0.2)
    return_model: Literal["assumptions", "historical"] = "assumptions"
    assumptions: AssumptionsIn = Field(default_factory=AssumptionsIn)
    contributions: ContributionsIn = Field(default_factory=ContributionsIn)
    withdrawal: WithdrawalIn = Field(default_factory=WithdrawalIn)
    target: TargetIn | None = None
    seed: int = Field(default=20261011, ge=0, le=2**32 - 1)
    paths: int = Field(default=2000, ge=100, le=10000)
    success_target: float = Field(default=0.90, gt=0, le=1)
    scope: str | None = None
    glide: list[GlideAgeIn] | None = Field(default=None, max_length=200)  # mix by age; overrides `mix`
    use_saved_glide: bool = False  # follow the glide path saved with the allocation targets
    leverage: LeverageIn | None = None
    hedge: HedgeIn | None = None
    leverage_sweep: list[float] | None = Field(default=None, max_length=8)


def _saved_glide(user: AppUser, start_age: int) -> list[dict[str, Any]] | None:
    """The targets' glide path (dates and class labels) as ages and simulator classes; today is `start_age`."""
    from app.modules.portfolio.api import get_targets
    from app.modules.portfolio.optimize import CLASS_OF

    pts = get_targets(user).get("glide")
    if not pts:
        return None
    now = today()
    out = []
    for p in pts:
        mix = dict.fromkeys(montecarlo.CLASSES, 0.0)
        for label, w in p["mix"].items():
            mix[CLASS_OF.get(label, "other")] += float(w)
        out.append({"age": start_age + (date.fromisoformat(p["date"]) - now).days / 365.25, "mix": mix})
    return out


@router.post("/montecarlo")
def montecarlo_run(body: MonteCarloIn, user: CurrentUser, db: Db) -> dict[str, Any]:
    """Simulate contributions then withdrawals on many paths; every strategy runs on the same paths."""
    if body.horizon_age - body.start_age > 70:
        raise HTTPException(422, "The horizon can be at most 70 years away")
    if any(k not in montecarlo.CLASSES for k in (*body.mix, *body.assumptions.mean, *body.assumptions.vol)):
        raise HTTPException(422, f"Asset classes are {', '.join(montecarlo.CLASSES)}")
    if any(set(d) != set(montecarlo.CLASSES) for d in (body.assumptions.mean, body.assumptions.vol)):
        raise HTTPException(422, "Give an expected return and a volatility for every asset class")
    if any(not -0.5 < v < 1.0 for v in body.assumptions.mean.values()) or any(
        not 0 <= v <= 1.5 for v in body.assumptions.vol.values()
    ):
        raise HTTPException(422, "Expected returns must lie in -50 %..100 % and volatilities in 0..150 %")
    d = body.model_dump(exclude={"scope", "use_saved_glide"})
    if body.leverage is not None or body.leverage_sweep:
        from app.modules.portfolio.api import leverage_defaults

        sc = Scope.parse(body.scope) if body.scope else Scope(None)
        lev = {**leverage_defaults(db, sc), **{k: v for k, v in (d.get("leverage") or {}).items() if v is not None}}
        d["leverage"] = lev
        if any(not 1.0 <= float(k) <= 10.0 for k in body.leverage_sweep or []):
            raise HTTPException(422, "Leverage ratios are 1–10×")
    if body.use_saved_glide:
        d["glide"] = _saved_glide(user, body.start_age)
        if not d["glide"]:
            raise HTTPException(422, "No glide path is saved with your targets; make one in the allocation advisor")
    p = montecarlo.Params.from_dict(d)
    history = None
    if p.return_model == "historical":
        me = _self(db)
        ent = db.scalar(select(Entity).where(Entity.person_id == me.id)) if me else None
        history = _history(db, Scope.parse(body.scope) if body.scope else (Scope(ent.id) if ent else Scope(None)))
    try:
        return montecarlo.run(p, history)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
