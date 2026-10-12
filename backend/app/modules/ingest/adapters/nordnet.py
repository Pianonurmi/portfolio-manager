"""Nordnet transaction export ("Tapahtumat ja laskelmat" → Vie CSV).

Built to tolerate the variants Nordnet ships: UTF-16 or UTF-8, tab or semicolon separated, Finnish,
English or Swedish headers, and repeated currency columns ("Valuutta") that belong to the amount
column before them. Unknown transaction types are not guessed: they go to review and the user's
choice is remembered (SourceMapping kind="txn_type").
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.platform.money import D

SOURCE = "nordnet"
ADAPTER = "nordnet-csv v1"

HEADER_ALIASES: dict[str, list[str]] = {
    "id": ["id"],
    "booking_date": ["kirjauspäivä", "booking day", "booking date", "bokföringsdag", "bokføringsdag"],
    "trade_date": ["kauppapäivä", "trade day", "trade date", "affärsdag", "handelsdag"],
    "settle_date": ["maksupäivä", "settlement date", "settlement day", "likviddag", "oppgjørsdag"],
    "portfolio": ["salkku", "portfolio", "depå", "depot", "konto"],
    "type": ["tapahtumatyyppi", "transaction type", "transaktionstyp", "transaksjonstype"],
    "security": ["arvopaperi", "security", "värdepapper", "verdipapir"],
    "isin": ["isin"],
    "quantity": ["määrä", "quantity", "antal", "antall"],
    "price": ["kurssi", "price", "kurs"],
    "interest": ["korko", "interest", "ränta", "rente"],
    "fees": ["kokonaiskulut", "total fees", "total avgift", "totalt avgift", "totala avgifter", "kulut"],
    "amount": ["summa", "amount", "belopp", "beløp"],
    "purchase_value": ["hankinta-arvo", "purchase value", "inköpsvärde", "kjøpsverdi"],
    "result": ["tulos", "result", "resultat"],
    "total_quantity": ["kokonaismäärä", "total quantity", "totalt antal", "totalt antall"],
    "balance": ["saldo", "balance"],
    "fx_rate": ["vaihtokurssi", "exchange rate", "växlingskurs", "vekslingskurs"],
    "text": ["tapahtumateksti", "transaction text", "transaktionstext", "transaksjonstekst"],
    "cancel_date": ["mitätöintipäivä", "cancellation date", "makuleringsdatum", "makuleringsdato"],
    "verification": ["vahvistusnumero", "verification number", "verifikationsnummer", "laskelman numero"],
    "brokerage": ["välityspalkkio", "brokerage", "courtage", "kurtasje"],
}
CURRENCY_HEADERS = {"valuutta", "currency", "valuta"}
ALIAS_TO_FIELD = {alias: f for f, aliases in HEADER_ALIASES.items() for alias in aliases}

# Transaction types → ledger kind. Matching is on the upper-cased type text.
TYPE_ALIASES: dict[str, list[str]] = {
    "buy": ["OSTO", "BUY", "BOUGHT", "KÖPT", "KJØPT", "OSTO, MERKINTÄ", "MERKINTÄ"],
    # a redemption (squeeze-out, rights sold for the holder) or a fraction sold after a merger comes as two rows:
    # OTTO takes the units out with the proceeds (booked as the sale), KÄT credits the same cash (ignored below)
    "sell": ["MYYNTI", "SELL", "SOLD", "SÅLT", "SOLGT", "LUNASTUS", "LUNASTUS AP OTTO", "DESIM KIRJAUS OTTO"],
    "ignore": ["LUNASTUS AP KÄT.", "LUNASTUS AP KÄT", "DESIM. ULOSKIRJ. KÄT", "DESIM ULOSKIRJ KÄT"],
    "dividend": ["OSINKO", "DIVIDEND", "UTDELNING", "UTBYTTE", "OSINGON MAKSU"],
    "interest": [
        "KORKO",
        "TALLETUSKORKO",
        "INTEREST",
        "RÄNTA",
        "RÄNTA INSÄTTNING",
        "KORKOHYVITYS",
        "LAINAN KORKO",
        "LAINAKORKO",
        "DEBIT INTEREST",
        "CREDIT INTEREST",
    ],
    "tax": [
        "ENNAKKOPIDÄTYS",
        "LÄHDEVERO",
        "WITHHOLDING TAX",
        "FOREIGN TAX",
        "KUPONGSKATT",
        "PRELIMINÄRSKATT",
        "UTL KUPSKATT",
        "LÄHDEVERON PALAUTUS",
    ],
    "deposit": ["TALLETUS", "DEPOSIT", "INSÄTTNING", "INNSKUDD", "PANKKISIIRTO SISÄÄN"],
    "withdrawal": ["NOSTO", "WITHDRAWAL", "UTTAG", "PANKKISIIRTO ULOS"],
    "fee": [
        "PALKKIO",
        "MAKSU",
        "FEE",
        "AVGIFT",
        "SÄILYTYSMAKSU",
        "PLATFORM FEE",
        "ETF_KK SÄÄSTÖN PALVELUMAKSU",  # monthly ETF savings plan service fee
        "PER ETF-KK SÄÄSTÖN PALVEMAKSU",
    ],
    "fx": [
        "VALUUTAN OSTO",
        "VALUUTAN MYYNTI",
        "VALUUTANVAIHTO",
        "CURRENCY EXCHANGE",
        "VALUTAVÄXLING",
        "VALUTAKÖP",
        "VALUTASÅLD",
        "VAIHTO VALUUTTA",
    ],
    "capital_return": ["PÄÄOMANPALAUTUS", "PÄÄOMAN PALAUTUS", "RETURN OF CAPITAL", "KAPITALÅTERBETALNING"],
    "security_in": [
        "VP-SIIRTO SISÄÄN",
        "ARVOPAPERISIIRTO SISÄÄN",
        "SECURITY TRANSFER IN",
        "INLEVERANS",
        "INSTRUMENTIN SIIRTO SISÄÄN",
        # corporate actions, units in (JÄTTÖ): IPO subscription placeholder, detached rights, rights issue,
        # the new line of an exchange or merger (its purchase value column carries the old line's cost)
        "JÄTTÖ SIIRTO",
        "JÄTTÖ IRROTUS",
        "EMISSIO AP JÄTTÖ",
        "VAIHTO AP-JÄTTÖ",
    ],
    "security_out": [
        "VP-SIIRTO ULOS",
        "ARVOPAPERISIIRTO ULOS",
        "SECURITY TRANSFER OUT",
        "UTLEVERANS",
        "INSTRUMENTIN SIIRTO ULOS",
        # corporate actions, units out (OTTO): placeholder removed, rights used, the old line of an exchange
        "AP OTTO",
        "OSAKEANTI AP OTTO",
        "VAIHTO AP-OTTO",
    ],
    "split": ["SPLIT", "AKTIESPLIT", "OSAKKEIDEN JAKO", "SPLIT IN", "SPLIT UT", "SPLIT AP JÄTTÖ", "SPLIT AP OTTO"],
    "transfer_in": ["SISÄINEN SIIRTO SISÄÄN", "INTERNAL TRANSFER IN", "ÖVERFÖRING IN"],
    "transfer_out": ["SISÄINEN SIIRTO ULOS", "INTERNAL TRANSFER OUT", "ÖVERFÖRING UT"],
}
TYPE_TO_KIND = {alias: k for k, aliases in TYPE_ALIASES.items() for alias in aliases}
NEEDS_INSTRUMENT = {"buy", "sell", "split", "security_in", "security_out"}
OPTIONAL_INSTRUMENT = {"dividend", "tax", "capital_return"}


class FormatError(ValueError):
    pass


def decode(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    sample = content[:2000]
    if sample.count(b"\x00") > len(sample) // 4:
        return content.decode("utf-16-le")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.replace("﻿", "").strip().strip('"').lower())


def sniff(text: str) -> bool:
    """True if this looks like a Nordnet transaction export."""
    first = text.splitlines()[0] if text else ""
    heads = {_norm(h) for h in re.split(r"[\t;,]", first)}
    hits = sum(1 for h in heads if h in ALIAS_TO_FIELD)
    return hits >= 6 and bool(heads & {"id"}) and bool(heads & set(HEADER_ALIASES["type"]))


def _delimiter(header: str) -> str:
    counts = {d: header.count(d) for d in ("\t", ";", ",")}
    return max(counts, key=lambda d: counts[d])


def map_headers(headers: list[str]) -> list[str]:
    """Canonical name per column; currency columns become '<previous field>_ccy'."""
    out: list[str] = []
    prev: str | None = None
    for i, h in enumerate(headers):
        n = _norm(h)
        if n in CURRENCY_HEADERS:
            name = f"{prev}_ccy" if prev else "currency"
        else:
            name = ALIAS_TO_FIELD.get(n, f"col{i}:{n}")
            prev = name if not name.startswith("col") else prev
        while name in out:
            name += "_"
        out.append(name)
    return out


def parse_date(v: str) -> date | None:
    v = v.strip()
    if not v:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?", v)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", v)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    raise FormatError(f"unrecognised date {v!r}")


def dec(v: str | None) -> Decimal | None:
    if v is None:
        return None
    v = v.strip()
    if v in ("", "-", "—"):
        return None
    return D(v)


@dataclass
class Row:
    row_no: int
    raw: dict[str, str]
    external_id: str | None
    content_hash: str
    parsed: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def parse(content: bytes) -> tuple[list[Row], dict[str, Any]]:
    text = decode(content)
    lines = text.splitlines()
    if not lines:
        raise FormatError("the file is empty")
    delim = _delimiter(lines[0])
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    headers = next(reader)
    fields = map_headers(headers)
    missing = [f for f in ("id", "type", "amount") if f not in fields]
    if missing:
        raise FormatError(f"not a Nordnet transaction export (missing columns: {', '.join(missing)})")
    lang = (
        "fi"
        if "kirjauspäivä" in {_norm(h) for h in headers}
        else "sv"
        if "affärsdag" in {_norm(h) for h in headers}
        else "en"
    )
    rows: list[Row] = []
    for i, values in enumerate(reader, start=1):
        if not any(v.strip() for v in values):
            continue
        raw = {fields[j]: values[j] for j in range(min(len(fields), len(values)))}
        normalized = "\x1f".join(f"{k}={raw[k].strip()}" for k in sorted(raw))
        h = hashlib.sha256(normalized.encode()).hexdigest()
        rid = raw.get("id", "").strip()
        portfolio = raw.get("portfolio", "").strip()
        ext = f"{portfolio}:{rid}" if rid else None
        row = Row(row_no=i, raw=raw, external_id=ext, content_hash=h)
        try:
            row.parsed = _parse_row(raw)
        except (FormatError, ValueError) as exc:
            row.error = str(exc)
        rows.append(row)
    meta = {
        "delimiter": "tab" if delim == "\t" else delim,
        "language": lang,
        "columns": len(fields),
        "rows": len(rows),
        "adapter": f"{ADAPTER} ({lang})",
    }
    return rows, meta


def _s(v: Decimal | date | None) -> str | None:
    return None if v is None else str(v) if isinstance(v, Decimal) else v.isoformat()


def _parse_row(raw: dict[str, str]) -> dict[str, Any]:
    def g(k: str) -> str:
        return raw.get(k, "").strip()

    trade = parse_date(g("trade_date")) or parse_date(g("booking_date"))
    if trade is None:
        raise FormatError("row has no date")
    rid = g("id")
    amount = dec(g("amount")) or Decimal(0)
    amount_ccy = g("amount_ccy") or g("currency") or "EUR"
    return {
        "id": rid,
        "seq": int(rid) if rid.isdigit() else 0,
        "date": _s(trade),
        "booking_date": _s(parse_date(g("booking_date"))),
        "settle_date": _s(parse_date(g("settle_date"))),
        "portfolio": g("portfolio"),
        "raw_type": g("type").upper(),
        "kind": TYPE_TO_KIND.get(g("type").upper()),
        "security": g("security") or None,
        "isin": (g("isin") or None) and g("isin").upper(),
        "quantity": _s(dec(g("quantity"))),
        "price": _s(dec(g("price"))),
        "price_ccy": g("price_ccy") or None,
        "amount": _s(amount),
        "amount_ccy": amount_ccy,
        "fees": _s(dec(g("fees")) or dec(g("brokerage"))),
        "fees_ccy": g("fees_ccy") or g("brokerage_ccy") or amount_ccy,
        "fx_rate": _s(dec(g("fx_rate"))),
        "purchase_value": _s(dec(g("purchase_value"))),
        "result": _s(dec(g("result"))),
        "total_quantity": _s(dec(g("total_quantity"))),
        "balance": _s(dec(g("balance"))),
        "text": g("text") or None,
        "cancelled": bool(g("cancel_date")),
        "cancel_date": g("cancel_date") or None,
    }


def guess_instrument_type(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ("etf", "ucits", "ishares", "xtrackers", "vanguard ftse", "spdr")):
        return "etf"
    if any(w in n for w in ("rahasto", "indeksi", "fund", "fond", "sijoitusrahasto")):
        return "fund"
    return "stock"


ISIN_COUNTRY_CCY = {"US": "USD", "SE": "SEK", "NO": "NOK", "DK": "DKK", "CA": "CAD", "CH": "CHF", "JP": "JPY"}


def guess_currency(p: dict[str, Any]) -> str:
    """Trading currency of a new instrument. Nordnet's Finnish export has no price-currency column, so a
    non-trivial exchange rate plus the ISIN country is the best signal. The user can correct it."""
    if p.get("price_ccy"):
        return str(p["price_ccy"])
    fx = p.get("fx_rate")
    isin = p.get("isin") or ""
    if fx and Decimal(fx) not in (Decimal(0), Decimal(1)) and isin[:2] in ISIN_COUNTRY_CCY:
        return ISIN_COUNTRY_CCY[isin[:2]]
    return str(p.get("amount_ccy") or "EUR")
