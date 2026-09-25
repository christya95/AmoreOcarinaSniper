"""Content-script behaviour against the local Discord-like fixture DOM.

chrome.runtime is stubbed; the three content scripts are injected as-is. Every
candidate the script would send to the service worker is captured in window.__sent.
"""

from __future__ import annotations

import pytest

from .conftest import CHANNEL_ID, EXTENSION, GUILD_ID, TARGET_ID

pytestmark = pytest.mark.browser

ALERT = (
    "@nintendo Amazon Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition"
    " ($709.99) has been found - https://lbabi.nz/MXGc8R"
)
UNRELATED = "@nintendo Amazon Nintendo Switch 2 – Mario Kart World Bundle ($629.99) has been found"

CHROME_STUB = """
window.__sent = [];
window.__listeners = [];
window.__cfg = %s;
window.chrome = {
  runtime: {
    id: "test-extension",
    sendMessage: async (msg) => {
      window.__sent.push(msg);
      if (msg.type === "get-config") return { config: window.__cfg };
      return { ok: true };
    },
    onMessage: { addListener: (fn) => window.__listeners.push(fn) },
  },
};
"""


def default_cfg(**over):
    cfg = {
        "guildId": GUILD_ID,
        "channelId": CHANNEL_ID,
        "freshnessMs": 90_000,
        "requiredPhrases": None,
        "targetId": TARGET_ID,
        "senderName": "",
        "senderUserId": "",
        "paused": False,
    }
    cfg.update(over)
    return cfg


class Harness:
    def __init__(self, page):
        self.page = page

    async def inject_scripts(self):
        for rel in ("shared/matcher.js", "content/selectors.js", "content/observer.js"):
            await self.page.add_script_tag(path=str(EXTENSION / rel))
        await self.tick()

    async def tick(self, ms: int = 20):
        await self.page.evaluate(f"new Promise(r => setTimeout(r, {ms}))")

    async def add(self, *, offset_ms: int = 0, content: str = ALERT, channel: str = CHANNEL_ID,
                  author="Alert Bot", user_id=None, embed=None, ts=True, seq=1, message_id=None,
                  bot=False) -> str:
        js = """
        ({offset, content, channel, author, userId, embed, ts, seq, messageId, bot}) => {
          const now = Date.now() + offset;
          const id = messageId || __fx.snowflakeFor(now, seq);
          __fx.addMessage({ channelId: channel, messageId: id, tsMs: ts ? now : null,
                            content, author, userId, embed, bot });
          return id;
        }"""
        mid = await self.page.evaluate(js, {
            "offset": offset_ms, "content": content, "channel": channel, "author": author,
            "userId": user_id, "embed": embed, "ts": ts, "seq": seq, "messageId": message_id,
            "bot": bot,
        })
        await self.tick()
        return mid

    async def candidates(self):
        return await self.page.evaluate(
            "window.__sent.filter(m => m.type === 'candidate').map(m => m.candidate)"
        )

    async def statuses(self):
        return await self.page.evaluate("window.__sent.filter(m => m.type === 'tab-status')")


@pytest.fixture
async def harness(browser, fixture_server):
    context = await browser.new_context()
    page = await context.new_page()
    await page.add_init_script(CHROME_STUB % __import__("json").dumps(default_cfg()))
    await page.goto(f"{fixture_server}/channels/{GUILD_ID}/{CHANNEL_ID}")
    h = Harness(page)
    yield h
    await context.close()


async def test_baseline_history_never_triggers(harness):
    # A matching message already rendered before the observer attaches.
    await harness.add(offset_ms=-30_000, seq=1)
    await harness.inject_scripts()
    assert await harness.candidates() == []
    st = (await harness.statuses())[-1]
    assert st["onChannel"] and st["attached"] and st["monitoring"]


