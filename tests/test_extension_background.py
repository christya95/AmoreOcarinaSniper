"""Service-worker logic (background.js) in a harness page with stubbed chrome.* and fetch.

Covers durable dedupe, bounded retries with the same event id, terminal 4xx handling,
worker restart (fresh script, same storage), and candidate gating.
"""

from __future__ import annotations

import json

import pytest

from .conftest import CHANNEL_ID, EXTENSION, GUILD_ID, TARGET_ID

pytestmark = pytest.mark.browser

SECRET = "s" * 43

STUB = """
window.__storage = {};
window.__fetchLog = [];
window.__fetchScript = [];
window.__timers = [];
window.chrome = {
  storage: { local: {
    get: async (keys) => {
      const list = typeof keys === 'string' ? [keys] : keys;
      const out = {};
      for (const k of list) if (k in window.__storage) out[k] = JSON.parse(JSON.stringify(window.__storage[k]));
      return out;
    },
    set: async (obj) => { Object.assign(window.__storage, JSON.parse(JSON.stringify(obj))); },
  } },
  alarms: { create() {}, onAlarm: { addListener() {} } },
  runtime: {
    onMessage: { addListener(fn) { window.__onMessage = fn; } },
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
  },
  tabs: { query: async () => [], sendMessage: async () => {}, onRemoved: { addListener() {} } },
};
window.importScripts = () => {};
window.fetch = async (url, init) => {
  window.__fetchLog.push({ url, method: init && init.method, headers: init && init.headers, body: init && init.body });
  const next = window.__fetchScript.shift() || { status: 200, body: { ack: true, disposition: 'accepted' } };
  if (next.throw) throw new TypeError(next.throw);
  return { ok: next.status < 300, status: next.status, json: async () => next.body };
};
"""

CONFIG = {
    "guildId": GUILD_ID,
    "channelId": CHANNEL_ID,
    "bridgePort": 48620,
    "pairingSecret": SECRET,
    "targetId": TARGET_ID,
    "freshnessSec": 90,
    "senderName": "",
    "senderUserId": "",
    "paused": False,
}

SENDER = {"url": f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}", "tab": {"id": 1}}


def candidate(message_id="1234567890123456789", **over):
    c = {
        "event_id": f"{CHANNEL_ID}-{message_id}",
        "message_id": message_id,
        "channel_id": CHANNEL_ID,
        "guild_id": GUILD_ID,
        "matched_target": TARGET_ID,
        "message_ts_ms": 0,  # set in JS to Date.now()-1000
        "detected_wall_ms": 0,
        "alert_price_text": "$709.99",
    }
    c.update(over)
    return c


class SW:
    def __init__(self, page):
        self.page = page

    async def load(self):
        await self.page.add_script_tag(path=str(EXTENSION / "shared" / "matcher.js"))
        await self.page.add_script_tag(path=str(EXTENSION / "background.js"))

    async def handle(self, cand, sender=SENDER):
        return await self.page.evaluate(
            """async ([c, s]) => {
                 const now = Date.now();
                 if (!c.message_ts_ms) c.message_ts_ms = now - 1000;
                 if (!c.detected_wall_ms) c.detected_wall_ms = now - 20;
                 return await __ocarinaBackground.handleCandidate(c, s);
               }""",
            [cand, sender],
        )

    async def fetch_log(self):
        return await self.page.evaluate("window.__fetchLog")

    async def script(self, responses):
        await self.page.evaluate("r => { window.__fetchScript = r; }", responses)

    async def sent_events(self):
        return await self.page.evaluate("__ocarinaBackground.getSentEvents()")

    async def wait(self, ms):
        await self.page.evaluate(f"new Promise(r => setTimeout(r, {ms}))")


@pytest.fixture
async def sw(browser, fixture_server):
    context = await browser.new_context()
    page = await context.new_page()
    await page.add_init_script(STUB + f"window.__storage.config = {json.dumps(CONFIG)};")
    await page.goto(f"{fixture_server}/discord/channel.html")
    s = SW(page)
    await s.load()
    yield s
    await context.close()


