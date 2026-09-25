"""Money parsing. Always Decimal, never float.

Amazon.ca renders prices in several shapes ("$709.99", "CDN$ 709.99", "CA$709.99",
"$709 . 99" in split price blocks, "1,299.99"). Parsing is deliberately strict:
anything we cannot read unambiguously returns None so callers fail closed.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CURRENCY_HINTS = {
    "CAD": ("cdn$", "ca$", "c$", "cad", "$"),
    "USD": ("us$", "usd"),
}

_AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})*|\d+)\s*(?:\.\s*(\d{2}))?(?![\d.])")


def parse_money(text: str | None) -> Decimal | None:
    """Parse a single monetary amount out of ``text``.

    Returns None when zero or more than one distinct amount is present, or when the
    amount lacks a two-digit fractional part and is not obviously a whole-dollar price.
    """
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    if not cleaned:
        return None
    # Reject explicitly non-CAD currency hints before extracting digits.
    lowered = cleaned.lower()
    for hint in _CURRENCY_HINTS["USD"]:
        if hint in lowered:
            return None
    matches = _AMOUNT_RE.findall(cleaned)
    if not matches:
        return None
    amounts: set[Decimal] = set()
    for whole, cents in matches:
        digits = whole.replace(",", "")
        candidate = f"{digits}.{cents}" if cents else digits
        try:
            amounts.add(Decimal(candidate).quantize(Decimal("0.01")))
        except InvalidOperation:
            return None
    if len(amounts) != 1:
        return None
    return amounts.pop()


def detect_currency(text: str | None) -> str | None:
    """Best-effort currency detection from a rendered price string.

    Returns "CAD" for Amazon.ca-style prices, "USD" when a US hint is present, None when
    no currency marker is visible (callers treat None as unreadable).
    """
    if not text:
        return None
    lowered = text.replace("\xa0", " ").lower()
    for hint in _CURRENCY_HINTS["USD"]:
        if hint in lowered:
            return "USD"
    for hint in _CURRENCY_HINTS["CAD"]:
        if hint in lowered:
            return "CAD"
    return None


def decimal_from_config(value: object, field: str) -> Decimal:
    """Parse a config money value. Only strings are accepted (floats lose precision)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value).quantize(Decimal("0.01"))
    if isinstance(value, str):
        try:
            return Decimal(value.strip()).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError(f"{field}: '{value}' is not a valid decimal amount") from exc
    raise ValueError(f'{field}: must be a decimal string like "709.99" (got {type(value).__name__})')
