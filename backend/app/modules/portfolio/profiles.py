"""Instrument profiles: what an ETF, fund or share really holds (asset classes, regions, sectors, costs, bonds).

Sources are kept side by side in `instrument_profile`; `effective()` picks per top-level field, so a hand-edited TER
sits on top of Morningstar's regions. All weights are fractions (0-1) of their own sleeve: regions, countries, sectors,
size and style are shares of the equity part, bond countries and credit shares of the bond part.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import Instrument, InstrumentProfile
from app.modules.pricing.providers import morningstar
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient

# manual wins, then what Morningstar reports, then a factsheet someone typed in, then what the ISIN alone tells
SOURCE_ORDER = ("manual", "morningstar", "factsheet", "derived")
# crypto is not a Morningstar class, but the same split carries it
ASSET_KEYS = ("equity", "bond", "cash", "other", "crypto")
WEIGHT_FIELDS = (
    "asset_classes",
    "regions",
    "market",
    "countries",
    "bond_countries",
    "sectors",
    "cap_size",
    "style",
    "credit",
)
SCALAR_FIELDS = ("ter", "style_box", "category", "not_classified")
FIELDS = (*WEIGHT_FIELDS, "bond", *SCALAR_FIELDS)

ASSET_CODES = {"1": "equity", "2": "bond", "3": "cash", "4": "other"}  # Morningstar EUR3 classification
REGION_CODES = {
    "1": "United States",
    "2": "Canada",
    "3": "Latin America",
    "4": "United Kingdom",
    "5": "Eurozone",
    "6": "Europe ex-Euro",
    "7": "Europe Emerging",
    "8": "Africa",
    "9": "Middle East",
    "10": "Japan",
    "11": "Australasia",
    "12": "Asia Developed",
    "13": "Asia Emerging",
}
MARKET_CODES = {"14": "emerging", "15": "developed"}
SECTOR_CODES = {
    "101": "Basic Materials",
    "102": "Consumer Cyclical",
    "103": "Financial Services",
    "104": "Real Estate",
    "205": "Consumer Defensive",
    "206": "Healthcare",
    "207": "Utilities",
    "308": "Communication Services",
    "309": "Energy",
    "310": "Industrials",
    "311": "Technology",
}
CAP_CODES = {"1": "giant", "2": "large", "3": "mid", "4": "small", "5": "micro"}
STYLE_NAMES = [
    "Large Value",
    "Large Blend",
    "Large Growth",
    "Mid Value",
    "Mid Blend",
    "Mid Growth",
    "Small Value",
    "Small Blend",
    "Small Growth",
]
STYLE_CODES = {str(i + 1): n for i, n in enumerate(STYLE_NAMES)}
CREDIT_CODES = {
    "1": "Government/other",
    "2": "AAA",
    "3": "AA",
    "4": "A",
    "5": "BBB",
    "6": "BB",
    "7": "B",
    "8": "Below B",
    "9": "Not rated",
}
# approximation: Morningstar gives the cap in EUR, the bands are the usual round numbers
CAP_BANDS = (("giant", 50_000_000_000), ("large", 10_000_000_000), ("mid", 2_000_000_000), ("small", 300_000_000))

_EURO = "Eurozone"
_XEU = "Europe ex-Euro"
_EME = "Europe Emerging"
_LAT = "Latin America"
_ASD = "Asia Developed"
_ASE = "Asia Emerging"
_ME = "Middle East"
_AFR = "Africa"
# ISO-3 -> (ISO-2, name, Morningstar-style region): for bond countries and ISIN-derived shares
COUNTRIES: dict[str, tuple[str, str, str]] = {
    "USA": ("US", "United States", "United States"),
    "CAN": ("CA", "Canada", "Canada"),
    "GBR": ("GB", "United Kingdom", "United Kingdom"),
    "JPN": ("JP", "Japan", "Japan"),
    "AUS": ("AU", "Australia", "Australasia"),
    "NZL": ("NZ", "New Zealand", "Australasia"),
    "FIN": ("FI", "Finland", _EURO),
    "DEU": ("DE", "Germany", _EURO),
    "FRA": ("FR", "France", _EURO),
    "NLD": ("NL", "Netherlands", _EURO),
    "IRL": ("IE", "Ireland", _EURO),
    "AUT": ("AT", "Austria", _EURO),
    "BEL": ("BE", "Belgium", _EURO),
    "ESP": ("ES", "Spain", _EURO),
    "ITA": ("IT", "Italy", _EURO),
    "PRT": ("PT", "Portugal", _EURO),
    "GRC": ("GR", "Greece", _EURO),
    "LUX": ("LU", "Luxembourg", _EURO),
    "EST": ("EE", "Estonia", _EURO),
    "LVA": ("LV", "Latvia", _EURO),
    "LTU": ("LT", "Lithuania", _EURO),
    "SVK": ("SK", "Slovakia", _EURO),
    "SVN": ("SI", "Slovenia", _EURO),
    "HRV": ("HR", "Croatia", _EURO),
    "CYP": ("CY", "Cyprus", _EURO),
    "MLT": ("MT", "Malta", _EURO),
    "BGR": ("BG", "Bulgaria", _EURO),
    "SWE": ("SE", "Sweden", _XEU),
    "NOR": ("NO", "Norway", _XEU),
    "DNK": ("DK", "Denmark", _XEU),
    "CHE": ("CH", "Switzerland", _XEU),
    "ISL": ("IS", "Iceland", _XEU),
    "LIE": ("LI", "Liechtenstein", _XEU),
    "POL": ("PL", "Poland", _EME),
    "CZE": ("CZ", "Czechia", _EME),
    "HUN": ("HU", "Hungary", _EME),
    "ROU": ("RO", "Romania", _EME),
    "SRB": ("RS", "Serbia", _EME),
    "TUR": ("TR", "Turkey", _EME),
    "RUS": ("RU", "Russia", _EME),
    "UKR": ("UA", "Ukraine", _EME),
    "MEX": ("MX", "Mexico", _LAT),
    "BRA": ("BR", "Brazil", _LAT),
    "ARG": ("AR", "Argentina", _LAT),
    "CHL": ("CL", "Chile", _LAT),
    "COL": ("CO", "Colombia", _LAT),
    "PER": ("PE", "Peru", _LAT),
    "URY": ("UY", "Uruguay", _LAT),
    "PAN": ("PA", "Panama", _LAT),
    "DOM": ("DO", "Dominican Republic", _LAT),
    "CRI": ("CR", "Costa Rica", _LAT),
    "ECU": ("EC", "Ecuador", _LAT),
    "HKG": ("HK", "Hong Kong", _ASD),
    "SGP": ("SG", "Singapore", _ASD),
    "KOR": ("KR", "South Korea", _ASD),
    "TWN": ("TW", "Taiwan", _ASD),
    "CHN": ("CN", "China", _ASE),
    "IND": ("IN", "India", _ASE),
    "IDN": ("ID", "Indonesia", _ASE),
    "THA": ("TH", "Thailand", _ASE),
    "MYS": ("MY", "Malaysia", _ASE),
    "PHL": ("PH", "Philippines", _ASE),
    "VNM": ("VN", "Vietnam", _ASE),
    "KAZ": ("KZ", "Kazakhstan", _ASE),
    "PAK": ("PK", "Pakistan", _ASE),
    "ISR": ("IL", "Israel", _ME),
    "SAU": ("SA", "Saudi Arabia", _ME),
    "ARE": ("AE", "United Arab Emirates", _ME),
    "QAT": ("QA", "Qatar", _ME),
    "KWT": ("KW", "Kuwait", _ME),
    "BHR": ("BH", "Bahrain", _ME),
    "OMN": ("OM", "Oman", _ME),
    "JOR": ("JO", "Jordan", _ME),
    "ZAF": ("ZA", "South Africa", _AFR),
    "EGY": ("EG", "Egypt", _AFR),
    "NGA": ("NG", "Nigeria", _AFR),
    "KEN": ("KE", "Kenya", _AFR),
    "MAR": ("MA", "Morocco", _AFR),
    "GHA": ("GH", "Ghana", _AFR),
    "AGO": ("AO", "Angola", _AFR),
}
OTHER_REGION = "Other regions"
ISO3_BY_ISO2 = {v[0]: k for k, v in COUNTRIES.items()}


def region_of(iso3: str) -> str:
    return COUNTRIES[iso3][2] if iso3 in COUNTRIES else OTHER_REGION


# ---- Morningstar ------------------------------------------------------------------------------------


def _net(entries: Any, type_: str | None = None) -> dict[str, float] | None:
    """The net (not long/short) breakdown as {code: percent}; None when the snapshot lacks the breakdown."""
    for e in entries or []:
        if e.get("SalePosition") not in (None, "N"):
            continue
        if type_ is not None and e.get("Type") != type_:
            continue
        return {str(b["Type"]): float(b["Value"]) for b in e.get("BreakdownValues") or [] if b.get("Value") is not None}
    return None


def _frac(v: float) -> float:
    return round(v / 100, 6)


def _named(raw: dict[str, float] | None, names: dict[str, str]) -> dict[str, float]:
    return {names[c]: _frac(v) for c, v in (raw or {}).items() if c in names and v > 0}


def _countries(raw: dict[str, float] | None) -> dict[str, float]:
    return {c: _frac(v) for c, v in (raw or {}).items() if v > 0}


def _number(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_morningstar(snapshot: dict[str, Any]) -> tuple[date | None, dict[str, Any]]:
    """(as-of date, profile data) from one security_details snapshot; only what the profile uses is kept."""
    pf = (snapshot.get("Portfolios") or [None])[0]
    if not isinstance(pf, dict):
        raise ValueError("snapshot has no portfolio")
    as_of = date.fromisoformat(str(pf["Date"])[:10]) if pf.get("Date") else None
    data: dict[str, Any] = {}

    raw = _net([a for a in pf.get("AssetAllocations") or [] if a.get("Type") == "MorningstarEUR3"])
    if raw is not None:
        data["asset_classes"] = {name: _frac(raw.get(code, 0.0)) for code, name in ASSET_CODES.items()}
        if raw.get("99"):
            data["not_classified"] = _frac(raw["99"])

    regional = _net(pf.get("RegionalExposure"))
    if regions := _named(regional, REGION_CODES):
        data["regions"] = regions
    if market := _named(regional, MARKET_CODES):
        data["market"] = market
    if countries := _countries(_net(pf.get("CountryExposure"), "Equity")):
        data["countries"] = countries
    if bond_countries := _countries(_net(pf.get("CountryExposure"), "Bond")):
        data["bond_countries"] = bond_countries
    if sectors := _named(_net(pf.get("GlobalStockSectorBreakdown")), SECTOR_CODES):
        data["sectors"] = sectors
    if cap := _named(_net(pf.get("MarketCapitalBreakdown")), CAP_CODES):
        data["cap_size"] = cap
    if style := _named(_net(pf.get("StyleBoxBreakdown")), STYLE_CODES):
        data["style"] = style
        data["style_box"] = max(style, key=lambda k: style[k])
    if credit := _named(_net(pf.get("CreditQualityBreakdown")), CREDIT_CODES):
        data["credit"] = credit

    stats = pf.get("BondStatistics")
    if isinstance(stats, dict):
        bond: dict[str, Any] = {}
        for key, src, is_pct in (
            ("modified_duration", "ModifiedDuration", False),
            ("effective_maturity", "EffectiveMaturity", False),
            ("ytm", "YieldToMaturity", True),
            ("current_yield", "CurrentYield", True),
            ("avg_coupon", "AverageCoupon", True),
        ):
            n = _number(stats.get(src))
            if n is not None:
                bond[key] = _frac(n) if is_pct else round(n, 4)
        if stats.get("AverageCreditQualityCode"):
            bond["credit_quality"] = str(stats["AverageCreditQualityCode"])
        if bond:
            data["bond"] = bond

    ter = _number(snapshot.get("OngoingCharge"))
    if ter is not None:
        data["ter"] = _frac(ter)
    return as_of, data


def fetch_morningstar(secid: str, client: OutboundClient) -> tuple[date | None, dict[str, Any]]:
    return parse_morningstar(morningstar.details(secid, client))


def parse_stock_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Sector, size and style of a share from its screener row (market cap is EUR; the bands are approximate)."""
    data: dict[str, Any] = {}
    if facts.get("SectorName") in SECTOR_CODES.values():
        data["sectors"] = {facts["SectorName"]: 1.0}
    cap = _number(facts.get("MarketCap"))
    if cap is not None and cap > 0:
        data["cap_size"] = {next((n for n, floor in CAP_BANDS if cap >= floor), "micro"): 1.0}
    if box := STYLE_CODES.get(str(facts.get("EquityStyleBox"))):
        data["style"] = {box: 1.0}
        data["style_box"] = box
    return data


