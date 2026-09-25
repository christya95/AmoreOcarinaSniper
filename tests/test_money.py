from decimal import Decimal

import pytest

from amore_ocarina_sniper.money import decimal_from_config, detect_currency, parse_money


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$709.99", Decimal("709.99")),
        ("CDN$ 709.99", Decimal("709.99")),
        ("CA$709.99", Decimal("709.99")),
        ("$1,299.00", Decimal("1299.00")),
        ("$709 . 99", Decimal("709.99")),
        ("$709.99 $709.99", Decimal("709.99")),  # duplicated visually-hidden price
        ("Order total: $802.29", Decimal("802.29")),
    ],
)
def test_parse_money_ok(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", ["", None, "US$709.99", "$709.99 $12.99", "free", "USD 5.00"])
def test_parse_money_fails_closed(text):
    assert parse_money(text) is None


def test_detect_currency():
    assert detect_currency("$709.99") == "CAD"
    assert detect_currency("CDN$ 709.99") == "CAD"
    assert detect_currency("US$709.99") == "USD"
    assert detect_currency("709.99") is None


def test_decimal_from_config_rejects_floats():
    assert decimal_from_config("709.99", "x") == Decimal("709.99")
    assert decimal_from_config(700, "x") == Decimal("700.00")
    with pytest.raises(ValueError):
        decimal_from_config(709.99, "x")
    with pytest.raises(ValueError):
        decimal_from_config("abc", "x")
