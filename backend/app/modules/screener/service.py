"""ETF screener universe: page through Morningstar's screener, one EtfListing per ISIN."""

import math
import re
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.pricing.providers import morningstar
from app.modules.screener.models import EtfListing
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient

DEFAULT_EXCHANGES = ["XETR", "XHEL", "XAMS", "XPAR", "XMIL", "XSTO", "XCSE", "XOSL", "XBRU"]
PAGE_SIZE = 1000
MAX_PAGES = 30
DATAPOINTS = (
    "SecId|Name|Isin|ExchangeId|Ticker|PriceCurrency|holdingTypeId|OngoingCharge|FundTNAV|CategoryName|"
    "GlobalCategoryName|ReturnM0|ReturnM12|ReturnM36|ReturnM60|ReturnM120|StandardDeviationM36|SharpeM36|"
    "MaxDrawdownM36|InceptionDate|Domicile|StarRatingM255|BrandingCompanyName|EquityStyleBox|PrimaryBenchmarkName|"
    "BaseCurrency|ClosePrice|Distribution|AssetAllocEquity|EffectiveDuration|AverageCreditQuality|SRRI|"
    "SustainabilityRank|Yield_M12"
)
RETURNS = {"ytd": "ReturnM0", "y1": "ReturnM12", "y3": "ReturnM36", "y5": "ReturnM60", "y10": "ReturnM120"}
STYLE_BOX = [
    "Large Value", "Large Blend", "Large Growth",
    "Mid Value", "Mid Blend", "Mid Growth",
    "Small Value", "Small Blend", "Small Growth",
]  # fmt: skip
HEDGED = re.compile(r"(?i:hedged)|\bH(?:Acc|Dist|Inc)?\b")
ACC = re.compile(r"\b(?:H?Acc|Accumulati\w*)\b", re.I)
DIST = re.compile(r"\b(?:H?Dist|H?Inc|Distribut\w*)\b", re.I)


def _num(v: Any) -> float | None:
    """A finite number, else None (provider junk such as "-" or NaN)."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pct(v: Any) -> float | None:
    x = _num(v)
    return None if x is None else round(x / 100, 10)


def _int(v: Any) -> int | None:
    x = _num(v)
    return None if x is None else int(x)


def _text(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _distribution(row: dict[str, Any]) -> str | None:
    d = (_text(row.get("Distribution")) or "").casefold()
    if d in ("acc", "accumulating"):
        return "acc"
    if d in ("inc", "dist", "distributing", "income"):
        return "dist"
    name = _text(row.get("Name")) or ""
    return "acc" if ACC.search(name) else "dist" if DIST.search(name) else None


def _exchange(row: dict[str, Any]) -> str:
    return str(row.get("ExchangeId") or "").removeprefix("EX$$$$")


def fetch_rows(client: OutboundClient, exchanges: list[str]) -> list[dict[str, Any]]:
    """Every ETF row listed on the exchanges; raises on provider errors so callers never see a partial universe."""
    flt = "ExchangeId:IN:" + ":".join(f"EX$$$${e}" for e in exchanges)
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        r = client.get(
            morningstar.SCREENER,
            params={
                "page": page,
                "pageSize": PAGE_SIZE,
                "outputType": "json",
                "version": 1,
                "languageId": "en-GB",
                "currencyId": "EUR",
                "universeIds": morningstar.ETF_UNIVERSE,
                "securityDataPoints": DATAPOINTS,
                "filters": flt,
                "sortOrder": "FundTNAV desc",
            },
        )
        r.raise_for_status()
        body = r.json()
        batch = [x for x in body.get("rows") or [] if isinstance(x, dict)]
        rows.extend(batch)
        if not batch or len(rows) >= (_int(body.get("total")) or 0):
            break
    return rows


def _build(group: list[dict[str, Any]], exchanges: list[str]) -> dict[str, Any]:
    """Fields of one ISIN from its listing rows; the preferred listing wins, others fill what it lacks."""

    def rank(r: dict[str, Any]) -> tuple[bool, int]:
        ex = _exchange(r)
        return (r.get("PriceCurrency") != "EUR", exchanges.index(ex) if ex in exchanges else len(exchanges))

    rows = sorted(group, key=rank)
    best = rows[0]

    def first(key: str, conv: Any = _text) -> Any:
        return next((v for r in rows if (v := conv(r.get(key))) is not None), None)

    name = _text(best.get("Name")) or ""
    box = _int(first("EquityStyleBox"))
    listings = []
    seen = set()
    for r in rows:
        item = (_exchange(r), _text(r.get("Ticker")), _text(r.get("PriceCurrency")), _text(r.get("SecId")))
        if item not in seen:
            seen.add(item)
            listings.append(dict(zip(("exchange", "ticker", "currency", "secid"), item, strict=True)))
    return {
        "name": name,
        "secid": _text(best.get("SecId")),
        "listings": listings,
        "provider": first("BrandingCompanyName"),
        "category": first("CategoryName"),
        "global_category": first("GlobalCategoryName"),
        "ter": _pct(first("OngoingCharge", _num)),
        "fund_size_eur": first("FundTNAV", _num),
        "inception": first("InceptionDate", _date),
        "domicile": first("Domicile"),
        "distribution": _distribution(best) or next((d for r in rows if (d := _distribution(r))), None),
        "hedged": bool(HEDGED.search(name)),
        "base_currency": first("BaseCurrency"),
        "equity_share": _pct(first("AssetAllocEquity", _num)),
        "duration": first("EffectiveDuration", _num),
        "credit_quality": _int(first("AverageCreditQuality", _num)),
        "srri": _int(first("SRRI", _num)),
        "star_rating": _int(first("StarRatingM255", _num)),
        "sustainability": _int(first("SustainabilityRank", _num)),
        "benchmark": first("PrimaryBenchmarkName"),
        "style_box": STYLE_BOX[box - 1] if box and 1 <= box <= 9 else None,
        "returns": {k: v for k, src in RETURNS.items() if (v := _pct(best.get(src))) is not None},
        "vol_3y": _pct(first("StandardDeviationM36", _num)),
        "sharpe_3y": first("SharpeM36", _num),
        "max_dd_3y": _pct(first("MaxDrawdownM36", _num)),
        "yield_12m": _pct(first("Yield_M12", _num)),
        "close_price": _num(best.get("ClosePrice")),
        "close_currency": _text(best.get("PriceCurrency")),
    }


def refresh_universe(db: Session, client: OutboundClient, exchanges: list[str] | None = None) -> dict[str, int]:
    """Upsert one row per ISIN; drops ETFs that vanished, but never when the fetch returned nothing."""
    exchanges = exchanges or DEFAULT_EXCHANGES
    rows = fetch_rows(client, exchanges)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        isin = _text(r.get("Isin"))
        if isin and _int(r.get("holdingTypeId")) == morningstar.ETF and _text(r.get("Name")):
            groups[isin.upper()].append(r)
    stats = {"rows": len(rows), "etfs": len(groups), "added": 0, "removed": 0}
    if not rows:
        return stats
    now = utcnow()
    existing = {e.isin: e for e in db.scalars(select(EtfListing))}
    for isin, group in groups.items():
        fields = _build(group, exchanges)
        row = existing.get(isin)
        if row is None:
            db.add(EtfListing(isin=isin, seen_at=now, updated_at=now, **fields))
            stats["added"] += 1
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            row.seen_at = row.updated_at = now
    db.flush()
    stats["removed"] = db.execute(delete(EtfListing).where(EtfListing.seen_at < now)).rowcount
    return stats
