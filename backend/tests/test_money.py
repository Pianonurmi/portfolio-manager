from decimal import Decimal

import pytest

from app.platform.money import D, Money


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234,56", "1234.56"),
        ("−12,5", "-12.5"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("0,00000001", "0.00000001"),
        ("12 345 €", "12345"),
        (7, "7"),
    ],
)
def test_parse(raw: object, expected: str) -> None:
    assert D(raw) == Decimal(expected)


def test_floats_rejected() -> None:
    with pytest.raises(TypeError):
        D(0.1)


def test_money_arithmetic_and_rounding() -> None:
    a = Money(D("10.005"))
    assert (a + Money(D("0.001"))).amount == Decimal("10.006")
    assert a.rounded().amount == Decimal("10.00")  # banker's rounding
    assert Money(D("10.015")).rounded().amount == Decimal("10.02")
    with pytest.raises(ValueError):
        a + Money(D(1), "USD")


def test_convert_keeps_exactness() -> None:
    usd = Money(D("182.40"), "USD")
    eur = usd.convert(D("1") / D("1.0900"), "EUR")
    assert eur.rounded().amount == Decimal("167.34")