# ---- derived ------------------------------------------------------------------------------------------


def derived(inst: Instrument) -> dict[str, Any]:
    """What the instrument's own type and ISIN prefix tell; funds need a real source, so they derive nothing."""
    if inst.type == "cash":
        return {"asset_classes": {"cash": 1.0}, "ter": 0.0}
    if inst.type == "crypto":
        return {"asset_classes": {"crypto": 1.0}, "ter": 0.0}
    if inst.type == "bond":
        return {"asset_classes": {"bond": 1.0}, "ter": 0.0}
    if inst.type != "stock":
        return {}
    data: dict[str, Any] = {"asset_classes": {"equity": 1.0}, "ter": 0.0}
    iso2 = (inst.isin or "")[:2].upper()
    if iso2.isalpha() and len(iso2) == 2:
        if iso3 := ISO3_BY_ISO2.get(iso2):
            data["countries"] = {iso3: 1.0}
            data["regions"] = {region_of(iso3): 1.0}
            data["market"] = {"developed": 1.0}
        else:
            data["regions"] = {OTHER_REGION: 1.0}
    return data


# ---- storage and resolution ---------------------------------------------------------------------------


def save(db: Session, instrument_id: int, source: str, as_of: date | None, data: dict[str, Any]) -> None:
    row = db.scalar(
        select(InstrumentProfile).where(
            InstrumentProfile.instrument_id == instrument_id, InstrumentProfile.source == source
        )
    )
    if row is None:
        db.add(
            InstrumentProfile(instrument_id=instrument_id, source=source, as_of=as_of, fetched_at=utcnow(), data=data)
        )
    else:
        row.as_of, row.fetched_at, row.data = as_of, utcnow(), data
    db.flush()


