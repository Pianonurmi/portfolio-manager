"""Factor regression of the portfolio on the Fama-French developed-market factors (Kenneth R. French Data Library).

Monthly factor series (percent, in USD) are downloaded as zipped CSVs, parsed defensively (the files carry prose,
a monthly block and then annual blocks) and cached in `index_series`. The portfolio's monthly total returns
(see risk.py) are converted from EUR to USD, reduced by the risk-free rate and regressed by OLS on market, size,
value, profitability, investment and momentum. The wording of the interpretation comes from fixed thresholds.
"""

import io
import re
import zipfile
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.networth.models import IndexSeries
from app.modules.portfolio.risk import risk_report
from app.modules.portfolio.valuation import Scope
from app.modules.pricing.service import PriceBook, ensure_fx
from app.platform.db import utcnow
from app.platform.outbound import OutboundClient

Arr = NDArray[np.float64]
BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FILES = {"dev5": "Developed_5_Factors_CSV.zip", "mom": "Developed_Mom_Factor_CSV.zip"}
FACTORS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "WML")
SOURCE_OF = {"Mkt-RF": "dev5", "SMB": "dev5", "HML": "dev5", "RMW": "dev5", "CMA": "dev5", "RF": "dev5", "WML": "mom"}
MIN_MONTHS = 36
TOP_N = 12
MISSING = -99.0  # the library marks missing data -99.99 (or -999)
MAX_ZIP = 2_000_000  # bytes, compressed and unpacked: the real files are a few kB
FX_GAP_DAYS = 10  # a month end further than this from an ECB rate has no usable rate
LABELS = {
    "Mkt-RF": "Market",
    "SMB": "Size",
    "HML": "Value",
    "RMW": "Profitability",
    "CMA": "Investment",
    "WML": "Momentum",
}
SIGNIFICANT = 2.0  # |t| at which a loading counts as measured rather than noise
TILT = 0.1  # smaller loadings are called neutral however precise
TILT_TEXT = {
    "SMB": ("small companies", "large companies"),
    "HML": ("value stocks (cheap on book value)", "growth stocks (expensive on book value)"),
    "RMW": ("highly profitable companies", "less profitable companies"),
    "CMA": ("conservatively investing companies", "aggressively investing companies"),
    "WML": ("recent winners (momentum)", "recent losers (contrarian)"),
}
SOURCE_NOTE = (
    "Factor data: Kenneth R. French Data Library (Developed 5 Factors and Developed Momentum Factor, monthly, in USD)."
)
CURRENCY_NOTE = (
    "The factors are in US dollars, so your euro returns are converted with the ECB month-end EUR/USD rate; "
    "currency moves therefore show up in the market loading and the fit."
)
NOTES = [
    SOURCE_NOTE,
    CURRENCY_NOTE,
    "Loadings describe the past months only and carry statistical noise: |t| below 2 means the loading could be zero.",
]


class FactorDataError(RuntimeError):
    """The factor data cannot be had (download failed or the file looked wrong); the message is shown to the user."""


# ---- parsing ---------------------------------------------------------------------------------------------------

_ROW = re.compile(r"^\s*(\d{6})\s*,")


def parse_monthly(text: str) -> dict[str, dict[str, float]]:
    """{column: {"YYYY-MM": percent}} from the first monthly block of a French-library CSV.

    The block starts at the first line that begins with a comma (the column names) and runs over consecutive
    `YYYYMM,` rows; the prose above it and the annual blocks below it are ignored, and missing values (-99.99,
    -999) are skipped. Raises FactorDataError when no block is found.
    """
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(",") and "," in ln.strip()[1:]), None)
    if head is None:
        head = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(",") and len(ln.strip()) > 1), None)
    if head is None:
        raise FactorDataError("The factor file has no column header")
    names = [c.strip() for c in lines[head].split(",")[1:]]
    out: dict[str, dict[str, float]] = {n: {} for n in names if n}
    for ln in lines[head + 1 :]:
        m = _ROW.match(ln)
        if m is None:
            if out and any(out[n] for n in out):
                break  # the first block ended (blank line or the annual header)
            continue
        ym = m.group(1)
        month = int(ym[4:])
        if not 1 <= month <= 12:
            continue
        for n, cell in zip(names, ln.split(",")[1:], strict=False):
            try:
                v = float(cell)
            except ValueError:
                continue
            if n in out and v > MISSING:
                out[n][f"{ym[:4]}-{ym[4:]}"] = v
    if not any(out.values()):
        raise FactorDataError("The factor file has no monthly rows")
    return {n: pts for n, pts in out.items() if pts}


def unzip_csv(blob: bytes) -> str:
    """The text of the single CSV inside a downloaded zip, with size limits (the download is untrusted)."""
    if len(blob) > MAX_ZIP:
        raise FactorDataError("The factor download is unexpectedly large")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            members = [i for i in z.infolist() if i.filename.lower().endswith(".csv") and not i.is_dir()]
            if len(members) != 1 or members[0].file_size > MAX_ZIP:
                raise FactorDataError("The factor download is not the expected single CSV file")
            return z.read(members[0]).decode("latin-1")
    except zipfile.BadZipFile as exc:
        raise FactorDataError("The factor download is not a zip file") from exc