async def test_new_matching_message_triggers_once(harness):
    await harness.add(offset_ms=-600_000, content="older history", seq=1)
    await harness.inject_scripts()
    mid = await harness.add(seq=2)
    cands = await harness.candidates()
    assert len(cands) == 1
    c = cands[0]
    assert c["message_id"] == mid and c["channel_id"] == CHANNEL_ID and c["guild_id"] == GUILD_ID
    assert c["event_id"] == f"{CHANNEL_ID}-{mid}"
    assert c["matched_target"] == TARGET_ID
    assert abs(c["detected_wall_ms"] - c["message_ts_ms"]) < 5000
    assert c["alert_price_text"] == "$709.99"
    # Duplicate mutation on the same message (content re-set, embed added) -> still one.
    await harness.page.evaluate(f"__fx.setContent('{mid}', {ALERT!r})")
    await harness.page.evaluate(f"__fx.addEmbed('{mid}', {{title: 'Amazon', description: 'still found'}})")
    await harness.tick()
    assert len(await harness.candidates()) == 1


async def test_unrelated_product_ignored(harness):
    await harness.inject_scripts()
    await harness.add(content=UNRELATED)
    await harness.add(content="Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition restock soon?")
    assert await harness.candidates() == []


async def test_unicode_variants_match(harness):
    await harness.inject_scripts()
    await harness.add(content="Ｎｉｎｔｅｎｄｏ Ｓｗｉｔｃｈ™ ２ — The Legend of Zelda® — 40TH ANNIVERSARY EDITION has been found!")
    assert len(await harness.candidates()) == 1


async def test_delayed_embed_completes_match(harness):
    await harness.inject_scripts()
    mid = await harness.add(content="@nintendo Amazon alert")
    assert await harness.candidates() == []
    await harness.page.evaluate(
        f"__fx.addEmbed('{mid}', {{title: 'Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition',"
        f" description: '($709.99) has been found'}})"
    )
    await harness.tick()
    cands = await harness.candidates()
    assert len(cands) == 1 and cands[0]["message_id"] == mid


async def test_stale_message_and_missing_timestamp_fail_closed(harness):
    await harness.inject_scripts()
    await harness.add(offset_ms=-120_000, seq=3)  # older than 90s freshness
    await harness.add(ts=False, seq=4)  # no rendered timestamp
    assert await harness.candidates() == []
    skips = [s.get("lastSkip") for s in await harness.statuses() if s.get("lastSkip")]
    assert any("stale" in s for s in skips) and any("timestamp" in s for s in skips)


async def test_virtualized_reuse_does_not_retrigger(harness):
    hist = await harness.add(offset_ms=-300_000, seq=1)
    await harness.inject_scripts()
    mid = await harness.add(seq=2)
    assert len(await harness.candidates()) == 1
    # Discord unmounts off-screen rows and re-mounts them with fresh elements.
    for m, off in ((hist, -300_000), (mid, 0)):
        await harness.page.evaluate(f"__fx.removeMessage('{CHANNEL_ID}', '{m}')")
        await harness.page.evaluate(
            "([c, m, off, text]) => __fx.reappend(c, m, { tsMs: Date.now() + off, content: text, author: 'Alert Bot' })",
            [CHANNEL_ID, m, off, ALERT],
        )
    await harness.tick()
    assert len(await harness.candidates()) == 1
    # Scrolling back renders a message older than the startup high-water mark: history.
    await harness.add(offset_ms=-200_000, seq=1)
    assert len(await harness.candidates()) == 1


async def test_list_replacement_reattaches(harness):
    await harness.inject_scripts()
    await harness.page.evaluate("__fx.replaceList()")
    await harness.tick(50)
    mid = await harness.add(seq=5)
    cands = await harness.candidates()
    assert [c["message_id"] for c in cands] == [mid]


async def test_navigation_to_other_channel_pauses_and_returns(harness):
    await harness.inject_scripts()
    other = "333333333333333333"
    await harness.page.evaluate(f"__fx.navigate('/channels/{GUILD_ID}/{other}')")
    await harness.tick(700)  # route poll interval is 500ms
    st = (await harness.statuses())[-1]
    assert not st["onChannel"] and not st["monitoring"]
    await harness.add(channel=other, seq=6)
    assert await harness.candidates() == []
    await harness.page.evaluate(f"__fx.navigate('/channels/{GUILD_ID}/{CHANNEL_ID}')")
    await harness.tick(700)
    st = (await harness.statuses())[-1]
    assert st["onChannel"] and st["monitoring"]
    # Messages rendered while we were away are re-baselined on re-attach; new ones trigger.
    mid = await harness.add(seq=7)
    assert [c["message_id"] for c in await harness.candidates()] == [mid]


