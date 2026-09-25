"""JS matcher (extension/shared/matcher.js) must agree with the Python matcher."""

from __future__ import annotations

import pytest

from amore_ocarina_sniper.matching import MatchRule, normalize_text

from .conftest import EXTENSION
from .test_matching import EXAMPLE, VECTORS

pytestmark = pytest.mark.browser

EXTRA = [
    "Zelda™ – 40th",
    "  A\tB\nC ",
    "Ｎｉｎｔｅｎｄｏ Ｓｗｉｔｃｈ ２",
    "café – naïve ® résumé",
    "$709.99 has been found - https://lbabi.nz/MXGc8R",
    "@nintendo <#123> **bold** _it_ ~~x~~",
]


@pytest.fixture
async def js(browser):
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("about:blank")
    await page.add_script_tag(path=str(EXTENSION / "shared" / "matcher.js"))
    yield page
    await context.close()


async def test_normalize_parity(js):
    texts = [t for t, _ in VECTORS] + EXTRA + [EXAMPLE]
    js_out = await js.evaluate("ts => ts.map(t => OcarinaMatcher.normalizeText(t))", texts)
    py_out = [normalize_text(t) for t in texts]
    assert js_out == py_out


async def test_matches_parity(js):
    rule = MatchRule("t")
    texts = [t for t, _ in VECTORS]
    js_out = await js.evaluate("ts => ts.map(t => OcarinaMatcher.matches([t]))", texts)
    assert js_out == [rule.matches(t) for t in texts]
    assert js_out == [expected for _, expected in VECTORS]


async def test_split_text_and_embed_parity(js):
    parts = ["@nintendo Amazon alert", "Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition has been found"]
    assert await js.evaluate("p => OcarinaMatcher.matches(p)", parts) is True
    assert MatchRule("t").matches(*parts) is True


async def test_snowflake_and_price_helpers(js):
    # 2015-01-01T00:00:00Z is the Discord epoch => id 0 (shifted) maps back to it.
    ms = await js.evaluate("OcarinaMatcher.snowflakeToMs('4194304')")  # 1 << 22 => epoch + 1ms
    assert ms == 1420070400001
    assert await js.evaluate(f"OcarinaMatcher.extractAlertPriceText({EXAMPLE!r})") == "$709.99"
