"""Market deposit rates from the ECB and the household's own bank offers.

Defaults per product (savings, 3m, 6m, 12m, 24m) come from market series; the user's own offers, stored in
`user.settings["deposit_rates"]`, override them.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.cash.models import MarketRate
from app.platform.db import today
from app.platform.outbound import OutboundClient

BASE = "https://data-api.ecb.europa.eu/service/data/"
PRODUCTS = ("savings", "3m", "6m", "12m", "24m")
HISTORY_DAYS = 3 * 366
FALLBACK_MARGIN = 0.005  # Euribor minus this when the Finnish deposit average is missing

OVERNIGHT = "MIR/M.FI.B.L21.A.R.A.2250.EUR.N"
UP_TO_1Y = "MIR/M.FI.B.L22.F.R.A.2250.EUR.N"
OVER_1Y_UP_TO_2Y = "MIR/M.FI.B.L22.G.R.A.2250.EUR.N"
EURIBOR = {t: f"FM/M.U2.EUR.RT.MM.EURIBOR{n}D_.HSTA" for t, n in ((3, "3M"), (6, "6M"), (12, "1Y"))}
AAA_2Y = "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
ESTR = "EST/B.EU000A2X2A25.WT"

SERIES: dict[str, str] = {
    OVERNIGHT: "Finnish households, overnight deposits (new business)",
    UP_TO_1Y: "Finnish households, deposits agreed maturity up to 1 year (new business)",
    OVER_1Y_UP_TO_2Y: "Finnish households, deposits agreed maturity over 1 and up to 2 years (new business)",
    EURIBOR[3]: "Euribor 3 months (monthly average)",
    EURIBOR[6]: "Euribor 6 months (monthly average)",
    EURIBOR[12]: "Euribor 12 months (monthly average)",
    AAA_2Y: "Euro area AAA government bond 2-year spot yield",
    ESTR: "Euro short-term rate (€STR)",
}


def parse(text: str) -> list[tuple[date, float]]:
    """ECB csvdata: TIME_PERIOD is a day or a month (YYYY-MM, taken as the 1st); values are percents."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        v, t = row.get("OBS_VALUE"), row.get("TIME_PERIOD")
        if not v or not t:
            continue
        d = date.fromisoformat(t if len(t) > 7 else t + "-01")
        out.append((d, float(Decimal(v) / 100)))
    return out


def fetch_series(key: str, start: date, client: OutboundClient) -> list[tuple[date, float]]:
    r = client.get(f"{BASE}{key}?startPeriod={start.isoformat()}&format=csvdata")
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return parse(r.text)


def refresh(db: Session, client: OutboundClient, warn: Any = None) -> dict[str, int]:
    """Upsert every series; one failing series must not stop the others."""
    start = today() - timedelta(days=HISTORY_DAYS)
    now = datetime.now(UTC)
    stats = {"series": 0, "rows": 0, "failed": 0}
    for key, label in SERIES.items():
        try:
            rows = fetch_series(key, start, client)
            if not rows:
                raise ValueError("no observations")
            stmt = insert(MarketRate).values(
                [{"series": key, "label": label, "date": d, "value": v, "fetched_at": now} for d, v in rows]
            )
            db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_market_rate_series_date",
                    set_={"value": stmt.excluded.value, "label": label, "fetched_at": now},
                )
            )
            stats["series"] += 1
            stats["rows"] += len(rows)
        except Exception as exc:  # noqa: BLE001 - one series must not stop the others
            stats["failed"] += 1
            if warn:
                warn(f"{label}: {type(exc).__name__}: {exc}")
    return stats


def latest(db: Session) -> dict[str, dict[str, Any]]:
    """Newest observation per series: {key: {label, date, value}}."""
    newest = select(MarketRate.series, func.max(MarketRate.date).label("d")).group_by(MarketRate.series).subquery()
    rows = db.execute(
        select(MarketRate).join(newest, (MarketRate.series == newest.c.series) & (MarketRate.date == newest.c.d))
    ).scalars()
    return {r.series: {"label": r.label, "date": r.date, "value": r.value} for r in rows}


