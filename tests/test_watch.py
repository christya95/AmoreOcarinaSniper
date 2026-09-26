"""Product watcher: second trigger source, gentle by construction. No browser, no network."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from amore_ocarina_sniper.config import WATCH_MIN_INTERVAL_S, WatchConfig
from amore_ocarina_sniper.coordinator import PurchaseCoordinator
from amore_ocarina_sniper.models import ChallengeDetected, ChallengeKind, PurchaseState
from amore_ocarina_sniper.telemetry import Telemetry
from amore_ocarina_sniper.watch import MAX_BACKOFF_S, WATCH_CHANNEL_ID, ProductWatcher

from .test_coordinator import FakeAdapter, arm, event


def build(config, store, *, dry_run=True, cooldown=120):
    cfg = replace(config, watch=WatchConfig(enabled=True, interval_s=30, retrigger_cooldown_s=cooldown))
    adapter = FakeAdapter(dry_run=dry_run)
    coord = PurchaseCoordinator(
        config=cfg, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False), dry_run=dry_run
    )
    watcher = ProductWatcher(
        config=cfg,
        store=store,
        adapter=adapter,
        telemetry=Telemetry(None, enabled=False),
        on_trigger=coord.handle_trigger,
        is_busy=lambda: coord.status()["busy"],
    )
    return watcher, coord, adapter


async def test_does_not_poll_unless_armed(config, store):
    watcher, _, adapter = build(config, store)
    assert await watcher.tick() is False
    assert adapter.calls == []
    arm(store)
    store.disarm("operator")
    assert await watcher.tick() is False
    assert adapter.calls == []


async def test_unpurchasable_page_never_triggers(config, store):
    watcher, _, adapter = build(config, store)
    arm(store)
    adapter.poll_result = replace(
        adapter.offer, in_stock=False, is_preorder=False, availability="Currently unavailable.",
        buy_now_available=False, add_to_cart_available=False,
    )
    for _ in range(3):
        assert await watcher.tick() is False
    assert adapter.calls == ["poll_offer"] * 3
    assert store.recent_events() == []
    assert store.get_control().state == PurchaseState.ARMED


async def test_purchasable_page_triggers_the_normal_attempt_path(config, store):
    watcher, coord, adapter = build(config, store, dry_run=True)
    arm(store)
    assert await watcher.tick() is True
    # The watcher's tab is promoted, then the coordinator re-verifies and runs the usual
    # dry-run attempt from it.
    assert adapter.calls == ["poll_offer", "promote_watch_page", "verify_offer", "prepare_checkout", "abandon"]
    events = store.recent_events()
    assert len(events) == 1 and events[0]["channel_id"] == WATCH_CHANNEL_ID
    assert events[0]["event_id"].startswith("watch-")
    assert coord.last_outcome is not None and coord.last_outcome.dry_run
    assert store.get_control().state == PurchaseState.ARMED


async def test_refused_attempt_starts_cooldown(config, store):
    watcher, _, adapter = build(config, store, dry_run=True, cooldown=3600)
    arm(store)
    assert await watcher.tick() is True  # dry-run attempt: not PURCHASED -> cooldown
    assert await watcher.tick() is False
    assert adapter.calls.count("poll_offer") == 1  # cooled down: no second poll at all


async def test_live_purchase_via_watcher_then_stops(config, store):
    watcher, _, adapter = build(config, store, dry_run=False)
    arm(store)
    assert await watcher.tick() is True
    ctl = store.get_control()
    assert ctl.state == PurchaseState.PURCHASED and ctl.purchase_disabled
    assert adapter.calls == [
        "poll_offer", "promote_watch_page", "verify_offer", "prepare_checkout", "submit_order", "confirm_order",
    ]
    # PURCHASED is not ARMED: the watcher goes quiet without touching the browser again.
    assert await watcher.tick() is False
    assert adapter.calls.count("poll_offer") == 1


async def test_challenge_backs_off_and_changes_no_state(config, store):
    watcher, _, adapter = build(config, store)
    arm(store)
    adapter.fail_poll = ChallengeDetected(ChallengeKind.CAPTCHA, "watch tab")
    assert await watcher.tick() is False
    assert await watcher.tick() is False
    assert watcher._challenges == 2
    assert watcher._delay() > 30 * 1.2  # backed off beyond the jittered base interval
    assert store.get_control().state == PurchaseState.ARMED
    adapter.fail_poll = None
    adapter.poll_result = replace(adapter.offer, in_stock=False, buy_now_available=False)
    await watcher.tick()
    assert watcher._challenges == 0


async def test_generic_error_is_swallowed(config, store):
    watcher, _, adapter = build(config, store)
    arm(store)
    adapter.fail_poll = RuntimeError("browser hiccup")
    assert await watcher.tick() is False
    assert store.get_control().state == PurchaseState.ARMED


async def test_timeouts_back_off_mildly_not_exponentially(config, store):
    """A slow Amazon during a rush must not push the watcher out to minutes."""
    watcher, _, adapter = build(config, store)
    arm(store)
    adapter.fail_poll = ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "product page did not render")
    for _ in range(5):
        assert await watcher.tick() is False
    assert watcher._failures == 5 and watcher._challenges == 0
    for _ in range(20):
        assert watcher._delay() <= 30 * 2 * 1.2


async def test_delay_is_jittered_and_capped(config, store):
    watcher, _, _ = build(config, store)
    for _ in range(50):
        assert 30 * 0.8 <= watcher._delay() <= 30 * 1.2
    watcher._challenges = 20
    assert watcher._delay() <= MAX_BACKOFF_S * 1.2
    assert WATCH_MIN_INTERVAL_S >= 10


async def test_transient_attempt_failure_does_not_start_cooldown(config, store):
    """Timed-out attempt: the very next poll may trigger again (the window is short)."""
    watcher, _, adapter = build(config, store, dry_run=True, cooldown=3600)
    arm(store)
    adapter.fail_prepare = ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "checkout surface did not appear")
    assert await watcher.tick() is True
    assert store.get_control().state == PurchaseState.ARMED
    assert await watcher.tick() is True  # no cooldown: polled and triggered again
    assert adapter.calls.count("poll_offer") == 2


async def test_skips_while_an_attempt_is_in_flight(config, store):
    watcher, coord, adapter = build(config, store, dry_run=True)
    arm(store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_verify(**_kw):
        started.set()
        await release.wait()
        return adapter.offer

    adapter.verify_offer = slow_verify
    store.record_event(event())
    task = asyncio.create_task(coord.handle_trigger(event()))
    await started.wait()
    assert await watcher.tick() is False  # busy: no poll, no second attempt
    assert "poll_offer" not in adapter.calls
    release.set()
    await task


async def test_run_loop_stops_promptly(config, store):
    watcher, _, adapter = build(config, store)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert time.monotonic() - t0 < 1
    assert adapter.calls == []
