"""What the hedging engine needs from the ledger: the portfolio's β to each index and the tax position."""

import math
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.core.models import INVESTMENT_KINDS, Instrument
from app.modules.hedging import products
from app.modules.hedging.engine import Market
from app.modules.hedging.models import HedgeProduct
from app.modules.portfolio import risk
from app.modules.portfolio.valuation import Scope, Valuer
from app.modules.pricing.service import PriceBook
from app.platform.db import today

MIN_MONTHS = 24
VOL_MONTHS = 60
DIVIDEND_YIELD = {"SPX": 0.013, "NDX": 0.007, "SX5E": 0.03, "DAX": 0.0, "OMXH25": 0.04, "OMXS30": 0.035}  # DAX is a
# total-return index, so its options price without dividends


def _index_instruments(db: Session) -> dict[str, Instrument]:
    return {
        str(i.meta["index"]): i
        for i in db.scalars(select(Instrument).where(Instrument.meta["index"].as_string().is_not(None)))
    }


def fits(db: Session, scope: Scope, on: date) -> list[dict[str, Any]]:
    """β, R² and the unexplained volatility of the portfolio's monthly returns against each index (in euros)."""
    dates, r, _ = risk.portfolio_monthly_returns(db, scope, on)
    insts = _index_instruments(db)
    if len(r) < MIN_MONTHS or not insts:
        return []
    book = PriceBook(db, {i.id for i in insts.values()})
    ends = risk.month_ends(dates[-1], len(dates))  # the same month ends, plus the one before the first return
    out = []
    for key, inst in insts.items():
        vals = risk.monthly_values(book, inst.id, ends)
        pairs = []
        for k in range(len(dates)):
            a, b = vals[k], vals[k + 1]
            if a is not None and b is not None and a > 0:
                pairs.append((float(r[k]), b / a - 1))
        if len(pairs) < MIN_MONTHS:
            continue
        y, x = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        cov = np.cov(x, y)
        beta = float(cov[0, 1] / cov[0, 0]) if cov[0, 0] > 0 else 0.0
        resid = y - (y.mean() - beta * x.mean()) - beta * x
        r2 = float(np.corrcoef(x, y)[0, 1] ** 2)
        recent = x[-VOL_MONTHS:]
        d = products.BY_KEY[key]
        out.append(
            {
                "key": key,
                "name": d.name,
                "currency": d.currency,
                "months": len(pairs),
                "beta": beta,
                "r2": r2,
                "resid_vol": float(resid.std(ddof=1) * math.sqrt(12)),
                "index_vol": float(recent.std(ddof=1) * math.sqrt(12)),
            }
        )
    return sorted(out, key=lambda f: -f["r2"])


def tax_position(db: Session, scope: Scope, on: date) -> dict[str, float]:
    """The investment value, the share inside an osakesäästötili and the unrealised gain share of the rest."""
    from app.modules.portfolio.api import unrealised_gain_share

    v = Valuer(db, scope, kinds=INVESTMENT_KINDS)
    total = ost = Decimal(0)
    for h in v.holdings(on):
        if h.value <= 0:
            continue
        total += h.value
        if v.accounts[h.account_id].kind == "ost":
            ost += h.value
    return {
        "value": float(total),
        "tax_free_share": float(ost / total) if total else 0.0,
        "gain_share": unrealised_gain_share(db, scope, on),
    }


def suggested_products(db: Session, key: str, kind: str, limit: int = 5) -> list[dict[str, Any]]:
    """Nordnet Markets shorts on the index: minis with 2–6× leverage, or bear certificates ×1–×3."""
    inst = _index_instruments(db).get(key)
    level = None
    if inst is not None:
        book = PriceBook(db, {inst.id})
        p = book.price(inst.id, today())
        level = float(p.close) if p else None
    q = select(HedgeProduct).where(HedgeProduct.index_key == key)
    out = []
    for hp in db.scalars(q):
        if kind == "short" and hp.kind in ("mini", "turbo") and level and hp.financing_level:
            lev = level / (hp.financing_level - level) if hp.financing_level > level else None
            if lev and 2 <= lev <= 6:
                out.append({**_row(hp), "leverage": round(lev, 1), "distance_to_stop": (hp.barrier or 0) / level - 1})
        elif kind == "bear" and hp.kind == "bear" and hp.leverage and hp.leverage <= 3:
            out.append({**_row(hp), "leverage": hp.leverage})
    key_fn = (lambda r: abs(r["leverage"] - 4)) if kind == "short" else (lambda r: r["leverage"])
    return sorted(out, key=key_fn)[:limit]


def _row(hp: HedgeProduct) -> dict[str, Any]:
    return {
        "isin": hp.isin,
        "name": hp.name,
        "kind": hp.kind,
        "index": hp.index_key,
        "financing_level": hp.financing_level,
        "barrier": hp.barrier,
        "issuer": hp.issuer,
    }


def market(
    db: Session, scope: Scope, on: date, fit: dict[str, Any] | None, means: dict[str, float], rate: float
) -> Market:
    tp = tax_position(db, scope, on)
    equity = means.get("equity", 0.07)
    if fit is None:  # no usable history: assume the shares move one to one with the index
        fit = {"key": None, "beta": 1.0, "resid_vol": 0.04, "index_vol": 0.16}
    return Market(
        value=tp["value"],
        beta=fit["beta"],
        resid_vol=fit["resid_vol"],
        index_mean=equity,
        index_vol=fit["index_vol"],
        portfolio_mean=equity * fit["beta"],
        rate=rate,
        dividend_yield=DIVIDEND_YIELD.get(fit["key"] or "", 0.015),
        gain_share=tp["gain_share"],
        tax_free_share=tp["tax_free_share"],
    )
