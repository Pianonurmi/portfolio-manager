"""Yahoo Finance chart and search endpoints (unofficial; may change — D3 keeps providers pluggable)."""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from app.platform.outbound import OutboundClient

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"

# Yahoo symbol suffixes of venues trading in a currency (used when search lists another venue only).
SUFFIXES = {"EUR": ["AS", "DE", "MI", "PA", "HE"], "SEK": ["ST"], "NOK": ["OL"], "DKK": ["CO"], "USD": []}

# Preferred exchanges per trading currency when resolving an ISIN to a symbol.
PREFERRED = {
    "EUR": ["HEL", "AMS", "GER", "FRA", "PAR", "MIL", "STU", "MUN"],
    "USD": ["NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS"],
    "SEK": ["STO"],
    "NOK": ["OSL"],
    "DKK": ["CPH"],
}


def history(symbol: str, start: date, client: OutboundClient) -> tuple[str | None, list[tuple[date, Decimal]]]:
    """Daily closes in the instrument's own (unadjusted-for-splits) terms. Returns (currency, [(date, close)])."""
    ccy, out, _ = history_with_splits(symbol, start, client)
    return ccy, out


def history_with_splits(
    symbol: str, start: date, client: OutboundClient
) -> tuple[str | None, list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
    """As history(), plus split events [(date, ratio new/old)] reported by the provider."""
    ccy, out, splits, _ = history_with_events(symbol, start, client)
    return ccy, out, splits


def history_with_events(
    symbol: str, start: date, client: OutboundClient
) -> tuple[str | None, list[tuple[date, Decimal]], list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
    """(currency, closes, splits [(date, ratio new/old)], dividends [(ex-date, per-unit amount)]).

    Closes and dividends are both un-adjusted for later splits, i.e. as the units traded on that day."""
    p1 = int(datetime.combine(start, time(), UTC).timestamp())
    p2 = int(datetime.now(UTC).timestamp()) + 86400
    r = client.get(
        CHART.format(symbol=symbol), params={"period1": p1, "period2": p2, "interval": "1d", "events": "div|split"}
    )
    if r.status_code == 404:
        return None, [], [], []
    r.raise_for_status()
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        return None, [], [], []
    data: dict[str, Any] = res[0]
    meta = data.get("meta", {})
    ts = data.get("timestamp") or []
    closes = ((data.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    gmtoff = int(meta.get("gmtoffset") or 0)
    # Compare by exchange-local date: the split day's own bar can be timestamped before the split event.
    splits = sorted(
        (
            (
                datetime.fromtimestamp(int(s["date"]) + gmtoff, UTC).date(),
                Decimal(str(s["numerator"])) / Decimal(str(s["denominator"])),
            )
            for s in ((data.get("events") or {}).get("splits") or {}).values()
        ),
        key=lambda x: x[0],
    )
    events = data.get("events") or {}
    divs: list[tuple[date, Decimal]] = []
    for e in (events.get("dividends") or {}).values():
        try:
            amount = Decimal(str(round(float(e["amount"]), 8)))
            d = datetime.fromtimestamp(int(e["date"]) + gmtoff, UTC).date()
        except (KeyError, TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        # back-adjusted for later splits exactly like the closes
        for sdate, ratio in splits:
            if d < sdate:
                amount *= ratio
        divs.append((d, amount.quantize(Decimal("0.00000001"))))
    divs.sort()
    out: list[tuple[date, Decimal]] = []
    for t, c in zip(ts, closes, strict=False):
        if c is None:
            continue
        # provider sends binary floats (229.2799987…); round to the exchange's precision before exact maths
        price = Decimal(str(round(c, 4 if abs(c) >= 1 else 8)))
        d = datetime.fromtimestamp(t + gmtoff, UTC).date()
        # Yahoo back-adjusts history for splits; undo that so price × booked quantity is right on every date.
        for sdate, ratio in splits:
            if d < sdate:
                price *= ratio
        out.append((d, price.quantize(Decimal("0.00000001"))))
    ccy = meta.get("currency")
    if ccy == "GBp":  # pence
        out = [(d, p / 100) for d, p in out]
        divs = [(d, a / 100) for d, a in divs]
        ccy = "GBP"
    return ccy, out, splits, divs


def candidates(isin: str, currency: str, client: OutboundClient) -> list[str]:
    """Symbols for an ISIN, best first: preferred exchanges for the currency, then Yahoo's own order."""
    r = client.get(SEARCH, params={"q": isin, "quotesCount": 10, "newsCount": 0, "listsCount": 0})
    if r.status_code != 200:
        return []
    quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
    pref = PREFERRED.get(currency, [])

    def rank(q: dict[str, Any]) -> int:
        ex = q.get("exchange", "")
        return pref.index(ex) if ex in pref else 50 + quotes.index(q)

    return [str(q["symbol"]) for q in sorted(quotes, key=rank)]


def resolve_isin(isin: str, currency: str, client: OutboundClient) -> str | None:
    c = candidates(isin, currency, client)
    return c[0] if c else None


def quote_currency(symbol: str, client: OutboundClient) -> str | None:
    r = client.get(CHART.format(symbol=symbol), params={"range": "5d", "interval": "1d"})
    if r.status_code != 200:
        return None
    res = (r.json().get("chart") or {}).get("result") or []
    return res[0].get("meta", {}).get("currency") if res else None


def resolve_matching(isin: str, currency: str, client: OutboundClient, tries: int = 4) -> str | None:
    """Prefer a listing quoted in the instrument's own currency (e.g. IWDA.AS in EUR over IWDA.L in USD)."""
    cands = candidates(isin, currency, client)
    for sym in cands[:tries]:
        if quote_currency(sym, client) == currency:
            return sym
    # Yahoo's ISIN search often lists only one venue; try the same ticker where it trades in our currency.
    for base in dict.fromkeys(c.split(".")[0] for c in cands[:2]):
        for suffix in SUFFIXES.get(currency, []):
            sym = f"{base}.{suffix}"
            if sym not in cands and quote_currency(sym, client) == currency:
                return sym
    return cands[0] if cands else None
