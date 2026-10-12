"""Fund factsheets / KIDs read by the local model into an instrument profile (source "factsheet").

Used where Morningstar has no look-through data: the X-ray takes each profile field from the best source, so a
factsheet fills exactly what Morningstar lacks. The model reads the printed figures (percent numbers); this module
turns them into the profile's shape (fractions, profile region/sector labels, ISO-3 countries) and flags every
figure that is not on the page text. Names that cannot be mapped stay unmapped: nothing is guessed.
"""

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai import statements
from app.modules.ai.attachments import Unit
from app.modules.core.models import Instrument
from app.modules.portfolio import profiles

MAX_CHARS = 12000
SCHEMA_PCT = {"type": ["number", "null"]}
_LIST = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "pct": {"type": ["number", "null"]}},
        "required": ["name", "pct"],
    },
}
FACTSHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fund_name": {"type": ["string", "null"]},
        "isins": {"type": "array", "items": {"type": "string"}},
        "share_class": {"type": ["string", "null"]},
        "as_of": {"type": ["string", "null"]},
        "ongoing_charge": SCHEMA_PCT,
        "asset_allocation": {
            "type": "object",
            "properties": {k: SCHEMA_PCT for k in ("equity", "bond", "cash", "other")},
            "required": ["equity", "bond", "cash", "other"],
        },
        "regions": _LIST,
        "countries": _LIST,
        "sectors": _LIST,
        "cap_size": _LIST,
        "bond": {
            "type": "object",
            "properties": {
                "duration": SCHEMA_PCT,
                "ytm": SCHEMA_PCT,
                "credit_quality": {"type": ["string", "null"]},
            },
            "required": ["duration", "ytm", "credit_quality"],
        },
    },
    "required": [
        "fund_name",
        "isins",
        "share_class",
        "as_of",
        "ongoing_charge",
        "asset_allocation",
        "regions",
        "countries",
        "sectors",
        "cap_size",
        "bond",
    ],
}
PROMPT = (
    "Extract the figures of this fund factsheet or KID exactly as printed. ongoing_charge = ongoing charges / TER / "
    "juoksevat kulut in percent. asset_allocation = share of equity (osakkeet), bond (korot, joukkolainat), cash "
    "(käteinen) and other, in percent. regions, countries, sectors and cap_size = lists of {name, pct} with the "
    "name as printed and pct the percent number; list only breakdowns the document shows. bond = modified or "
    "effective duration in years, yield to maturity in percent and the average credit quality as printed. "
    "isins = every ISIN printed for the fund's share classes. Percentages are percent numbers (12.5 for 12,5 %), "
    "never fractions. Never guess: use null or an empty list for anything not shown. " + statements.RULES
)

# ---- name tables ---------------------------------------------------------------------------------------

REGION_ALIASES: dict[str, str] = {
    **{r.lower(): r for r in profiles.REGION_CODES.values()},
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "yhdysvallat": "United States",
    "euro area": "Eurozone",
    "eurozone": "Eurozone",
    "euroalue": "Eurozone",
    "euro zone": "Eurozone",
    "europe excluding euro": "Europe ex-Euro",
    "europe ex euro": "Europe ex-Euro",
    "eurooppa ilman euroaluetta": "Europe ex-Euro",
    "eurooppa ex-euro": "Europe ex-Euro",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "iso-britannia": "United Kingdom",
    "yhdistynyt kuningaskunta": "United Kingdom",
    "japani": "Japan",
    "kanada": "Canada",
    "developed asia": "Asia Developed",
    "asia developed": "Asia Developed",
    "kehittyneet aasian maat": "Asia Developed",
    "kehittynyt aasia": "Asia Developed",
    "emerging asia": "Asia Emerging",
    "asia emerging": "Asia Emerging",
    "kehittyvä aasia": "Asia Emerging",
    "kehittyvät aasian maat": "Asia Emerging",
    "emerging europe": "Europe Emerging",
    "kehittyvä eurooppa": "Europe Emerging",
    "latinalainen amerikka": "Latin America",
    "middle east": "Middle East",
    "lähi-itä": "Middle East",
    "afrikka": "Africa",
    "australasia": "Australasia",
    "oseania": "Australasia",
}

