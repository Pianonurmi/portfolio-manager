"""Deposit interest ladder: pure month-by-month simulation, no database.

Model: a term deposit pays simple interest for the term (rate x months / 12) at maturity, net of tax, and the payout
(principal + net interest) is rolled as the policy says. The savings account accrues rate / 12 per month on the
balance and credits it net of tax every month (an approximation of daily accrual). Rates are held constant.
Deposits still open at the horizon count at principal plus net interest accrued pro rata. Amounts are floats
rounded to cents on output.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any

TENORS = (3, 6, 12, 24)
ROLLOVERS = ("longest", "same", "savings")
MAX_HORIZON = 120


class LadderError(ValueError):
    """Invalid ladder input; the message is shown to the user."""


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _c(x: float) -> float:
    return round(x + 0.0, 2)


@dataclass
class _Dep:
    tenor: int
    principal: float
    start: int  # month index
    rate: float


def normalise_rungs(rest: float, rungs: list[dict[str, Any]] | None) -> list[tuple[int, float]]:
    """(tenor, amount) per rung; missing shares mean an equal split, shares are scaled to add up to 1."""
    if rest <= 0:
        return []
    rungs = rungs if rungs is not None else [{"tenor": t} for t in TENORS]
    if not rungs:
        raise LadderError("Choose at least one deposit term for the money that is not kept liquid")
    tenors = [int(r["tenor"]) for r in rungs]
    bad = [t for t in tenors if t not in TENORS]
    if bad:
        raise LadderError(f"Unknown term {bad[0]} months; choose from {', '.join(map(str, TENORS))}")
    if len(set(tenors)) != len(tenors):
        raise LadderError("Each term can appear only once")
    given = [r.get("share") for r in rungs]
    shares = [1.0 if s is None or None in given else float(s) for s in given]
    if any(s < 0 for s in shares):
        raise LadderError("Shares cannot be negative")
    total = sum(shares)
    if total <= 0:
        raise LadderError("Shares must add up to more than zero")
    amounts = [_c(rest * s / total) for s in shares]
    amounts[-1] = _c(rest - sum(amounts[:-1]))  # cents add up exactly
    return [(t, a) for t, a in zip(tenors, amounts, strict=True) if a > 0]


def simulate(
    total: float,
    liquid: float,
    rungs: list[tuple[int, float]],
    horizon: int,
    rollover: str,
    rates: dict[str, float],
    tax: float,
    start: date,
) -> dict[str, Any]:
    keep = 1 - tax
    sav_rate = rates["savings"]
    deps = [_Dep(t, a, 0, rates[f"{t}m"]) for t, a in rungs]
    longest = max((t for t, _ in rungs), default=None)
    savings = liquid
    rows: list[dict[str, Any]] = []
    gross_sum = 0.0
    events: list[tuple[int, float]] = []  # (month, payout)
    for m in range(1, horizon + 1):
        g_sav = savings * sav_rate / 12
        savings += g_sav * keep
        gross = g_sav
        maturing = 0.0
        for d in [d for d in deps if d.start + d.tenor == m]:
            g = d.principal * d.rate * d.tenor / 12
            payout = d.principal + g * keep
            gross += g
            maturing += payout
            deps.remove(d)
            events.append((m, payout))
            if rollover == "savings":
                savings += payout
            else:
                t = d.tenor if rollover == "same" or longest is None else longest
                deps.append(_Dep(t, payout, m, rates[f"{t}m"]))
        gross_sum += gross
        accrued = sum(d.principal * d.rate * (m - d.start) / 12 * keep for d in deps)
        deposits = sum(d.principal for d in deps) + accrued
        rows.append(
            {
                "month": m,
                "date": add_months(start, m).isoformat(),
                "maturing": _c(maturing),
                "interest_gross": _c(gross),
                "interest_tax": _c(gross * tax),
                "interest_net": _c(gross * keep),
                "savings": _c(savings),
                "deposits": _c(deposits),
                "total": _c(savings + deposits),
            }
        )
    accrued_gross = sum(d.principal * d.rate * (horizon - d.start) / 12 for d in deps)
    gross_total = gross_sum + accrued_gross
    final = rows[-1]["total"] if rows else total
    net = final - total
    years = horizon / 12
    return {
        "rows": rows,
        "final_value": _c(final),
        "gross_interest": _c(gross_total),
        "tax": _c(gross_total * tax),
        "net_interest": _c(net),
        "accrued_interest_net": _c(accrued_gross * keep),
        "effective_net_yield": (final / total) ** (1 / years) - 1 if total > 0 and final > 0 else 0.0,
        "events": events,
    }


def build_ladder(
    total: float,
    liquid: float,
    rungs: list[dict[str, Any]] | None,
    horizon: int,
    rollover: str,
    rates: dict[str, float],
    tax: float,
    start: date,
) -> dict[str, Any]:
    if total <= 0:
        raise LadderError("The amount must be above zero")
    if liquid < 0:
        raise LadderError("The liquid amount cannot be negative")
    if liquid > total:
        raise LadderError("The liquid amount cannot be larger than the total amount")
    if not 1 <= horizon <= MAX_HORIZON:
        raise LadderError(f"The horizon must be 1 to {MAX_HORIZON} months")
    if rollover not in ROLLOVERS:
        raise LadderError(f"Unknown rollover policy {rollover!r}")
    if not 0 <= tax < 1:
        raise LadderError("Tax rate must be between 0 and 1")
    for k in ("savings", *(f"{t}m" for t in TENORS)):
        if k not in rates:
            raise LadderError(f"Missing rate for {k}")
    parts = normalise_rungs(total - liquid, rungs)
    sim = simulate(total, liquid, parts, horizon, rollover, rates, tax, start)

    initial = [
        {
            "product": f"{t}m",
            "months": t,
            "amount": a,
            "rate": rates[f"{t}m"],
            "start": start.isoformat(),
            "maturity": add_months(start, t).isoformat(),
            "interest_gross": _c(a * rates[f"{t}m"] * t / 12),
            "interest_net": _c(a * rates[f"{t}m"] * t / 12 * (1 - tax)),
        }
        for t, a in parts
    ]
    nxt = [
        {"date": add_months(start, m).isoformat(), "amount": _c(p)} for m, p in sorted(sim["events"]) if m <= horizon
    ][:6]

    def alt(label: str, lq: float, tenor: int | None) -> dict[str, Any]:
        r = simulate(total, lq, [(tenor, total)] if tenor else [], horizon, "same", rates, tax, start)
        return {
            "label": label,
            "net_interest": r["net_interest"],
            "final_value": r["final_value"],
            "effective_net_yield": r["effective_net_yield"],
        }

    me = {
        "label": "This ladder",
        "net_interest": sim["net_interest"],
        "final_value": sim["final_value"],
        "effective_net_yield": sim["effective_net_yield"],
    }
    return {
        "inputs": {
            "amount": _c(total),
            "liquid": _c(liquid),
            "horizon": horizon,
            "rollover": rollover,
            "tax_rate": tax,
            "start": start.isoformat(),
            "rates": {k: rates[k] for k in ("savings", *(f"{t}m" for t in TENORS))},
        },
        "rungs": initial,
        "months": sim["rows"],
        "totals": {
            "gross_interest": sim["gross_interest"],
            "tax": sim["tax"],
            "net_interest": sim["net_interest"],
            "accrued_interest_net": sim["accrued_interest_net"],
            "final_value": sim["final_value"],
            "effective_net_yield": sim["effective_net_yield"],
        },
        "available": {"now": _c(liquid), "next": nxt},
        "comparison": [
            me,
            alt("All in the savings account", total, None),
            alt("All in 12-month deposits, renewed", 0, 12),
            alt("All in 24-month deposits, renewed", 0, 24),
        ],
    }
