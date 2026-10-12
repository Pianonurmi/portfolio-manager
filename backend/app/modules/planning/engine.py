"""Retirement and life-event simulation (PLAN-01..06, PEN-02/03). Real (inflation-adjusted) euros, yearly steps.

Deterministic for a given (params, seed): results are reproducible (NFR-15).
"""

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class Params:
    age_now: int
    start_wealth: float
    monthly_savings: float
    retire_age: int
    real_return: float  # e.g. 0.045
    volatility: float  # e.g. 0.14
    monthly_spending: float  # in retirement, real
    pension_monthly: float  # after tax, real, from pension_age
    pension_age: int
    withdrawal_tax: float = 0.10  # share of withdrawals lost to capital income tax
    horizon_age: int = 95
    events: list[dict[str, Any]] | None = (
        None  # {"age", "amount", "label"} lumps; {"from_age","to_age","savings_delta"}
    )
    inheritances: list[dict[str, Any]] | None = None  # {"age", "amount_after_tax", "probability"}
    stress: str | None = None  # crash_at_retirement | lost_decade | None (PLAN-07)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Params":
        fields = Params.__dataclass_fields__
        return Params(**{k: d[k] for k in fields if k in d})


def pension_monthly(
    accrued_monthly: float,
    annual_earnings: float,
    accrual_rate: float,
    statement_age: int,
    retire_age: int,
    target_age: int,
    life_coef: float,
    tax_rate: float,
    work_until: int | None = None,
) -> float:
    """Työeläke estimate at `retire_age`: accrued + future accrual, × life-expectancy coefficient,
    −0,4 %/month if taken before the target age, +0,4 %/month if postponed (PEN-02)."""
    stop = min(work_until or retire_age, retire_age)
    future = max(0, stop - statement_age) * annual_earnings * accrual_rate / 12
    gross = (accrued_monthly + future) * life_coef
    months = (retire_age - target_age) * 12
    gross *= 1 + 0.004 * months
    return max(0.0, gross * (1 - tax_rate))


def simulate(p: Params, paths: int = 2000, seed: int = 20261011) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    years = max(1, p.horizon_age - p.age_now)
    ages = np.arange(p.age_now, p.horizon_age + 1)
    mu = np.log1p(p.real_return) - 0.5 * p.volatility**2
    shocks = rng.normal(mu, p.volatility, size=(paths, years))
    w: Any = np.full(paths, float(p.start_wealth))
    grid = np.empty((years + 1, paths))
    grid[0] = w
    alive: Any = np.ones(paths, dtype=bool)
    depleted_age = np.full(paths, np.nan)
    inh = [
        (int(i["age"]), float(i["amount_after_tax"]), float(i.get("probability", 1.0))) for i in (p.inheritances or [])
    ]
    inh_draw = {a: rng.random(paths) < pr for a, _, pr in inh}
    for y in range(1, years + 1):
        age = p.age_now + y
        g = shocks[:, y - 1]
        if p.stress == "crash_at_retirement" and age == p.retire_age + 1:
            g = np.full(paths, np.log(0.65))  # −35 % in the first year of retirement
        elif p.stress == "lost_decade" and p.retire_age < age <= p.retire_age + 10:
            g = g - 0.04  # ten years of 4 %-points lower real returns
        w = w * np.exp(g)
        saving = 0.0
        for e in p.events or []:
            if "from_age" in e and int(e["from_age"]) <= age <= int(e["to_age"]):
                saving += float(e.get("savings_delta", 0)) * 12
            if "age" in e and int(e["age"]) == age:
                w = w + float(e["amount"])
        if age <= p.retire_age:
            w = w + (p.monthly_savings * 12 + saving)
        else:
            need = p.monthly_spending * 12 - (p.pension_monthly * 12 if age >= p.pension_age else 0.0)
            if need > 0:
                w = w - need / (1 - p.withdrawal_tax)
            else:
                w = w - need  # surplus pension is saved
        for a, amt, _ in inh:
            if a == age:
                w = w + np.where(inh_draw[a], amt, 0.0)
        newly = alive & (w <= 0)
        depleted_age[newly] = age
        alive &= w > 0
        w = np.where(alive, w, 0.0)
        grid[y] = w
    pct = {f"p{q}": np.percentile(grid, q, axis=1).round(0).tolist() for q in (10, 25, 50, 75, 90)}
    return {
        "ages": ages.tolist(),
        **pct,
        "success": float(alive.mean()),
        "paths": paths,
        "seed": seed,
        "median_depletion_age": None if np.isnan(depleted_age).all() else float(np.nanmedian(depleted_age)),
    }


def deterministic(p: Params) -> list[float]:
    q = Params(**{**p.__dict__, "volatility": 0.0})
    return simulate(q, paths=1, seed=0)["p50"]  # type: ignore[no-any-return]


def earliest_retirement(p: Params, target: float = 0.85, paths: int = 600, seed: int = 20261011) -> int | None:
    for age in range(max(p.age_now, 40), 71):
        if simulate(Params(**{**p.__dict__, "retire_age": age}), paths=paths, seed=seed)["success"] >= target:
            return age
    return None
