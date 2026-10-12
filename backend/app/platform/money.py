"""Exact money arithmetic. Floats never touch money (NFR-04)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal(0)


def D(value: object) -> Decimal:
    """Parse a number into Decimal. Accepts Finnish formatting ("1 234,56", "−12,5")."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError("floats are not accepted for money; pass a string or Decimal")
    text = str(value).strip()
    if not text:
        raise ValueError("empty number")
    text = text.replace("−", "-").replace(" ", "").replace(" ", "").replace(" ", "").replace("€", "")
    if "," in text and "." in text:
        # "1.234,56" (European) vs "1,234.56" (US): the last separator is the decimal point.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", D(self.amount))
        if len(self.currency) not in (3, 4) and self.currency not in ("USDT", "USDC"):
            raise ValueError(f"bad currency {self.currency!r}")

    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __mul__(self, k: Decimal | int) -> Money:
        return Money(self.amount * D(k), self.currency)

    def rounded(self, q: Decimal = CENT) -> Money:
        return Money(self.amount.quantize(q, rounding=ROUND_HALF_EVEN), self.currency)

    def convert(self, rate: Decimal, to: str) -> Money:
        """rate = units of `to` per one unit of self.currency."""
        return Money(self.amount * rate, to)

    @staticmethod
    def zero(currency: str = "EUR") -> Money:
        return Money(ZERO, currency)
