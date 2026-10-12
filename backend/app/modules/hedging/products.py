"""Market indices to hedge against and the Nordnet Markets products that short them.

Index levels come from Yahoo (allowlisted) as instruments marked meta["index"]; the price job keeps them current.
Products come from Nordnet's public lists (mini futures, turbos, bull/bear certificates), short direction only.
"""

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.hedging.models import HedgeProduct
from app.modules.screener.nordnet import BASE, HEADERS, PAGE
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient


@dataclass(frozen=True)
class IndexDef:
    key: str
    name: str
    yahoo: str
    currency: str
    nordnet_underlying: str  # etp_info.underlying_name in Nordnet's lists
    search: str  # free-text search that finds its products
    region: str  # the X-ray region it stands for


INDICES = [
    IndexDef("SPX", "S&P 500", "^GSPC", "USD", "S&P 500 Index", "SP500", "North America"),
    IndexDef("NDX", "Nasdaq 100", "^NDX", "USD", "NASDAQ 100 Index", "NQ100", "North America"),
    IndexDef("SX5E", "Euro Stoxx 50", "^STOXX50E", "EUR", "Euro Stoxx 50 Index", "SX5E", "Eurozone"),
    IndexDef("DAX", "DAX", "^GDAXI", "EUR", "DAX Index", "DAX", "Eurozone"),
    IndexDef("OMXH25", "OMX Helsinki 25", "^OMXH25", "EUR", "OMX Helsinki 25", "OMXH25", "Eurozone"),
    IndexDef("OMXS30", "OMX Stockholm 30", "^OMX", "SEK", "OMX Stockholm 30 Index", "OMX", "Europe ex-Euro"),
]
BY_KEY = {i.key: i for i in INDICES}
LISTS = {"minifuturelist": "mini", "warrantlist": "turbo", "bullbearlist": "bear"}
FACTOR = re.compile(r"\bX(\d+(?:[.,]\d+)?)\b", re.I)


def ensure_indices(db: Session) -> dict[str, Instrument]:
    """One instrument per index (type other, priced from Yahoo); created on first use."""
    have = {
        str(i.meta.get("index")): i
        for i in db.scalars(select(Instrument).where(Instrument.meta["index"].as_string().is_not(None)))
    }
    for d in INDICES:
        if d.key not in have:
            inst = Instrument(
                type="other",
                name=f"{d.name} (index)",
                currency=d.currency,
                price_symbol=d.yahoo,
                asset_class="equity",
                meta={"index": d.key, "price_source": "yahoo"},
            )
            db.add(inst)
            have[d.key] = inst
    db.flush()
    return have


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(client: OutboundClient, lst: str, term: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(40):
        r = client.get(
            f"{BASE}/{lst}",
            params={"limit": PAGE, "offset": page * PAGE, "free_text_search": term},
            headers=HEADERS,
        )
        r.raise_for_status()
        body = r.json()
        batch = [x for x in body.get("results") or [] if isinstance(x, dict)]
        rows.extend(batch)
        total = body.get("total_hits")
        if not batch or (isinstance(total, int) and len(rows) >= total):
            break
    return rows


def parse(row: dict[str, Any], lst: str) -> dict[str, Any] | None:
    """A short product on one of our indices, or None."""
    info, etp, ko = row.get("instrument_info") or {}, row.get("etp_info") or {}, row.get("ko_info") or {}
    if str(etp.get("direction") or "").lower() != "short" or not info.get("isin") or info.get("is_tradable") is False:
        return None
    idx = next((d for d in INDICES if etp.get("underlying_name") == d.nordnet_underlying), None)
    if idx is None:
        return None
    kind = LISTS[lst]
    name = str(info.get("name") or "")
    if kind == "turbo" and not name.upper().startswith("T SHRT"):
        kind = "mini" if name.upper().startswith("MINI") else kind
    lev = None
    if kind == "bear":
        m = FACTOR.search(name)
        lev = float(m.group(1).replace(",", ".")) if m else None
    return {
        "isin": str(info["isin"]).upper(),
        "name": name,
        "kind": kind,
        "direction": "short",
        "index_key": idx.key,
        "leverage": lev,
        "financing_level": _num(ko.get("financial_level")),
        "barrier": _num(ko.get("barrier_price")),
        "currency": info.get("currency"),
        "issuer": info.get("issuer_name"),
        "nordnet_id": info.get("instrument_id"),
    }


def refresh_products(db: Session, client: OutboundClient) -> dict[str, int]:
    """Replace the catalogue with today's short products; keeps the old one when nothing came back."""
    found: dict[str, dict[str, Any]] = {}
    for lst in LISTS:
        for d in INDICES:
            for row in _rows(client, lst, d.search):
                p = parse(row, lst)
                if p:
                    found[p["isin"]] = p
    if not found:
        return {"products": 0}
    now = utcnow()
    db.execute(delete(HedgeProduct))
    for p in found.values():
        db.add(HedgeProduct(**p, seen_at=now))
    db.flush()
    return {"products": len(found)}
