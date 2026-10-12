"""European option prices (Black–Scholes with a dividend yield) and the zero-cost collar.

Prices are per unit of the underlying's level today (S = 1), so a strike of 0.9 is a put 10 % out of the money.
No option chains are fetched: the implied volatility is an input (by default the index's realised volatility plus a
skew premium, since out-of-the-money puts trade above realised volatility).
"""

import math


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _d(s: float, k: float, t: float, r: float, q: float, vol: float) -> tuple[float, float]:
    d1 = (math.log(s / k) + (r - q + vol * vol / 2) * t) / (vol * math.sqrt(t))
    return d1, d1 - vol * math.sqrt(t)


def put(k: float, t: float, r: float, q: float, vol: float, s: float = 1.0) -> float:
    if t <= 0 or vol <= 0:
        return max(k * math.exp(-r * max(t, 0)) - s * math.exp(-q * max(t, 0)), 0.0)
    d1, d2 = _d(s, k, t, r, q, vol)
    return k * math.exp(-r * t) * _cdf(-d2) - s * math.exp(-q * t) * _cdf(-d1)


def call(k: float, t: float, r: float, q: float, vol: float, s: float = 1.0) -> float:
    if t <= 0 or vol <= 0:
        return max(s * math.exp(-q * max(t, 0)) - k * math.exp(-r * max(t, 0)), 0.0)
    d1, d2 = _d(s, k, t, r, q, vol)
    return s * math.exp(-q * t) * _cdf(d1) - k * math.exp(-r * t) * _cdf(d2)


def collar_cap(put_strike: float, t: float, r: float, q: float, put_vol: float, call_vol: float) -> float:
    """The call strike whose premium pays for the put (a zero-cost collar); capped at 3× the level."""
    target = put(put_strike, t, r, q, put_vol)
    lo, hi = 1.0, 3.0
    if call(lo, t, r, q, call_vol) < target:
        return lo  # even an at-the-money call does not pay for this put
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if call(mid, t, r, q, call_vol) > target else (lo, mid)
    return (lo + hi) / 2
