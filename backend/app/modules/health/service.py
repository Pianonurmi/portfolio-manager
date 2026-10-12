"""Health maths: series keys, window averages and target evaluation."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.modules.health.models import HealthMetric, HealthTarget

ZERO = Decimal(0)


def series_key(kind: str, name: str | None) -> str:
    return f"custom:{name.strip().lower()}" if kind == "custom" and name else kind


def window_avg(rows: list[HealthMetric], on: date, days: int) -> dict[str, Decimal | None] | None:
    """Mean of readings in the `days` days up to and including `on`."""
    start = on - timedelta(days=days - 1)
    sel = [r for r in rows if start <= r.date <= on]
    if not sel:
        return None
    v2 = [r.value2 for r in sel if r.value2 is not None]
    return {
        "value": (sum((r.value for r in sel), ZERO) / len(sel)).quantize(Decimal("0.1")),
        "value2": (sum(v2, ZERO) / len(v2)).quantize(Decimal("0.1")) if v2 else None,
    }


def evaluate(t: HealthTarget | None, value: Decimal | None, value2: Decimal | None = None) -> str:
    """ok | above | below | no_target | no_data. 'above'/'below' say which side of the band the value is on."""
    if t is None:
        return "no_target"
    if value is None:
        return "no_data"
    if t.direction == "below":
        limit_ok = t.high is None or value <= t.high
        dia_ok = t.high2 is None or value2 is None or value2 <= t.high2
        return "ok" if limit_ok and dia_ok else "above"
    if t.direction == "above":
        return "ok" if t.low is None or value >= t.low else "below"
    if t.low is not None and value < t.low:
        return "below"
    if t.high is not None and value > t.high:
        return "above"
    return "ok"


def series_summary(rows: list[HealthMetric], target: HealthTarget | None, on: date) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: (r.date, r.id))
    last = rows[-1] if rows else None
    return {
        "latest": {"date": last.date, "value": last.value, "value2": last.value2, "note": last.note} if last else None,
        "avg_30": window_avg(rows, on, 30),
        "avg_90": window_avg(rows, on, 90),
        "count": len(rows),
        "status": evaluate(target, last.value if last else None, last.value2 if last else None),
        "points": [
            {"date": r.date, "value": r.value, "value2": r.value2} for r in rows if r.date >= on - timedelta(days=365)
        ],
    }
