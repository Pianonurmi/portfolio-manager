"""Which ETFs Nordnet Finland sells: its public ETF list (the one behind nordnet.fi "ETF-listat"), matched by ISIN.

Nordea publishes no such list, so availability there cannot be checked; its price list puts every ETF under a 1 % fee
cap. The Nordnet list also says whether monthly saving is possible and how many Nordnet customers own the ETF.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument
from app.modules.screener.models import EtfListing
from app.platform.outbound import OutboundClient

BASE = "https://www.nordnet.fi/api/2/instrument_search/query"
URL = f"{BASE}/etflist"
# the lists that carry a collateral rate (instrument_pawn_percentage) for what can be pledged for credit
COLLATERAL_LISTS = ("stocklist", "fundlist", "etflist")
PAGE = 500
MAX_PAGES = 20
# Nordnet's market ids for the venues its ETF list uses
MARKETS = {4: "XETR", 11: "XSTO", 14: "XCSE", 15: "XOSL", 24: "XHEL"}
HEADERS = {"client-id": "NEXT", "Accept": "application/json"}


def fetch(client: OutboundClient) -> dict[str, dict[str, Any]]:
    """ISIN -> how Nordnet sells it; raises on provider errors so a failed fetch never clears the flags."""
    out: dict[str, dict[str, Any]] = {}
    for page in range(MAX_PAGES):
        r = client.get(URL, params={"limit": PAGE, "offset": page * PAGE}, headers=HEADERS)
        r.raise_for_status()
        body = r.json()
        batch = [x for x in body.get("results") or [] if isinstance(x, dict)]
        for row in batch:
            info = row.get("instrument_info") or {}
            isin = str(info.get("isin") or "").upper()
            if not isin or info.get("is_tradable") is False:
                continue
            market = (row.get("market_info") or {}).get("market_id")
            item = {
                "symbol": info.get("symbol"),
                "exchange": MARKETS.get(market) if isinstance(market, int) else None,
                "currency": info.get("currency"),
                "monthly_saving": bool(info.get("is_monthly_saveable")),
                "owners": (row.get("statistical_info") or {}).get("number_of_owners"),
            }
            # the same ETF on two venues: keep the euro line, then the most owned
            prev = out.get(isin)
            if prev is None or (item["currency"] == "EUR", item["owners"] or 0) > (
                prev["currency"] == "EUR",
                prev["owners"] or 0,
            ):
                out[isin] = item
        total = body.get("total_hits")
        if not batch or (isinstance(total, int) and (page + 1) * PAGE >= total):
            break
    return out


def refresh(db: Session, client: OutboundClient) -> dict[str, int]:
    """Mark the screener rows Nordnet sells; leaves every flag as it was when the list comes back empty."""
    sold = fetch(client)
    stats = {"nordnet": len(sold), "matched": 0}
    if not sold:
        return stats
    for e in db.scalars(select(EtfListing)):
        brokers = {k: v for k, v in (e.brokers or {}).items() if k != "nordnet"}
        if e.isin in sold:
            brokers["nordnet"] = sold[e.isin]
            stats["matched"] += 1
        if brokers != (e.brokers or {}):
            e.brokers = brokers
    db.flush()
    return stats


def _pages(client: OutboundClient, url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(60):
        r = client.get(url, params={"limit": PAGE, "offset": page * PAGE}, headers=HEADERS)
        r.raise_for_status()
        body = r.json()
        batch = [x for x in body.get("results") or [] if isinstance(x, dict)]
        rows.extend(batch)
        total = body.get("total_hits")
        if not batch or (isinstance(total, int) and len(rows) >= total):
            break
    return rows


def fetch_collateral(client: OutboundClient) -> dict[str, dict[str, Any]]:
    """ISIN -> {pawn (fraction), symbol, kind}: Nordnet's published minimum collateral rate (lainoitusaste)."""
    out: dict[str, dict[str, Any]] = {}
    for name in COLLATERAL_LISTS:
        for row in _pages(client, f"{BASE}/{name}"):
            info = row.get("instrument_info") or {}
            isin = str(info.get("isin") or "").upper()
            pawn = info.get("instrument_pawn_percentage")
            if not isin or not isinstance(pawn, int | float):
                continue
            item = {
                "pawn": round(float(pawn) / 100, 4),
                "symbol": info.get("symbol"),
                "kind": name.removesuffix("list"),
            }
            # several listings of one ISIN: the highest rate is the one a holding there would get
            if isin not in out or item["pawn"] > out[isin]["pawn"]:
                out[isin] = item
    return out


def refresh_collateral(db: Session, client: OutboundClient, on: str) -> dict[str, int]:
    """Store Nordnet's collateral rate on every instrument with an ISIN; an instrument Nordnet does not list is
    marked as such (0 % collateral). Leaves everything as it was when the lists come back empty."""
    rates = fetch_collateral(client)
    stats = {"listed": len(rates), "matched": 0, "unlisted": 0}
    if not rates:
        return stats
    for inst in db.scalars(select(Instrument).where(Instrument.isin.is_not(None), Instrument.type != "cash")):
        found = rates.get((inst.isin or "").upper())
        if found:
            stats["matched"] += 1
            nn = {**found, "as_of": on}
        else:
            stats["unlisted"] += 1
            nn = {"pawn": 0.0, "symbol": None, "kind": None, "as_of": on, "unlisted": True}
        if (inst.meta or {}).get("nordnet") != nn:
            inst.meta = {**(inst.meta or {}), "nordnet": nn}
    db.flush()
    return stats