def key_of(group: str, name: str) -> str:
    return f"ff:{group}:{name}"


def refresh_factors(db: Session, client: OutboundClient) -> dict[str, int]:
    """Download both files, replace the cached series; {column: months}. Nothing is stored unless all parse."""
    parsed: dict[str, dict[str, dict[str, float]]] = {}
    for group, fname in FILES.items():
        r = client.get(BASE + fname)
        if r.status_code != 200:
            raise FactorDataError(f"{fname}: HTTP {r.status_code}")
        parsed[group] = parse_monthly(unzip_csv(r.content))
    counts: dict[str, int] = {}
    for name, group in SOURCE_OF.items():
        pts = parsed[group].get(name)
        if not pts:
            raise FactorDataError(f"{FILES[group]} has no {name} column")
        key = key_of(group, name)
        s = db.get(IndexSeries, key) or IndexSeries(key=key)
        s.label, s.unit, s.points, s.trades, s.fetched_at = (
            f"Fama-French developed {name}",
            "pct_month",
            pts,
            {},
            utcnow(),
        )
        db.add(s)
        counts[name] = len(pts)
    db.flush()
    return counts


def load_factors(db: Session) -> tuple[dict[str, dict[str, float]], Any] | None:
    """(the cached series by column incl. RF, when they were fetched), or None while any series is missing."""
    rows = {s.key: s for s in db.scalars(select(IndexSeries).where(IndexSeries.key.like("ff:%")))}
    out: dict[str, dict[str, float]] = {}
    for name, group in SOURCE_OF.items():
        s = rows.get(key_of(group, name))
        if s is None or not s.points:
            return None
        out[name] = {p: float(v) for p, v in s.points.items()}
    return out, min(rows[key_of(g, n)].fetched_at for n, g in SOURCE_OF.items())


def ensure_data(db: Session, client: OutboundClient | None = None) -> tuple[dict[str, dict[str, float]], Any]:
    """The cached factors, downloaded on demand when missing; FactorDataError says why when that fails."""
    got = load_factors(db)
    if got is not None:
        return got
    try:
        with client or OutboundClient() as c:
            refresh_factors(db, c)
    except FactorDataError:
        raise
    except Exception as exc:  # noqa: BLE001 - network, allowlist and parsing problems all read the same to the user
        raise FactorDataError(f"The factor data could not be downloaded: {exc}") from exc
    got = load_factors(db)
    assert got is not None
    return got


# ---- regression ------------------------------------------------------------------------------------------------


def ols(y: Arr, x: Arr) -> dict[str, Any]:
    """OLS of y on x with an intercept: betas (intercept first), t-statistics, R², residual standard error."""
    t, k = x.shape[0], x.shape[1] + 1
    a = np.column_stack([np.ones(t), x])
    beta, *_ = np.linalg.lstsq(a, y, rcond=None)
    resid = y - a @ beta
    rss = float(resid @ resid)
    tss = float(((y - y.mean()) ** 2).sum())
    dof = t - k
    s2 = rss / dof if dof > 0 else float("nan")
    cov = s2 * np.linalg.pinv(a.T @ a)
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        tstat = np.where(se > 1e-15, beta / se, np.nan)
    return {"beta": beta, "t": tstat, "r2": 1 - rss / tss if tss > 1e-18 else float("nan"), "dof": dof}


def eur_to_usd(r_eur: Arr, usd_per_eur: list[float | None]) -> Arr:
    """Monthly USD returns from EUR returns and USD per EUR at the T + 1 month ends; NaN where a rate is missing."""
    fx = np.array([np.nan if v is None else v for v in usd_per_eur], dtype=np.float64)
    return np.asarray((1 + r_eur) * fx[1:] / fx[:-1] - 1, dtype=np.float64)