async def test_dom_channel_mismatch_ignored(harness):
    await harness.inject_scripts()
    await harness.add(channel="444444444444444444", seq=8)
    assert await harness.candidates() == []


async def test_sender_user_id_gate(harness):
    await harness.inject_scripts()
    await harness.page.evaluate(
        "cfg => window.__listeners.forEach(fn => fn({type: 'config-updated', config: cfg}))",
        default_cfg(senderUserId="987654321098765432"),
    )
    await harness.tick()
    await harness.add(seq=9)  # no avatar => user id not visible => fail closed
    await harness.add(seq=10, user_id="111111111111111119")  # different user
    assert await harness.candidates() == []
    mid = await harness.add(seq=11, user_id="987654321098765432")
    assert [c["message_id"] for c in await harness.candidates()] == [mid]


async def test_sender_user_id_gate_fails_closed_for_default_avatar_app(harness):
    # Live-observed 2026-09-24: the alert app uses Discord's default avatar (/assets/<hash>.png),
    # so no user id exists anywhere in the message DOM even though an avatar IS shown.
    await harness.inject_scripts()
    await harness.page.evaluate(
        "cfg => window.__listeners.forEach(fn => fn({type: 'config-updated', config: cfg}))",
        default_cfg(senderUserId="755174187349442640", senderName="Lbabinz"),
    )
    await harness.tick()
    await harness.add(seq=14, author="Lbabinz", bot=True)
    assert await harness.candidates() == []
    st = (await harness.statuses())[-1]
    assert "sender user id not visible" in st["lastSkip"]
    assert "Sender user id" in st["lastSkip"]  # actionable: tells the operator what to change
    # The skip reason is sticky across the periodic status refresh.
    await harness.tick(700)
    assert "sender user id not visible" in (await harness.statuses())[-1]["lastSkip"]


async def test_sender_name_matches_despite_app_badge(harness):
    # Live Discord renders <span id=message-username-…><span data-text="Lbabinz">Lbabinz</span>
    # <span class=botTag…>APP</span></span>; header.textContent is "LbabinzAPP".
    await harness.inject_scripts()
    await harness.page.evaluate(
        "cfg => window.__listeners.forEach(fn => fn({type: 'config-updated', config: cfg}))",
        default_cfg(senderName="Lbabinz"),
    )
    await harness.tick()
    header_text = await harness.page.evaluate(
        "ch => { __fx.addMessage({channelId: ch, messageId: __fx.snowflakeFor(Date.now(), 15), tsMs: Date.now(),"
        " content: 'probe', author: 'Lbabinz', bot: true}); return document.querySelector('[id^=\"message-username-\"]').textContent; }",
        CHANNEL_ID,
    )
    assert header_text == "LbabinzAPP"
    await harness.add(seq=16, author="Someone Else", bot=True)  # wrong author
    await harness.add(seq=17, author="LbabinzAPP", bot=False)  # impersonation via name text, no badge
    assert await harness.candidates() == []
    mid = await harness.add(seq=18, author="Lbabinz", bot=True)
    assert [c["message_id"] for c in await harness.candidates()] == [mid]


async def test_paused_config_blocks_detection(harness):
    await harness.inject_scripts()
    await harness.page.evaluate(
        "cfg => window.__listeners.forEach(fn => fn({type: 'config-updated', config: cfg}))",
        default_cfg(paused=True),
    )
    await harness.tick()
    await harness.add(seq=12)
    assert await harness.candidates() == []
    st = (await harness.statuses())[-1]
    assert st["paused"] and not st["monitoring"]


async def test_extension_reload_shuts_down_quietly(harness):
    await harness.inject_scripts()
    # Chrome throws synchronously from chrome.runtime.* once the extension is reloaded.
    await harness.page.evaluate(
        "window.chrome.runtime.sendMessage = () => { throw new Error('Extension context invalidated.'); }; true"
    )
    await harness.add(seq=13)
    await harness.tick(600)
    assert await harness.page.evaluate("__ocarinaTest.state.dead") is True
