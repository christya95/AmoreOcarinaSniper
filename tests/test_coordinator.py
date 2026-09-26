"""Coordinator state machine with a scripted fake adapter. No browser, no network."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from amore_ocarina_sniper import killswitch
from amore_ocarina_sniper.amazon.adapter import DryRunRefusal
from amore_ocarina_sniper.coordinator import MAX_CONSECUTIVE_PRE_INTENT_FAILURES, PurchaseCoordinator
from amore_ocarina_sniper.models import ChallengeDetected, ChallengeKind, PurchaseState, TriggerEvent
from amore_ocarina_sniper.telemetry import Telemetry

from .conftest import CHANNEL_ID, GUILD_ID, TARGET_ID
from .test_policy import good_checkout, good_offer


class FakeAdapter:
    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run
        self.offer = good_offer()
        self.checkout = good_checkout()
        self.order_id: str | None = "702-1234567-7654321"
        self.fail_verify: Exception | None = None
        self.fail_prepare: Exception | None = None
        self.fail_submit: Exception | None = None
        self.fail_confirm: Exception | None = None
        self.fail_poll: Exception | None = None
        self.poll_result = None  # watcher sees this; None = same as self.offer
        self.calls: list[str] = []
        self.submit_delay = 0.0
        self.on_submit = None

    async def ensure_ready(self):
        self.calls.append("ensure_ready")

    async def verify_offer(self):
        self.calls.append("verify_offer")
        if self.fail_verify:
            raise self.fail_verify
        return self.offer

    async def poll_offer(self):
        self.calls.append("poll_offer")
        if self.fail_poll:
            raise self.fail_poll
        return self.poll_result if self.poll_result is not None else self.offer

    async def prepare_checkout(self, offer):
        self.calls.append("prepare_checkout")
        if self.fail_prepare:
            raise self.fail_prepare
        return self.checkout

    async def submit_order(self):
        self.calls.append("submit_order")
        if self.dry_run:
            raise DryRunRefusal("dry-run")
        if self.on_submit:
            await self.on_submit()
        if self.fail_submit:
            raise self.fail_submit

    async def confirm_order(self):
        self.calls.append("confirm_order")
        if self.fail_confirm:
            raise self.fail_confirm
        return self.order_id

    async def abandon(self):
        self.calls.append("abandon")

    async def capture_artifact(self, label, *, html=False):
        self.calls.append(f"capture_artifact:{label}")
        return None


def event(i: int = 1) -> TriggerEvent:
    now = int(time.time() * 1000)
    return TriggerEvent(
        event_id=f"{CHANNEL_ID}-{5000 + i}",
        message_id=str(5000 + i),
        channel_id=CHANNEL_ID,
        guild_id=GUILD_ID,
        matched_target=TARGET_ID,
        message_ts_ms=now - 1500,
        sent_at_ms=now - 100,
        detected_at_offset_ms=20,
    )


@pytest.fixture
def live(config, store):
    adapter = FakeAdapter(dry_run=False)
    coord = PurchaseCoordinator(
        config=config, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False), dry_run=False
    )
    return coord, adapter, store


@pytest.fixture
def dry(config, store):
    adapter = FakeAdapter(dry_run=True)
    coord = PurchaseCoordinator(
        config=config, store=store, adapter=adapter, telemetry=Telemetry(None, enabled=False), dry_run=True
    )
    return coord, adapter, store


def arm(store, minutes=10):
    now = int(time.time() * 1000)
    store.arm(now + minutes * 60_000, at_ms=now)


async def test_not_armed_trigger_is_skipped(live):
    coord, adapter, store = live
    store.record_event(event())
    out = await coord.handle_trigger(event())
    assert "not armed" in out.reason
    assert adapter.calls == []


async def test_dry_run_never_submits(dry):
    coord, adapter, store = dry
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.dry_run and "would have submitted" in out.reason
    assert "submit_order" not in adapter.calls
    assert adapter.calls[-1] == "abandon"
    ctl = store.get_control()
    assert ctl.state == PurchaseState.ARMED and not ctl.purchase_disabled
    assert store.attempts_for(event().event_id)[0].intent_persisted_at_ms is None


async def test_live_purchase_confirms_and_disables(live):
    coord, adapter, store = live
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.PURCHASED and out.order_id == "702-1234567-7654321"
    assert adapter.calls == ["verify_offer", "prepare_checkout", "submit_order", "confirm_order"]
    ctl = store.get_control()
    assert ctl.purchase_disabled
    # Second alert: permanently blocked until reset.
    out2 = await coord.handle_trigger(event(2))
    assert "disabled" in out2.reason
    assert adapter.calls.count("submit_order") == 1
    store.reset()
    assert store.get_control().state == PurchaseState.DISARMED


async def test_offer_rejected_stays_armed(live):
    coord, adapter, store = live
    adapter.offer = replace(good_offer(), in_stock=False, availability="Currently unavailable.")
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.ARMED and "not in stock" in out.reason
    assert "prepare_checkout" not in adapter.calls
    # Stock appears for the next alert: still armed, proceeds.
    adapter.offer = good_offer()
    out2 = await coord.handle_trigger(event(2))
    assert out2.final_state == PurchaseState.PURCHASED


async def test_checkout_rejected_abandons_without_submit(live):
    coord, adapter, store = live
    adapter.checkout = replace(good_checkout(), line_item_count=2)
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.ARMED and "2 line items" in out.reason
    # Evidence is captured while still on the review page, i.e. before abandon() navigates away.
    assert adapter.calls == ["verify_offer", "prepare_checkout", "capture_artifact:checkout-rejected", "abandon"]


async def test_captcha_before_intent_needs_attention(live):
    coord, adapter, store = live
    adapter.fail_verify = ChallengeDetected(ChallengeKind.CAPTCHA, "robot check")
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.NEEDS_ATTENTION
    assert not store.get_control().purchase_disabled
    out2 = await coord.handle_trigger(event(2))
    assert "not armed" in out2.reason
    assert adapter.calls.count("verify_offer") == 1


async def test_expired_session_challenge(live):
    coord, adapter, store = live
    adapter.fail_prepare = ChallengeDetected(ChallengeKind.LOGIN_REQUIRED)
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.NEEDS_ATTENTION
    assert "submit_order" not in adapter.calls


async def test_crash_before_intent_returns_to_armed_then_needs_attention(live):
    coord, adapter, store = live
    adapter.fail_prepare = RuntimeError("selector timeout")
    arm(store)
    for i in range(MAX_CONSECUTIVE_PRE_INTENT_FAILURES - 1):
        out = await coord.handle_trigger(event(i + 1))
        assert out.final_state == PurchaseState.ARMED
    out = await coord.handle_trigger(event(99))
    assert out.final_state == PurchaseState.NEEDS_ATTENTION
    assert "submit_order" not in adapter.calls


async def test_crash_after_submit_is_unknown_and_never_retried(live):
    coord, adapter, store = live
    adapter.fail_confirm = RuntimeError("browser died")
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.UNKNOWN
    ctl = store.get_control()
    assert ctl.purchase_disabled and "unknown" in (ctl.disabled_reason or "").lower() or ctl.purchase_disabled
    attempt = store.attempts_for(event().event_id)[0]
    assert attempt.intent_persisted_at_ms and attempt.submitted_at_ms
    out2 = await coord.handle_trigger(event(2))
    assert "disabled" in out2.reason
    assert adapter.calls.count("submit_order") == 1


async def test_ambiguous_submission_no_confirmation(live):
    coord, adapter, store = live
    adapter.order_id = None
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.UNKNOWN and "no confirmation" in out.reason
    assert store.get_control().purchase_disabled


async def test_challenge_after_intent_is_unknown(live):
    coord, adapter, store = live
    adapter.fail_confirm = ChallengeDetected(ChallengeKind.PAYMENT_CHALLENGE)
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.UNKNOWN


async def test_kill_switch_checked_immediately_before_submission(live, config):
    coord, adapter, store = live
    killswitch.engage(config.paths.kill_switch_path)
    arm(store)
    out = await coord.handle_trigger(event())
    assert "kill switch" in out.reason
    assert "submit_order" not in adapter.calls
    assert store.get_control().state == PurchaseState.DISARMED


async def test_disarm_during_checkout_blocks_submission(live):
    coord, adapter, store = live
    arm(store)

    original = adapter.prepare_checkout

    async def prepare_and_disarm(offer):
        snap = await original(offer)
        store.disarm()
        return snap

    adapter.prepare_checkout = prepare_and_disarm
    out = await coord.handle_trigger(event())
    assert "submit_order" not in adapter.calls
    assert out.final_state == PurchaseState.DISARMED


async def test_armed_session_expiry_blocks_attempt(live):
    coord, adapter, store = live
    now = int(time.time() * 1000)
    store.arm(now + 1000, at_ms=now)
    # Simulate an expired session by disarming via expiry watcher semantics.
    store.disarm("armed session expired")
    out = await coord.handle_trigger(event())
    assert "not armed" in out.reason


async def test_concurrent_triggers_only_one_attempt(live):
    coord, adapter, store = live
    arm(store)
    gate = asyncio.Event()

    async def slow_submit():
        await gate.wait()

    adapter.on_submit = slow_submit
    t1 = asyncio.create_task(coord.handle_trigger(event(1)))
    await asyncio.sleep(0.05)
    out2 = await coord.handle_trigger(event(2))
    assert "busy" in out2.reason
    gate.set()
    out1 = await t1
    assert out1.final_state == PurchaseState.PURCHASED
    assert adapter.calls.count("submit_order") == 1


async def test_status_shape(live):
    coord, adapter, store = live
    s = coord.status()
    assert s["state"] == "DISARMED" and s["dry_run"] is False and s["kill_switch"] is False