SECTOR_ALIASES: dict[str, str] = {
    **{s.lower(): s for s in profiles.SECTOR_CODES.values()},
    "information technology": "Technology",
    "it": "Technology",
    "teknologia": "Technology",
    "tietotekniikka": "Technology",
    "financials": "Financial Services",
    "financial": "Financial Services",
    "rahoitus": "Financial Services",
    "rahoitussektori": "Financial Services",
    "health care": "Healthcare",
    "terveydenhuolto": "Healthcare",
    "terveys": "Healthcare",
    "consumer discretionary": "Consumer Cyclical",
    "kulutuksen syklinen": "Consumer Cyclical",
    "syklinen kulutus": "Consumer Cyclical",
    "kestokulutustavarat": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "defensiivinen kulutus": "Consumer Defensive",
    "päivittäistavarat": "Consumer Defensive",
    "materials": "Basic Materials",
    "raaka-aineet": "Basic Materials",
    "perusteollisuus": "Basic Materials",
    "teollisuus": "Industrials",
    "energia": "Energy",
    "kiinteistöt": "Real Estate",
    "kiinteistö": "Real Estate",
    "communication": "Communication Services",
    "telecommunication services": "Communication Services",
    "telecommunications": "Communication Services",
    "viestintäpalvelut": "Communication Services",
    "telekommunikaatio": "Communication Services",
    "utility": "Utilities",
    "yleishyödylliset palvelut": "Utilities",
    "energia- ja vesihuolto": "Utilities",
}
FI_COUNTRIES: dict[str, str] = {
    "yhdysvallat": "USA",
    "kanada": "CAN",
    "iso-britannia": "GBR",
    "yhdistynyt kuningaskunta": "GBR",
    "japani": "JPN",
    "australia": "AUS",
    "suomi": "FIN",
    "saksa": "DEU",
    "ranska": "FRA",
    "alankomaat": "NLD",
    "irlanti": "IRL",
    "itävalta": "AUT",
    "belgia": "BEL",
    "espanja": "ESP",
    "italia": "ITA",
    "ruotsi": "SWE",
    "norja": "NOR",
    "tanska": "DNK",
    "sveitsi": "CHE",
    "kiina": "CHN",
    "intia": "IND",
    "etelä-korea": "KOR",
    "korea": "KOR",
    "taiwan": "TWN",
    "brasilia": "BRA",
    "meksiko": "MEX",
    "puola": "POL",
    "viro": "EST",
    "tšekki": "CZE",
    "unkari": "HUN",
    "turkki": "TUR",
    "etelä-afrikka": "ZAF",
    "hongkong": "HKG",
    "singapore": "SGP",
    "uusi-seelanti": "NZL",
    "luxemburg": "LUX",
    "portugali": "PRT",
    "kreikka": "GRC",
}
COUNTRY_ALIASES: dict[str, str] = {
    **{v[1].lower(): k for k, v in profiles.COUNTRIES.items()},
    **{k.lower(): k for k in profiles.COUNTRIES},
    **FI_COUNTRIES,
    "united states of america": "USA",
    "us": "USA",
    "uk": "GBR",
    "great britain": "GBR",
    "korea": "KOR",
    "czech republic": "CZE",
    "holland": "NLD",
    "the netherlands": "NLD",
}
CAP_ALIASES = {
    "giant": "giant",
    "mega": "giant",
    "mega cap": "giant",
    "large": "large",
    "large cap": "large",
    "suuret yhtiöt": "large",
    "mid": "mid",
    "medium": "mid",
    "mid cap": "mid",
    "keskisuuret yhtiöt": "mid",
    "small": "small",
    "small cap": "small",
    "pienet yhtiöt": "small",
    "micro": "micro",
    "micro cap": "micro",
}
ASSET_KEYS = ("equity", "bond", "cash", "other")


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace(" ", " ")).strip().lower().rstrip(".:")


def _label(table: dict[str, str], name: str) -> str | None:
    k = _key(name)
    return table.get(k) or table.get(re.sub(r"\s*\(.*\)$", "", k))


def _frac(v: Any) -> Decimal | None:
    n = statements.num(v)
    return None if n is None else n / 100


def _items(raw: Any, table: dict[str, str], nums: list[Decimal] | None) -> list[dict[str, Any]]:
    out = []
    for e in raw or []:
        if not isinstance(e, dict) or not str(e.get("name") or "").strip():
            continue
        pct = statements.num(e.get("pct"))
        if pct is None:
            continue
        out.append(
            {
                "name": str(e["name"]).strip()[:120],
                "label": _label(table, str(e["name"])),
                "weight": float(pct / 100),
                "found": None if nums is None else statements._in_text(pct, nums),  # noqa: SLF001
            }
        )
    return out


