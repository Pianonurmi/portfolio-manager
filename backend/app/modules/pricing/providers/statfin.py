"""Statistics Finland PxWeb API: old-dwelling prices (R5, RE-03). Public data, no key.

Two series are offered:
- region index (table 15it): quality-adjusted chained price index by area, house type and rooms, quarterly;
- postal-code prices (table 13mt): average €/m² and number of trades by postal code and house type, quarterly.
  Not quality-adjusted and noisy where trades are few; small areas are suppressed.

Variable codes carry a classification date (e.g. "alue_43_20260625") that changes when Statistics Finland
re-publishes, so they are looked up from the table metadata by prefix on every call.
"""

from decimal import Decimal
from typing import Any

from app.platform.outbound import OutboundClient

BASE = "https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/ashi"
REGION_TABLE = "15it"
POSTAL_TABLE = "13mt"
HOUSE_TYPES_REGION = {"0": "All", "1": "Terraced (rivitalo)", "3": "Block of flats (kerrostalo)"}
ROOMS_REGION = {"00": "All", "01": "Studio", "02": "Two rooms", "03": "Three rooms or more"}
HOUSE_TYPES_POSTAL = {
    "1": "Block of flats, studio",
    "2": "Block of flats, two rooms",
    "3": "Block of flats, three rooms+",
    "5": "Terraced houses",
}


class StatFinError(RuntimeError):
    pass


def _client(client: Any | None) -> Any:
    return client or OutboundClient(timeout=30)


def metadata(table: str, client: Any | None = None) -> dict[str, Any]:
    r = _client(client).get(f"{BASE}/{table}.px")
    if r.status_code >= 400:
        raise StatFinError(f"Statistics Finland table {table}: HTTP {r.status_code}")
    data: dict[str, Any] = r.json()
    return data


def _code(meta: dict[str, Any], prefix: str) -> str:
    for v in meta["variables"]:
        if v["code"].startswith(prefix):
            return str(v["code"])
    raise StatFinError(f"variable {prefix}* not found; the table layout has changed")


def _values(meta: dict[str, Any], prefix: str) -> list[tuple[str, str]]:
    for v in meta["variables"]:
        if v["code"].startswith(prefix):
            return list(zip(v["values"], v["valueTexts"], strict=False))
    return []


def regions(client: Any | None = None) -> list[dict[str, str]]:
    return [{"code": c, "label": t} for c, t in _values(metadata(REGION_TABLE, client), "alue")]


def _query(table: str, sel: dict[str, list[str]], client: Any | None) -> dict[str, Any]:
    body = {
        "query": [{"code": k, "selection": {"filter": "item", "values": v}} for k, v in sel.items()],
        "response": {"format": "json-stat2"},
    }
    r = _client(client).request("POST", f"{BASE}/{table}.px", json=body)
    if r.status_code >= 400:
        raise StatFinError(f"Statistics Finland {table}: HTTP {r.status_code} {r.text[:200]}")
    data: dict[str, Any] = r.json()
    return data


def _series(
    data: dict[str, Any], time_code: str, fixed: dict[str, str] | None = None
) -> list[tuple[str, Decimal | None]]:
    """Read one time series out of a json-stat2 cube: every other dimension fixed to `fixed` (or its only value)."""
    ids: list[str] = data["id"]
    sizes: list[int] = data["size"]
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    base = 0
    for i, dim in enumerate(ids):
        if dim == time_code:
            continue
        index: dict[str, int] = data["dimension"][dim]["category"]["index"]
        pos = index[(fixed or {})[dim]] if fixed and dim in fixed else 0
        base += pos * strides[i]
    t = ids.index(time_code)
    out = []
    for period, i in sorted(data["dimension"][time_code]["category"]["index"].items(), key=lambda kv: kv[1]):
        v = data["value"][base + i * strides[t]]
        out.append((period, Decimal(str(v)) if v is not None else None))
    return out


def region_index(region: str, house_type: str = "3", rooms: str = "00", client: Any | None = None) -> dict[str, Any]:
    """Quarterly chained index for an area. Rooms apply to blocks of flats only (Statistics Finland's rule)."""
    meta = metadata(REGION_TABLE, client)
    contents = [c for c, _ in _values(meta, "contentscode") if c.startswith("ashivq_indeksi_")]
    if not contents:
        raise StatFinError("no price index in table 15it")
    if house_type != "3":
        rooms = "00"
    time_code = _code(meta, "timeperiod")
    content_code = _code(meta, "contentscode")
    labels = dict(_values(meta, "alue"))
    sel = {
        _code(meta, "alue"): [region],
        _code(meta, "talotyyppi"): [house_type],
        _code(meta, "huoneluku"): [rooms],
        content_code: contents,
        time_code: [c for c, _ in _values(meta, "timeperiod")],
    }
    data = _query(REGION_TABLE, sel, client)
    # chained indices with different base years are proportional; use the one that reaches furthest back
    best: tuple[str, list[tuple[str, Decimal]]] = ("", [])
    for c in contents:
        pts = [(p, v) for p, v in _series(data, time_code, {content_code: c}) if v is not None]
        if len(pts) > len(best[1]) or (len(pts) == len(best[1]) and c > best[0]):
            best = (c, pts)
    content, points = best
    return {
        "key": f"statfin:{REGION_TABLE}:{region}:{house_type}:{rooms}",
        "label": f"{labels.get(region, region)} · {HOUSE_TYPES_REGION.get(house_type, house_type)}"
        + (f" · {ROOMS_REGION.get(rooms, rooms)}" if rooms != "00" else "")
        + f" · index {content.rsplit('_', 1)[-1]}=100",
        "unit": "index",
        "points": points,
    }


def postal_prices(postal_code: str, house_type: str = "2", client: Any | None = None) -> dict[str, Any]:
    """Quarterly €/m² for a postal code area (and the number of trades behind each figure)."""
    meta = metadata(POSTAL_TABLE, client)
    time_code = _code(meta, "timeperiod")
    pc_code = _code(meta, "postinumeroalue")
    labels = dict(_values(meta, "postinumeroalue"))
    if postal_code not in labels:
        raise StatFinError(f"postal code {postal_code} is not in Statistics Finland's price data")
    contents = [c for c, _ in _values(meta, "contentscode")]
    price = next(c for c in contents if c.startswith("keskihinta"))
    count = next((c for c in contents if c.startswith("lkm")), None)
    sel = {
        time_code: [c for c, _ in _values(meta, "timeperiod")],
        pc_code: [postal_code],
        _code(meta, "talotyyppi"): [house_type],
        _code(meta, "contentscode"): [price] + ([count] if count else []),
    }
    data = _query(POSTAL_TABLE, sel, client)
    content_code = _code(meta, "contentscode")
    prices = _series(data, time_code, {content_code: price})
    counts = _series(data, time_code, {content_code: count}) if count else [(p, None) for p, _ in prices]
    n = dict(counts)
    return {
        "key": f"statfin:{POSTAL_TABLE}:{postal_code}:{house_type}",
        "label": f"{' '.join(labels[postal_code].split())} · {HOUSE_TYPES_POSTAL.get(house_type, house_type)} · €/m²",
        "unit": "eur_m2",
        "points": [(p, v) for p, v in prices if v is not None],
        "trades": {p: int(c) for p, c in n.items() if c is not None},
    }
