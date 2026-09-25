"""Alert-text matching, mirrored by ``extension/shared/matcher.js``.

The extension decides *whether to send a trigger*; Python re-checks the declared
``matched_target`` against its own configuration. Both sides must normalize the same
way, so keep this file and the JS implementation in lock-step (tests in
``tests/test_matching.py`` and ``tests/test_extension_matcher.py`` pin identical vectors).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

DEFAULT_REQUIRED_PHRASES: tuple[str, ...] = (
    "nintendo switch 2",
    "legend of zelda",
    "40th anniversary edition",
    "has been found",
)

# Symbols that carry no meaning for matching: trademark/copyright marks, and the
# various dash/quote characters Discord and Amazon like to substitute.
_STRIP_SYMBOLS = dict.fromkeys(map(ord, "\u2122\u00ae\u00a9\u2120"), " ")
_PUNCT_RE = re.compile(r"[^\w\s$.]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """Normalize for matching: NFKC, casefold, strip marks/punctuation, collapse spaces."""
    if not text:
        return ""
    # Strip marks *before* NFKC, which would otherwise expand "™" into "TM".
    value = text.translate(_STRIP_SYMBOLS)
    value = unicodedata.normalize("NFKC", value)
    value = value.casefold()
    value = _PUNCT_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    return value


@dataclass(frozen=True)
class MatchRule:
    target_id: str
    required_phrases: tuple[str, ...] = DEFAULT_REQUIRED_PHRASES
    excluded_phrases: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, *texts: str | None) -> bool:
        """True when every required phrase appears in the combined normalized text."""
        haystack = " ".join(normalize_text(t) for t in texts if t)
        if not haystack:
            return False
        for phrase in self.excluded_phrases:
            if normalize_text(phrase) in haystack:
                return False
        return all(normalize_text(p) in haystack for p in self.required_phrases)


PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")


def extract_alert_price_text(text: str) -> str | None:
    """Return the first "$709.99"-style token in alert text, for telemetry only.

    Alert prices are reference information; they never define or authorize a purchase.
    """
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    whole, cents = m.group(1), m.group(2)
    return f"${whole}.{cents}" if cents else f"${whole}"