def normalise(raw: dict[str, Any], text: str | None) -> dict[str, Any]:
    """The model's answer in profile terms. `found` per figure: its number is on the page (None: read from an
    image, nothing to check against). Unmapped names keep label None."""
    nums = None if text is None else statements.numbers_in(text)

    def found(pct: Any) -> bool | None:
        n = statements.num(pct)
        if nums is None or n is None:
            return None
        return statements._in_text(n, nums)  # noqa: SLF001

    checks: dict[str, bool | None] = {}
    ter = _frac(raw.get("ongoing_charge"))
    checks["ter"] = found(raw.get("ongoing_charge"))
    alloc = raw.get("asset_allocation") or {}
    asset = {k: float(f) for k in ASSET_KEYS if (f := _frac(alloc.get(k))) is not None}
    for k in asset:
        checks[f"asset_classes.{k}"] = found(alloc.get(k))
    b = raw.get("bond") or {}
    bond: dict[str, Any] = {}
    if (d := statements.num(b.get("duration"))) is not None:
        bond["modified_duration"] = float(d)
        checks["bond.modified_duration"] = found(d)
    if (y := _frac(b.get("ytm"))) is not None:
        bond["ytm"] = float(y)
        checks["bond.ytm"] = found(b.get("ytm"))
    if str(b.get("credit_quality") or "").strip():
        bond["credit_quality"] = str(b["credit_quality"]).strip()[:20]
    isins = [
        i
        for i in (str(x).strip().upper().replace(" ", "") for x in raw.get("isins") or [])
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", i)
    ]
    d_as_of = statements.statement_date(raw.get("as_of"))
    return {
        "name": str(raw.get("fund_name") or "")[:255] or None,
        "isins": isins,
        "share_class": str(raw.get("share_class") or "")[:60] or None,
        "as_of": d_as_of.isoformat() if d_as_of else None,
        "ter": None if ter is None else float(ter),
        "asset_classes": asset,
        "regions": _items(raw.get("regions"), REGION_ALIASES, nums),
        "countries": _items(raw.get("countries"), COUNTRY_ALIASES, nums),
        "sectors": _items(raw.get("sectors"), SECTOR_ALIASES, nums),
        "cap_size": _items(raw.get("cap_size"), CAP_ALIASES, nums),
        "bond": bond,
        "checks": checks,
    }


def has_content(fs: dict[str, Any]) -> bool:
    return bool(
        fs.get("ter") is not None
        or fs.get("asset_classes")
        or fs.get("bond")
        or any(fs.get(k) for k in ("regions", "countries", "sectors", "cap_size"))
    )


def options() -> dict[str, Any]:
    """The labels a person may assign to a name the alias tables did not know."""
    return {
        "regions": sorted(set(profiles.REGION_CODES.values())),
        "sectors": sorted(set(profiles.SECTOR_CODES.values())),
        "countries": sorted(
            ({"code": k, "name": v[1]} for k, v in profiles.COUNTRIES.items()), key=lambda c: c["name"]
        ),
        "cap_size": list(profiles.CAP_CODES.values()),
    }


def profile_data(fs: dict[str, Any]) -> dict[str, Any]:
    """Profile data from a (possibly hand-edited) normalised factsheet: unmapped rows are left out, rows with the
    same label add up, a total a hair over 100 % (rounded figures) is scaled to 100. Raises ValueError."""
    patch: dict[str, Any] = {}
    if fs.get("ter") is not None:
        patch["ter"] = float(fs["ter"])
    if fs.get("asset_classes"):
        patch["asset_classes"] = {k: float(v) for k, v in fs["asset_classes"].items()}
    for key in ("regions", "countries", "sectors", "cap_size"):
        w: dict[str, float] = {}
        for it in fs.get(key) or []:
            if it.get("label") and it.get("weight") is not None:
                w[it["label"]] = w.get(it["label"], 0.0) + float(it["weight"])
        total = sum(w.values())
        if 1.0001 < total <= 1.03:
            w = {k: v / total for k, v in w.items()}
        if w:
            patch[key] = {k: round(v, 6) for k, v in w.items()}
    if fs.get("bond"):
        patch["bond"] = dict(fs["bond"])
    for key in ("asset_classes",):
        total = sum((patch.get(key) or {}).values())
        if 1.0001 < total <= 1.03:
            patch[key] = {k: round(v / total, 6) for k, v in patch[key].items()}
    if not patch:
        raise ValueError("Nothing to save: the factsheet has no usable figures")
    return profiles.clean_manual(patch)


# ---- reading -----------------------------------------------------------------------------------------------


def read(db: Session, units: list[Unit], client: Any = None) -> tuple[dict[str, Any], str]:
    """(normalised factsheet, model). Text pages are read together; scans one page at a time (first three)."""
    text_units = [u for u in units if not u.is_image]
    if text_units:
        text = "\n".join(u.text or "" for u in text_units)[:MAX_CHARS]
        raw, r = statements._ask(db, PROMPT, Unit("the whole document", text=text), "", FACTSHEET_SCHEMA, client)  # noqa: SLF001
        return normalise(raw, text), r.model
    merged: dict[str, Any] = {}
    model = ""
    for u in units[:3]:
        raw, r = statements._ask(db, PROMPT, u, "", FACTSHEET_SCHEMA, client)  # noqa: SLF001
        model = r.model
        for k, v in raw.items():
            if not merged.get(k) or (isinstance(v, dict) and not any(merged[k].values())):
                merged[k] = v
    return normalise(merged, None), model


def match_instrument(db: Session, fs: dict[str, Any]) -> int | None:
    """The held instrument whose ISIN is printed on the factsheet."""
    if not fs["isins"]:
        return None
    return db.scalar(select(Instrument.id).where(Instrument.isin.in_(fs["isins"])).order_by(Instrument.id).limit(1))