async def test_candidate_delivered_with_auth_and_schema(sw):
    res = await sw.handle(candidate())
    assert res["accepted"] and res["status"] == "acked"
    log = await sw.fetch_log()
    assert len(log) == 1
    assert log[0]["url"] == "http://127.0.0.1:48620/v1/trigger" and log[0]["method"] == "POST"
    assert log[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = json.loads(log[0]["body"])
    assert body["schema"] == 1 and body["attempt"] == 1 and body["source"] == "extension"
    assert body["event_id"] == f"{CHANNEL_ID}-1234567890123456789"
    assert body["matched_target"] == TARGET_ID
    assert 0 <= body["detected_at_offset_ms"] < 5000
    assert set(body) == {
        "schema", "event_id", "message_id", "channel_id", "guild_id", "matched_target",
        "message_ts_ms", "sent_at_ms", "detected_at_offset_ms", "attempt", "source",
    }  # no chat text, no price, no secret in the body


async def test_duplicate_candidate_not_resent(sw):
    await sw.handle(candidate())
    res = await sw.handle(candidate())
    assert not res["accepted"] and "duplicate" in res["reason"]
    assert len(await sw.fetch_log()) == 1


async def test_terminal_4xx_is_not_retried(sw):
    await sw.script([{"status": 410, "body": {"ack": False, "terminal": True, "code": "stale"}}])
    res = await sw.handle(candidate())
    assert res["status"] == "terminal"
    await sw.wait(1300)
    assert len(await sw.fetch_log()) == 1
    ev = (await sw.sent_events())[f"{CHANNEL_ID}-1234567890123456789"]
    assert ev["status"] == "terminal" and "stale" in ev["lastError"]


async def test_network_failure_retries_same_event_id_bounded(sw):
    await sw.script([{"throw": "Failed to fetch"}, {"status": 503, "body": None}])
    res = await sw.handle(candidate())
    assert res["status"] == "pending"
    await sw.wait(3600)  # backoff 1s then 2s
    log = await sw.fetch_log()
    assert len(log) == 3
    ids = {json.loads(e["body"])["event_id"] for e in log}
    assert ids == {f"{CHANNEL_ID}-1234567890123456789"}
    assert [json.loads(e["body"])["attempt"] for e in log] == [1, 2, 3]
    ev = (await sw.sent_events())[f"{CHANNEL_ID}-1234567890123456789"]
    assert ev["status"] == "acked" and ev["attempts"] == 3


async def test_retries_stop_after_max_attempts(sw):
    await sw.script([{"throw": "down"}] * 10)
    await sw.handle(candidate())
    await sw.wait(1000 + 2000 + 4000 + 8000 + 800)
    log = await sw.fetch_log()
    assert len(log) == 5
    ev = (await sw.sent_events())[f"{CHANNEL_ID}-1234567890123456789"]
    assert ev["status"] == "terminal"


async def test_worker_restart_keeps_dedupe(sw, fixture_server):
    await sw.handle(candidate())
    assert len(await sw.fetch_log()) == 1
    # Fresh worker: brand-new script instance (no in-memory globals) over the same
    # chrome.storage.local contents.
    storage = await sw.page.evaluate("window.__storage")
    page2 = await sw.page.context.new_page()
    await page2.add_init_script(STUB + f"window.__storage = {json.dumps(storage)};")
    await page2.goto(f"{fixture_server}/discord/channel.html")
    sw2 = SW(page2)
    await sw2.load()
    res = await sw2.handle(candidate())
    assert not res["accepted"] and "duplicate" in res["reason"]
    assert await sw2.fetch_log() == []


async def test_gating_channel_target_sender_paused(sw):
    assert (await sw.handle(candidate(channel_id="999999999999999999")))["reason"] == "channel mismatch"
    assert (await sw.handle(candidate(matched_target="other")))["reason"] == "target mismatch"
    assert (await sw.handle(candidate(), sender={"url": "https://evil.example/"}))["reason"] == "unexpected sender"
    assert (await sw.handle(candidate(event_id="short")))["reason"] == "bad event id"
    await sw.page.evaluate("window.__storage.config.paused = true; true")
    assert (await sw.handle(candidate()))["reason"] == "paused"
    assert await sw.fetch_log() == []


async def test_no_secret_is_terminal_without_network(sw):
    await sw.page.evaluate("window.__storage.config.pairingSecret = ''; true")
    res = await sw.handle(candidate())
    assert res["status"] == "terminal"
    assert await sw.fetch_log() == []


async def test_status_message_handler_hides_secret_from_content(sw):
    out = await sw.page.evaluate(
        """() => new Promise(resolve => window.__onMessage({type: 'get-config'}, SENDER, resolve))"""
        .replace("SENDER", json.dumps(SENDER))
    )
    cfg = out["config"]
    assert "pairingSecret" not in cfg and "bridgePort" not in cfg
    assert cfg["channelId"] == CHANNEL_ID and cfg["freshnessMs"] == 90_000
