"""Time-weighted and money-weighted returns (PORT-03).

TWR: daily sub-period returns r_d = (V_d − F_d) / V_{d−1} − 1, chained geometrically; F_d = external flows on day d.
XIRR: rate r solving −V_start − Σ F_d/(1+r)^t_d + V_end/(1+r)^T = 0 (investor's view: contributions are outflows).
Periods up to one year are cumulative; longer periods are annualised.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from scipy.optimize import brentq

PERIODS = ["1M", "YTD", "1Y", "3Y", "5Y", "Max"]
MIN_BASE = Decimal(1)  # ignore sub-periods that start from (almost) nothing


def period_start(period: str, end: date, first: date) -> date:
    if period == "1M":
        s = end - timedelta(days=30)
    elif period == "YTD":
        s = date(end.year - 1, 12, 31)
    elif period == "1Y":
        s = end.replace(year=end.year - 1)
    elif period == "3Y":
        s = end.replace(year=end.year - 3)
    elif period == "5Y":
        s = end.replace(year=end.year - 5)
    else:
        s = first
    return max(s, first)


@dataclass
class PeriodResult:
    period: str
    start: date
    end: date
    twr: float | None
    xirr: float | None
    start_value: Decimal
    end_value: Decimal
    net_flows: Decimal
    gain: Decimal  # end − start − flows
    annualised: bool
    days: int


def _annualise(cum: float, days: int) -> float:
    return (1 + cum) ** (365.0 / days) - 1 if days > 0 else cum


def twr(values: list[Decimal], flows: list[Decimal]) -> float | None:
    acc = 1.0
    any_ = False
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev < MIN_BASE:
            continue
        r = float((values[i] - flows[i]) / prev) - 1.0
        acc *= 1 + r
        any_ = True
    return acc - 1 if any_ else None


def xirr(dates: list[date], values: list[Decimal], flows: list[Decimal]) -> float | None:
    if not dates:
        return None
    t0 = dates[0]
    cfs: list[tuple[float, float]] = []
    if values[0] != 0:
        cfs.append((0.0, -float(values[0])))
    for d, f in zip(dates[1:], flows[1:], strict=False):
        if f != 0:
            cfs.append(((d - t0).days / 365.0, -float(f)))
    cfs.append(((dates[-1] - t0).days / 365.0, float(values[-1])))
    if len(cfs) < 2 or all(c >= 0 for _, c in cfs) or all(c <= 0 for _, c in cfs):
        return None

    def npv(r: float) -> float:
        return float(sum(c / (1 + r) ** t for t, c in cfs))

    try:
        root: float = brentq(npv, -0.9999, 100.0, maxiter=200)
        return float(root)
    except ValueError:
        return None


def compute(period: str, dates: list[date], values: list[Decimal], flows: list[Decimal]) -> PeriodResult | None:
    if not dates:
        return None
    end = dates[-1]
    start = period_start(period, end, dates[0])
    i0 = next((i for i, d in enumerate(dates) if d >= start), 0)
    ds, vs, fs = dates[i0:], values[i0:], flows[i0:]
    days = (ds[-1] - ds[0]).days
    annual = days > 366
    t = twr(vs, fs)
    x = xirr(ds, vs, fs)
    if t is not None and annual:
        t = _annualise(t, days)
    if x is not None and not annual:
        x = (1 + x) ** (days / 365.0) - 1 if days > 0 else None
    net = sum(fs[1:], Decimal(0))
    return PeriodResult(period, ds[0], ds[-1], t, x, vs[0], vs[-1], net, vs[-1] - vs[0] - net, annual, days)