@dataclass
class Effective:
    data: dict[str, Any] = field(default_factory=dict)
    # field -> {"source": ..., "as_of": iso date or None}
    sources: dict[str, dict[str, str | None]] = field(default_factory=dict)
    rows: list[InstrumentProfile] = field(default_factory=list)

    def source_of(self, name: str) -> str | None:
        s = self.sources.get(name)
        return s["source"] if s else None


def _present(v: Any) -> bool:
    return v is not None and v != {} and v != ""


def effective_many(db: Session, insts: Iterable[Instrument]) -> dict[int, Effective]:
    """Per instrument, per top-level field: manual > morningstar > factsheet > derived."""
    insts = list(insts)
    stored: dict[int, dict[str, InstrumentProfile]] = {i.id: {} for i in insts}
    if insts:
        for r in db.scalars(select(InstrumentProfile).where(InstrumentProfile.instrument_id.in_(stored))):
            stored[r.instrument_id][r.source] = r
    out: dict[int, Effective] = {}
    for inst in insts:
        rows = stored[inst.id]
        layers: dict[str, tuple[date | None, dict[str, Any]]] = {s: (r.as_of, r.data) for s, r in rows.items()}
        # a share that was never refreshed still has its ISIN-derived country, so it does not wait for the weekly job
        layers.setdefault("derived", (None, derived(inst)))
        eff = Effective(rows=sorted(rows.values(), key=lambda r: SOURCE_ORDER.index(r.source)))
        for name in FIELDS:
            for src in SOURCE_ORDER:
                as_of, data = layers.get(src, (None, {}))
                if _present(data.get(name)):
                    eff.data[name] = data[name]
                    eff.sources[name] = {"source": src, "as_of": as_of.isoformat() if as_of else None}
                    break
        out[inst.id] = eff
    return out


