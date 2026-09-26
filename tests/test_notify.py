"""Operator notifications: fire-and-forget, never on the critical path, never raising."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from amore_ocarina_sniper.config import ConfigError, NotifyConfig, load_config_dict
from amore_ocarina_sniper.coordinator import PurchaseCoordinator
from amore_ocarina_sniper.models import PurchaseState
from amore_ocarina_sniper.notify import Notifier
from amore_ocarina_sniper.telemetry import Telemetry

from .test_config import _example
from .test_coordinator import FakeAdapter, arm, event


@pytest.fixture
async def sink():
    received: list[dict] = []

    async def handle(request: web.Request):
        received.append({"topic": request.match_info["topic"], "headers": dict(request.headers),
                         "body": await request.text()})
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/{topic}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"http://127.0.0.1:{port}", received
    await runner.cleanup()


async def test_send_posts_title_priority_and_body(sink):
    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="ocarina-test", ntfy_server=url))
    assert await n.send("Hello", "body text", priority="5", tags="warning")
    assert received[0]["topic"] == "ocarina-test"
    assert received[0]["headers"]["Title"] == "Hello"
    assert received[0]["headers"]["Priority"] == "5"
    assert received[0]["headers"]["Tags"] == "warning"
    assert received[0]["body"] == "body text"


async def test_disabled_notifier_is_silent(sink):
    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="", ntfy_server=url))
    assert not n.enabled
    n.fire("x", "y")
    assert await n.send("x", "y") is False
    await n.close()
    assert received == []


async def test_unreachable_server_never_raises():
    n = Notifier(NotifyConfig(ntfy_topic="t", ntfy_server="http://127.0.0.1:9", timeout_s=1))
    assert await n.send("x", "y") is False
    n.fire("x", "y")
    await n.close()


async def test_purchase_and_refusal_push_to_phone(config, store, sink):
    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="t", ntfy_server=url))
    adapter = FakeAdapter(dry_run=False)
    coord = PurchaseCoordinator(
        config=config, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False),
        dry_run=False, notifier=n,
    )
    arm(store)
    # Stock present but checkout refused -> "buy manually now" push.
    adapter.checkout.address_text = "9 Other Road"
    out = await coord.handle_trigger(event(1))
    assert out.final_state == PurchaseState.ARMED
    await n.close()
    assert len(received) == 1 and "REFUSED" in received[0]["headers"]["Title"]
    assert "address" in received[0]["body"]
    assert "Other Road" not in received[0]["body"]  # reasons only, never page text

    adapter.checkout.address_text = "Josua Example 123 Maple Street Milton, ON"
    out = await coord.handle_trigger(event(2))
    assert out.final_state == PurchaseState.PURCHASED
    await n.close()
    assert len(received) == 2
    assert "ORDER PLACED" in received[1]["headers"]["Title"]
    assert received[1]["headers"]["Priority"] == "5"
    assert out.order_id in received[1]["body"]


async def test_retry_with_stock_present_pushes_an_informational_heads_up(config, store, sink):
    from amore_ocarina_sniper.models import ChallengeDetected, ChallengeKind

    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="t", ntfy_server=url))
    adapter = FakeAdapter(dry_run=False)
    coord = PurchaseCoordinator(
        config=config, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False),
        dry_run=False, notifier=n,
    )
    arm(store)
    adapter.prepare_failures = [ChallengeDetected(ChallengeKind.SERVER_ERROR, "sorry! something went wrong")]
    out = await coord.handle_trigger(event(1))
    assert out.final_state == PurchaseState.PURCHASED
    await n.close()
    by_title = {r["headers"]["Title"]: r for r in received}  # delivery order is not deterministic
    assert len(by_title) == 2
    retry = next(r for t, r in by_title.items() if t.startswith("Stock seen"))
    assert "Do NOT buy yet" in retry["body"]
    assert retry["headers"]["Priority"] == "3"  # informational, below the REFUSED push
    assert any(t.startswith("ORDER PLACED") for t in by_title)


async def test_arm_expiry_warns_then_disarms_with_a_push(store, sink):
    import time

    from amore_ocarina_sniper.app import _expiry_watch

    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="t", ntfy_server=url))
    now = int(time.time() * 1000)
    store.arm(now + 10 * 60_000, at_ms=now)  # expires in 10 min: inside the 30-min warning band
    stop = asyncio.Event()
    task = asyncio.create_task(_expiry_watch(store, stop, n, period_s=0.02))
    await asyncio.sleep(0.1)
    await n.close()
    assert [r["headers"]["Title"] for r in received] == ["Bot arm window ends in 30 minutes"]

    store.arm(now + 1, at_ms=now)  # expires within a millisecond of now
    await asyncio.sleep(0.1)
    stop.set()
    await task
    await n.close()
    assert store.get_control().state == PurchaseState.DISARMED
    titles = [r["headers"]["Title"] for r in received]
    assert titles[-1] == "Bot DISARMED: armed session expired"
    assert received[-1]["headers"]["Priority"] == "4"
    assert len(titles) == 2  # the warning is not repeated


async def test_dry_run_refusal_does_not_push(config, store, sink):
    url, received = sink
    n = Notifier(NotifyConfig(ntfy_topic="t", ntfy_server=url))
    adapter = FakeAdapter(dry_run=True)
    coord = PurchaseCoordinator(
        config=config, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False),
        dry_run=True, notifier=n,
    )
    arm(store)
    await coord.handle_trigger(event(1))
    await n.close()
    await asyncio.sleep(0)
    assert received == []


def test_notify_config_validation(tmp_path):
    raw = _example()
    assert load_config_dict(raw, base_dir=tmp_path).notify.ntfy_topic == ""
    raw["notify"] = {"ntfy_topic": "my-secret-topic_1"}
    cfg = load_config_dict(raw, base_dir=tmp_path)
    assert cfg.notify.ntfy_topic == "my-secret-topic_1" and cfg.notify.ntfy_server == "https://ntfy.sh"
    raw["notify"] = {"ntfy_topic": "bad topic!"}
    with pytest.raises(ConfigError):
        load_config_dict(raw, base_dir=tmp_path)
    raw["notify"] = {"ntfy_topic": "ok", "ntfy_server": "http://ntfy.sh"}
    with pytest.raises(ConfigError):
        load_config_dict(raw, base_dir=tmp_path)
