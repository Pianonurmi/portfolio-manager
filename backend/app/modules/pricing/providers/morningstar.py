"""Morningstar screener and price history (unofficial; covers funds and Helsinki stocks Yahoo lacks)."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.platform.outbound import OutboundClient

BASE = "https://lt.morningstar.com/api/rest.svc/klr5zyak8x"
SCREENER = f"{BASE}/security/screener"
SERIES = "https://lt.morningstar.com/api/rest.svc/timeseries_price/klr5zyak8x"

ETF_UNIVERSE = "ETEUR$$ALL"
FUND_UNIVERSE = "FOALL$$ALL"
HELSINKI_UNIVERSE = "E0EXG$XHEL"
WORLD_UNIVERSE = "E0WWE$$ALL"
FUND, STOCK, ETF = 2, 3, 22
DATAPOINTS = "SecId|Name|Isin|ExchangeId|Ticker|PriceCurrency|holdingTypeId"
PREFERRED_EXCHANGES = ["XHEL", "XAMS", "XETR", "XFRA", "XPAR", "XMIL", "XSTO"]
FINNISH_MARKERS = {"oyj", "abp", "oy"}
LEGAL_SUFFIXES = FINNISH_MARKERS | {"plc", "ab", "asa", "ag", "inc", "corp", "nv", "sa", "se"}
# their names add these to the company name ("Harvia PLC Ordinary Shares"); ours never do
NOISE_WORDS = {"ordinary", "shares", "share", "registered", "common", "stock", "units"}
# primary home listings per currency for the global stock universe (first is preferred)
HOME_EXCHANGES = {
    "USD": ["XNYS", "XNAS"],
    "SEK": ["XSTO"],
    "GBP": ["XLON"],
    "CHF": ["XSWX"],
    "DKK": ["XCSE"],
    "NOK": ["XOSL"],
}
# fund names are abbreviated differently by every issuer; both sides are expanded before comparing
ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "aggt": ("aggregate",),
    "bd": ("bond",),
    "glb": ("global",),
    "govt": ("government",),
    "h": ("hedged",),
    "hdg": ("hedged",),
    "hedge": ("hedged",),
    "accumulating": ("acc",),
    "distributing": ("dist",),
    "distribution": ("dist",),
    "inc": ("dist",),
    "hacc": ("hedged", "acc"),
    "hdist": ("hedged", "dist"),
    "hinc": ("hedged", "dist"),
}
SHARE_CLASS_MARKERS = {"hedged", "acc", "dist"}
# words that carry no identity in a fund name; ignored when our words must all appear in theirs
FUND_FILLER = {"ucits", "etf", "fund", "core", "the", "index"}
PRICE_TOLERANCE = Decimal("0.015")


@dataclass(frozen=True)
class Match:
    secid: str
    name: str
    isin: str | None
    exchange: str | None
    ticker: str | None
    currency: str | None
    holding_type: int
    universe: str


def search(term: str, universe: str, client: OutboundClient, size: int = 10) -> list[Match]:
    r = client.get(
        SCREENER,
        params={
            "page": 1,
            "pageSize": size,
            "outputType": "json",
            "version": 1,
            "languageId": "en-GB",
            "currencyId": "EUR",
            "universeIds": universe,
            "securityDataPoints": DATAPOINTS,
            "term": term,
        },
    )
    r.raise_for_status()
    out = []
    for row in r.json().get("rows") or []:
        if not row.get("SecId"):
            continue
        try:
            htype = int(row.get("holdingTypeId"))
        except (TypeError, ValueError):
            continue
        out.append(
            Match(
                secid=str(row["SecId"]),
                name=str(row.get("Name") or ""),
                isin=row.get("Isin") or None,
                exchange=str(row["ExchangeId"]).removeprefix("EX$$$$") if row.get("ExchangeId") else None,
                ticker=row.get("Ticker") or None,
                currency=row.get("PriceCurrency") or None,
                holding_type=htype,
                universe=universe,
            )
        )
    return out


def history(m: Match, start: date, currency: str, client: OutboundClient) -> list[tuple[date, Decimal]]:
    """Daily closes (NAV for funds) from `start`, in `currency`."""
    r = client.get(
        SERIES,
        params={
            "id": f"{m.secid}]{m.holding_type}]0]{m.universe}",
            "currencyId": currency,
            "idtype": "Morningstar",
            "frequency": "daily",
            "startDate": start.isoformat(),
            "outputType": "JSON",
        },
    )
    r.raise_for_status()
    secs: list[dict[str, Any]] = (r.json().get("TimeSeries") or {}).get("Security") or []
    out: list[tuple[date, Decimal]] = []
    for row in (secs[0].get("HistoryDetail") or []) if secs else []:
        try:
            out.append((date.fromisoformat(row["EndDate"]), Decimal(str(row["Value"])).quantize(Decimal("0.00000001"))))
        except (KeyError, ValueError, InvalidOperation):
            continue
    return out


# ---- matching -------------------------------------------------------------------------------------


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", name.casefold().replace("&", " and ")).split()


def split_name(name: str) -> tuple[list[str], str | None]:
    """(significant words, share class letter): "Kesko Oyj Class B" and "Kesko Oyj B" both give (["kesko"], "b")."""
    t = [w for w in _tokens(name) if w not in NOISE_WORDS]
    cls = None
    if len(t) >= 3 and t[-2] in ("class", "series") and len(t[-1]) == 1:
        cls, t = t[-1], t[:-2]
    elif len(t) >= 2 and len(t[-1]) == 1 and t[-1].isalpha():
        cls, t = t[-1], t[:-1]
    return [w for w in t if w not in LEGAL_SUFFIXES], cls


def is_finnish_company(name: str) -> bool:
    return bool(FINNISH_MARKERS & set(_tokens(name)))


def _name_ok(ours: str, theirs: str) -> bool:
    ow, ocls = split_name(ours)
    tw, tcls = split_name(theirs)
    if not ow or not tw:
        return False
    if ocls is not None and ocls != tcls:
        return False
    # statements truncate long names, so a multi-word name may be the start of the listed one; a lone word may not
    return ow == tw or (len(ow) >= 2 and tw[: len(ow)] == ow)


def fund_words(name: str, currency: str) -> tuple[list[str], set[str]]:
    """(identity words in order, share-class markers): abbreviations expanded, fillers and our own currency dropped."""
    words: list[str] = []
    for t in _tokens(name):
        words.extend(ABBREVIATIONS.get(t, (t,)))
    markers = set(words) & SHARE_CLASS_MARKERS
    skip = FUND_FILLER | SHARE_CLASS_MARKERS | NOISE_WORDS | {currency.casefold()}
    return list(dict.fromkeys(w for w in words if w not in skip)), markers


def _markers_ok(ours: set[str], theirs: set[str]) -> bool:
    """Their share class must not say the opposite of ours; silence on our side accepts any class."""
    if "hedged" in ours and "hedged" not in theirs:
        return False
    return not (("acc" in ours and "dist" in theirs) or ("dist" in ours and "acc" in theirs))


def _rank_exchange(m: Match) -> int:
    return PREFERRED_EXCHANGES.index(m.exchange) if m.exchange in PREFERRED_EXCHANGES else len(PREFERRED_EXCHANGES)


def _by_listing(rows: list[Match], rank: Callable[[Match], int] = _rank_exchange) -> dict[str, Match]:
    """One row per security (ISIN): the same share on several venues is not ambiguity; the best venue represents it."""
    return {m.isin or m.secid: m for m in sorted(rows, key=rank, reverse=True)}


def _by_isin(isin: str, currency: str, client: OutboundClient) -> Match | None:
    universes = [ETF_UNIVERSE, FUND_UNIVERSE] + ([HELSINKI_UNIVERSE] if isin.startswith("FI") else [])
    for universe in universes:
        rows = [m for m in search(isin, universe, client) if m.isin == isin]
        if rows:
            return min(rows, key=lambda m: (m.currency != currency, _rank_exchange(m)))
    return None


def _home_rank(home: list[str]) -> Callable[[Match], int]:
    return lambda m: home.index(m.exchange) if m.exchange in home else len(home)


def _stock_by_name(name: str, currency: str, universe: str, client: OutboundClient) -> Match | None:
    words, _ = split_name(name)
    if not words:
        return None
    rows = [
        m for m in search(" ".join(words), universe, client, 100) if m.currency == currency and _name_ok(name, m.name)
    ]
    rank: Callable[[Match], int] = _rank_exchange
    if universe == WORLD_UNIVERSE:
        home = HOME_EXCHANGES.get(currency)
        if not home:
            return None  # no known primary venue for this currency: anything else is a guess among foreign listings
        rows = [m for m in rows if m.exchange in home]
        rank = _home_rank(home)

    distinct = _by_listing(rows, rank)
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _close_before(hist: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    prior = [(d, c) for d, c in hist if d <= on]
    return max(prior)[1] if prior else None


def _fund_by_name(name: str, currency: str, client: OutboundClient, hint: tuple[date, Decimal] | None) -> Match | None:
    words, markers = fund_words(name, currency)
    if len(words) < 2:
        return None
    for universe in (ETF_UNIVERSE, FUND_UNIVERSE):
        rows = []
        for m in search(" ".join(words), universe, client, 100):
            if m.currency != currency:
                continue
            theirs, tmarkers = fund_words(m.name, currency)
            if set(words) <= set(theirs) and _markers_ok(markers, tmarkers):
                rows.append(m)
        distinct = _by_listing(rows)
        if len(distinct) == 1:
            return next(iter(distinct.values()))
        if not distinct:
            continue
        if hint is None:
            return None
        # several share classes fit the name: the price the statement printed tells them apart
        on, price = hint
        near = []
        for m in distinct.values():
            close = _close_before(history(m, on - timedelta(days=7), currency, client), on)
            if close is not None and price > 0 and abs(close - price) / price <= PRICE_TOLERANCE:
                near.append(m)
        return near[0] if len(near) == 1 else None
    return None


def _by_name(
    name: str, type: str, currency: str, client: OutboundClient, hint: tuple[date, Decimal] | None
) -> Match | None:
    if is_finnish_company(name) or (type == "stock" and currency == "EUR"):
        return _stock_by_name(name, currency, HELSINKI_UNIVERSE, client)
    if type == "stock":
        return _stock_by_name(name, currency, WORLD_UNIVERSE, client)
    return _fund_by_name(name, currency, client, hint)


def find(
    name: str,
    isin: str | None,
    type: str,
    currency: str,
    client: OutboundClient,
    hint: tuple[date, Decimal] | None = None,
) -> Match | None:
    """Strict lookup: by ISIN equality, else by an unambiguous name; None rather than a guess.

    `hint` is a (date, price) the statement printed; it only separates share classes of the same fund."""
    if type in ("cash", "crypto"):
        return None
    return _by_isin(isin, currency, client) if isin else _by_name(name, type, currency, client, hint)


# ---- profiles ------------------------------------------------------------------------------------


def details(secid: str, client: OutboundClient) -> dict[str, Any]:
    """The ETF/fund snapshot (look-through, costs, bond statistics): one 0.7-1 MB JSON, parsed by the caller."""
    r = client.get(
        f"{BASE}/security_details/{secid}",
        params={
            "viewId": "ETFsnapshot",
            "idtype": "msid",
            "responseViewFormat": "json",
            "languageId": "en-GB",
            "currencyId": "EUR",
        },
    )
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list) or not body or not isinstance(body[0], dict):
        raise ValueError(f"no snapshot for {secid}")
    return body[0]


def stock_facts(isin: str, client: OutboundClient) -> dict[str, Any] | None:
    """Sector, market cap (EUR) and style box of a listed share, from the screener row with this ISIN."""
    universe = HELSINKI_UNIVERSE if isin.startswith("FI") else WORLD_UNIVERSE
    r = client.get(
        SCREENER,
        params={
            "page": 1,
            "pageSize": 10,
            "outputType": "json",
            "version": 1,
            "languageId": "en-GB",
            "currencyId": "EUR",
            "universeIds": universe,
            "securityDataPoints": "SecId|Isin|SectorName|MarketCap|EquityStyleBox",
            "term": isin,
        },
    )
    r.raise_for_status()
    for row in r.json().get("rows") or []:
        if row.get("Isin") == isin and (row.get("SectorName") or row.get("MarketCap")):
            return dict(row)
    return None