def effective(db: Session, inst: Instrument) -> tuple[dict[str, Any], dict[str, dict[str, str | None]]]:
    e = effective_many(db, [inst])[inst.id]
    return e.data, e.sources


# ---- manual overrides ---------------------------------------------------------------------------------


def _weight(key: str, v: Any) -> float:
    n = _number(v)
    if n is None or not 0 <= n <= 1:
        raise ValueError(f"{key} values must be fractions between 0 and 1")
    return n


def clean_manual(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a partial override; null or empty values stay as None so the caller removes that field."""
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in FIELDS:
            raise ValueError(f"unknown profile field {key}")
        if value is None or value == {} or value == "":
            out[key] = None
        elif key in WEIGHT_FIELDS:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object of weights")
            weights = {str(k): _weight(key, v) for k, v in value.items()}
            if key == "asset_classes" and not set(weights) <= set(ASSET_KEYS):
                raise ValueError(f"asset_classes keys must be among {', '.join(ASSET_KEYS)}")
            if sum(weights.values()) > 1.0001:
                raise ValueError(f"{key} weights add up to more than 100 %")
            out[key] = weights
        elif key in ("ter", "not_classified"):
            out[key] = _weight(key, value)
        elif key == "bond":
            if not isinstance(value, dict):
                raise ValueError("bond must be an object")
            bond: dict[str, Any] = {}
            for k, v in value.items():
                if k == "credit_quality":
                    bond[k] = str(v)
                elif (n := _number(v)) is None:
                    raise ValueError(f"bond.{k} must be a number")
                else:
                    bond[k] = n
            out[key] = bond
        else:
            out[key] = str(value)
    return out


def set_manual(db: Session, inst: Instrument, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated patch into the manual row (None removes a field); returns the manual data now stored."""
    row = db.scalar(
        select(InstrumentProfile).where(
            InstrumentProfile.instrument_id == inst.id, InstrumentProfile.source == "manual"
        )
    )
    data = dict(row.data) if row else {}
    for k, v in patch.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    if not data:
        if row:
            db.delete(row)
            db.flush()
        return {}
    save(db, inst.id, "manual", None, data)
    return data


def asset_class_label(asset_classes: dict[str, Any]) -> str:
    """The coarse Instrument.asset_class a profile implies: equity or bonds at 90 %, else mixed."""
    if float(asset_classes.get("equity", 0)) >= 0.9:
        return "equity"
    if float(asset_classes.get("bond", 0)) >= 0.9:
        return "fixed_income"
    return "mixed"
