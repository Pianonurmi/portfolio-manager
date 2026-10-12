"""Nordnet "holdings by purchase lot" export (Omistukset → Ostoerittäin → Lataa): exact lots, no AI needed.

Columns (Finnish; English names accepted where Nordnet uses them): Tulostuspäivä, Salkku, Hankintapäivä,
Instrumentti, Tunnus, ISIN, Valuutta, Määrä, Hankintahinta (Noteerausvaluutta/EUR), Hankinta-arvo (… / EUR),
Markkina-arvo (… / EUR), Tuotto-%. Numbers use a decimal comma. Each row is one purchase lot.
"""

import csv
import io
import re
from datetime import date
from decimal import Decimal
from typing import Any

from app.modules.ai import extract

ALIASES = {
    "printed": ["tulostuspäivä", "print date", "utskriftsdatum"],
    "portfolio": ["salkku", "portfolio", "depå"],
    "acquired": ["hankintapäivä", "purchase date", "acquisition date", "anskaffningsdatum", "köpdatum"],
    "name": ["instrumentti", "instrument"],
    "symbol": ["tunnus", "symbol", "ticker"],
    "isin": ["isin"],
    "currency": ["valuutta", "currency", "valuta"],
    "quantity": ["määrä", "quantity", "antal"],
    "price_local": ["hankintahinta (noteerausvaluutta)", "purchase price (trading currency)"],
    "cost_local": ["hankinta-arvo (noteerausvaluutta)", "purchase value (trading currency)"],
    "cost_eur": ["hankinta-arvo (eur)", "purchase value (eur)"],
    "value_local": ["markkina-arvo (noteerausvaluutta)", "market value (trading currency)"],
    "value_eur": ["markkina-arvo (eur)", "market value (eur)"],
}
FIELD = {a: f for f, names in ALIASES.items() for a in names}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.replace("﻿", "").strip().strip('"').lower())


def sniff(text: str) -> bool:
    first = text.splitlines()[0] if text.strip() else ""
    heads = {_norm(h) for h in re.split(r"[\t;]", first)}
    found = {FIELD[h] for h in heads if h in FIELD}
    return {"acquired", "name", "quantity", "portfolio"} <= found and bool({"cost_eur", "cost_local"} & found)


def parse(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    first = text.splitlines()[0]
    delim = "\t" if "\t" in first else ";"
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    heads = [FIELD.get(_norm(h), "") for h in next(reader)]
    rows: list[dict[str, Any]] = []
    printed: date | None = None
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        r = {heads[i]: raw[i].strip() for i in range(min(len(heads), len(raw))) if heads[i]}
        n = extract.parse_number
        printed = printed or extract.parse_date(r.get("printed"))
        qty = n(r.get("quantity"))
        value = n(r.get("value_eur")) if r.get("value_eur") else n(r.get("value_local"))
        rows.append(
            {
                "name": r.get("name", "")[:255],
                "isin": (r.get("isin") or "").upper() or None,
                "symbol": r.get("symbol") or None,
                "quantity": qty,
                "price": (value / qty).quantize(Decimal("0.000001")) if value is not None and qty else None,
                "price_ccy": "EUR",
                "value": value,
                "value_ccy": "EUR",
                "cost": n(r.get("cost_eur")) if r.get("cost_eur") else n(r.get("cost_local")),
                "acquired": (d.isoformat() if (d := extract.parse_date(r.get("acquired"))) else None),
                "account": re.sub(r"\D", "", r.get("portfolio", "")) or r.get("portfolio", ""),
                "source": "Nordnet lots export",
                "read_from": "file",
                "in_text": True,
                "status": "ok",
                "reasons": [],
                "currency": r.get("currency"),
            }
        )
    for x in rows:
        if x["quantity"] is None or x["cost"] is None or x["acquired"] is None:
            x["status"], x["reasons"] = "check", ["quantity, purchase value or purchase date missing"]
    header: dict[str, Any] = {
        "institution": "Nordnet",
        "account": rows[0]["account"] if rows else None,
        "as_of": (printed or date.today()).isoformat(),  # noqa: DTZ011
        "cash": {},
        "totals": {},
        "lots": True,
    }
    return header, rows
