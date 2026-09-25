from amore_ocarina_sniper.matching import (
    DEFAULT_REQUIRED_PHRASES,
    MatchRule,
    extract_alert_price_text,
    normalize_text,
)

EXAMPLE = (
    "@nintendo Amazon Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition"
    " ($709.99) has been found - https://lbabi.nz/MXGc8R"
)

# Shared with tests/test_extension_matcher.py to pin JS/Python parity.
VECTORS = [
    (EXAMPLE, True),
    ("Nintendo Switch™ 2 – The Legend of Zelda™ – 40th Anniversary Edition has been found", True),
    ("NINTENDO SWITCH 2 — the legend of zelda — 40TH ANNIVERSARY EDITION has been found!", True),
    ("Nintendo  Switch\u00a02 - Legend of Zelda - 40th Anniversary Edition has been found", True),
    ("Ｎｉｎｔｅｎｄｏ Ｓｗｉｔｃｈ ２ – The Legend of Zelda – 40th Anniversary Edition has been found", True),
    ("Nintendo Switch 2 – Mario Kart World Bundle ($629.99) has been found", False),
    ("Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition", False),  # no 'has been found'
    ("The Legend of Zelda 40th Anniversary Edition amiibo has been found", False),  # no console
    ("", False),
]


def test_example_alert_matches():
    rule = MatchRule("t")
    assert rule.matches(EXAMPLE)


def test_vectors():
    rule = MatchRule("t")
    for text, expected in VECTORS:
        assert rule.matches(text) is expected, text


def test_split_across_content_and_embed():
    rule = MatchRule("t")
    assert rule.matches("@nintendo Amazon alert", "Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition has been found")
    assert not rule.matches("@nintendo Amazon alert", None)


def test_excluded_phrase():
    rule = MatchRule("t", excluded_phrases=("pre-order",))
    assert not rule.matches(EXAMPLE + " pre-order")


def test_normalize_strips_marks_and_dashes():
    assert normalize_text("Zelda™ – 40th") == "zelda 40th"
    assert normalize_text("  A\tB\nC ") == "a b c"


def test_price_extraction_is_reference_only():
    assert extract_alert_price_text(EXAMPLE) == "$709.99"
    assert extract_alert_price_text("no price here") is None
    assert extract_alert_price_text("$1,299") == "$1,299"


def test_default_phrases_stable():
    assert DEFAULT_REQUIRED_PHRASES == (
        "nintendo switch 2",
        "legend of zelda",
        "40th anniversary edition",
        "has been found",
    )