def _pct(x: float) -> str:
    return f"{x * 100:.2f} %"


def defaults(lat: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Market-derived default per product: {rate, label, as_of}; a product is absent when nothing can be derived."""
    out: dict[str, dict[str, Any]] = {}
    overnight, estr, fin = lat.get(OVERNIGHT), lat.get(ESTR), lat.get(UP_TO_1Y)
    two, aaa = lat.get(OVER_1Y_UP_TO_2Y), lat.get(AAA_2Y)

    if overnight:
        out["savings"] = {
            "rate": overnight["value"],
            "label": "Finnish households' average overnight rate (ECB); real savings accounts pay more, enter yours",
            "as_of": overnight["date"],
        }
    elif estr:
        out["savings"] = {
            "rate": estr["value"],
            "label": "€STR (ECB); Finnish deposit average unavailable, enter your account's rate",
            "as_of": estr["date"],
        }

    eur = {t: lat[k] for t, k in EURIBOR.items() if k in lat}
    if eur:
        mean = sum(e["value"] for e in eur.values()) / len(eur)
        if fin:
            margin, how = mean - fin["value"], f"Euribor minus {_pct(mean - fin['value'])} Finnish deposit margin"
        else:
            margin, how = (
                FALLBACK_MARGIN,
                f"Euribor minus {_pct(FALLBACK_MARGIN)} (Finnish deposit average unavailable)",
            )
        for t in (3, 6, 12):
            e = eur.get(t) or min(eur.values(), key=lambda x: abs(x["value"] - mean))  # nearest available tenor
            note = "" if t in eur else f" ({t}m Euribor missing, using another tenor)"
            out[f"{t}m"] = {"rate": max(0.0, e["value"] - margin), "label": how + note, "as_of": e["date"]}
    elif fin:
        for t in (3, 6, 12):
            out[f"{t}m"] = {
                "rate": fin["value"],
                "label": "Finnish households' up to 1 year deposit average (ECB)",
                "as_of": fin["date"],
            }

    if two:
        out["24m"] = {
            "rate": two["value"],
            "label": "Finnish households' 1-2 year deposit average (ECB)",
            "as_of": two["date"],
        }
    elif aaa:
        out["24m"] = {
            "rate": max(0.0, aaa["value"] - FALLBACK_MARGIN),
            "label": f"AAA 2-year yield minus {_pct(FALLBACK_MARGIN)} (Finnish 1-2 year deposit average unavailable)",
            "as_of": aaa["date"],
        }
    return out


def own_offers(user: Any) -> dict[str, dict[str, Any]]:
    raw = (user.settings or {}).get("deposit_rates") or {}
    return {p: raw[p] for p in PRODUCTS if isinstance(raw.get(p), dict) and raw[p].get("rate") is not None}


def rates(db: Session, user: Any) -> dict[str, dict[str, Any]]:
    """Rate in effect per product: {rate, source: yours|market, label, as_of, market_rate, bank}."""
    market = defaults(latest(db))
    own = own_offers(user)
    out: dict[str, dict[str, Any]] = {}
    for p in PRODUCTS:
        m = market.get(p)
        if p in own:
            o = own[p]
            bank = o.get("bank")
            out[p] = {
                "rate": float(o["rate"]),
                "source": "yours",
                "label": f"Your offer{f' ({bank})' if bank else ''}",
                "as_of": None,
                "bank": bank,
            }
        elif m:
            out[p] = {
                "rate": m["rate"],
                "source": "market",
                "label": m["label"],
                "as_of": m["as_of"].isoformat(),
                "bank": None,
            }
        else:
            out[p] = {
                "rate": 0.0,
                "source": "market",
                "label": "No market data yet; refresh the rates or enter your own",
                "as_of": None,
                "bank": None,
            }
        out[p]["market_rate"] = m["rate"] if m else None
    return out


def cash_return(db: Session, user: Any) -> float:
    """The savings account rate in effect (the user's own, else the market default)."""
    return float(rates(db, user)["savings"]["rate"])