def _f(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def _fit(
    r_usd: Arr, months: list[str], factors: dict[str, dict[str, float]]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Regress excess USD returns on the factors over the months all of them cover; (fit or None, months used)."""
    common = [
        m for i, m in enumerate(months) if np.isfinite(r_usd[i]) and all(m in factors[n] for n in (*FACTORS, "RF"))
    ]
    if len(common) < MIN_MONTHS:
        return None, common
    idx = [months.index(m) for m in common]
    y = r_usd[idx] - np.array([factors["RF"][m] for m in common]) / 100
    x = np.column_stack([np.array([factors[n][m] for m in common]) / 100 for n in FACTORS])
    res = ols(y, x)
    beta, tst = res["beta"], res["t"]
    return {
        "alpha": _f(beta[0] * 12),
        "alpha_t": _f(tst[0]),
        "r2": _f(res["r2"]),
        "months": len(common),
        "loadings": {n: _f(beta[i + 1]) for i, n in enumerate(FACTORS)},
        "t": {n: _f(tst[i + 1]) for i, n in enumerate(FACTORS)},
    }, common


def interpret(fit: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain-language lines per factor, alpha and fit, chosen by thresholds (never free text)."""
    out: list[dict[str, Any]] = []
    b, t = fit["loadings"]["Mkt-RF"], fit["t"]["Mkt-RF"]
    if b is not None:
        how = (
            "barely moves with developed markets"
            if b < 0.3
            else "moves less than developed markets"
            if b < 0.8
            else "moves with developed markets"
            if b <= 1.2
            else "amplifies developed-market moves"
        )
        out.append({"factor": "Mkt-RF", "text": f"Market beta {b:.2f}: {how}", "loading": b, "t": t})
    for n in FACTORS[1:]:
        b, t = fit["loadings"][n], fit["t"][n]
        if b is None:
            continue
        pos, neg = TILT_TEXT[n]
        if abs(b) < TILT or t is None or abs(t) < SIGNIFICANT:
            text = f"{LABELS[n]} {b:+.2f}: no clear tilt"
        else:
            text = f"{LABELS[n]} {b:+.2f}: tilted to {pos if b > 0 else neg}"
        out.append({"factor": n, "text": text.replace("-", "−"), "loading": b, "t": t})
    a, at = fit["alpha"], fit["alpha_t"]
    if a is not None:
        sig = at is not None and abs(at) >= SIGNIFICANT
        text = (
            f"Alpha {a:+.1%} a year after what the factors explain"
            + (" (statistically clear)" if sig else ": not distinguishable from zero")
        ).replace("-", "−")
        out.append({"factor": "alpha", "text": text, "loading": a, "t": at})
    r2 = fit["r2"]
    if r2 is not None:
        weak = "; much of the portfolio is not equity-factor risk (bonds, cash, other)" if r2 < 0.6 else ""
        out.append({"factor": "r2", "text": f"The six factors explain {r2:.0%} of monthly moves{weak}", "loading": r2})
    return out


def factor_report(
    db: Session, scope: Scope, period: str, on: date, client: OutboundClient | None = None
) -> dict[str, Any]:
    """Factor loadings of today's portfolio and of its largest holdings; `status` is "insufficient" with a reason."""
    cap: dict[str, Any] = {}
    risk_report(db, scope, period, 0.0, on, proxies=True, capture=cap)
    out: dict[str, Any] = {
        "scope": scope.token,
        "period": period.upper(),
        "as_of": on,
        "status": "insufficient",
        "explanation": None,
        "data": None,
        "window": None,
        "portfolio": None,
        "interpretation": [],
        "holdings": [],
        "notes": list(NOTES),
    }
    if "parts" not in cap:
        out["explanation"] = "Not enough price history to measure the portfolio's monthly returns."
        return out
    factors, fetched = ensure_data(db, client)
    dates: list[date] = cap["dates"]  # T + 1 month ends around the T returns
    ensure_fx(db, {"USD"}, dates[0], client)
    book = PriceBook(db)
    usd: list[float | None] = []
    for d in dates:
        try:
            rate, used = book.eur_per("USD", d)
            usd.append(float(1 / rate) if used is not None and abs((d - used).days) <= FX_GAP_DAYS else None)
        except LookupError:
            usd.append(None)
    last = max(factors["Mkt-RF"])
    out["data"] = {"source": "Kenneth R. French Data Library", "last_month": last, "fetched_at": fetched}
    if all(v is None for v in usd):
        out["explanation"] = "No EUR/USD exchange rates are stored, so returns cannot be compared with the USD factors."
        return out
    months = [f"{d:%Y-%m}" for d in dates[1:]]
    fit, used_months = _fit(eur_to_usd(cap["r"], usd), months, factors)
    if fit is None:
        out["explanation"] = (
            f"Only {len(used_months)} months overlap between your portfolio's history and the factor data "
            f"(which ends {last}); at least {MIN_MONTHS} are needed for a meaningful regression."
        )
        return out
    out.update(status="ok", portfolio=fit, interpretation=interpret(fit))
    out["window"] = {"start": used_months[0], "end": used_months[-1], "months": len(used_months)}
    p = cap["parts"]
    cols: Arr = p["cols"]
    live = [k for k, i in enumerate(p["ids"]) if p["insts"][p["ids"][k]].type != "cash"][:TOP_N]
    covered = float(p["covered"])
    for k in live:
        i = p["ids"][k]
        hfit, _ = _fit(eur_to_usd(cols[:, k], usd), months, factors)
        out["holdings"].append(
            {
                "instrument_id": i,
                "name": p["insts"][i].name,
                "weight": float(p["values"][i]) / covered,
                **(hfit or {"loadings": None, "t": None, "alpha": None, "alpha_t": None, "r2": None, "months": 0}),
            }
        )
    return out
