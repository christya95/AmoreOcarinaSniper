"""Coordinator state machine with a scripted fake adapter. No browser, no network."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from amore_ocarina_sniper import killswitch
from amore_ocarina_sniper.amazon.adapter import DryRunRefusal
from amore_ocarina_sniper.coordinator import (
    MAX_CONSECUTIVE_PRE_INTENT_FAILURES,
    MAX_TRANSIENT_RETRIES,
    WATCH_PAGE_REUSE_S,
    PurchaseCoordinator,
)
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
        # Scripted transient failures: pop one exception per prepare_checkout call.
        self.prepare_failures: list[Exception] = []
        self.reuse_requests: list[float] = []

    async def ensure_ready(self):
        self.calls.append("ensure_ready")

    def promote_watch_page(self):
        self.calls.append("promote_watch_page")
        return True

    async def verify_offer(self, *, reuse_within_s: float = 0.0):
        self.calls.append("verify_offer")
        self.reuse_requests.append(reuse_within_s)
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
        if self.prepare_failures:
            raise self.prepare_failures.pop(0)
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


async def test_transient_timeout_is_retried_then_purchases(live):
    """Slow cart page under load: the attempt retries before intent and still buys once."""
    coord, adapter, store = live
    adapter.prepare_failures = [ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "checkout surface did not appear")]
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.PURCHASED
    assert adapter.calls.count("prepare_checkout") == 2
    assert adapter.calls.count("submit_order") == 1
    # The retry re-verifies the offer from the product page abandon() just reloaded (bounded
    # reuse window; anything older is navigated again by the adapter).
    assert adapter.reuse_requests == [0.0, WATCH_PAGE_REUSE_S]
    # No evidence capture while a retry is still to come (that would cost time on the path).
    assert not any(c.startswith("capture_artifact") for c in adapter.calls)


async def test_amazon_error_page_is_retried_like_a_timeout(live):
    """The 'Sorry! Something went wrong' page under load is overload, not a ban."""
    coord, adapter, store = live
    adapter.prepare_failures = [ChallengeDetected(ChallengeKind.SERVER_ERROR, "sorry! something went wrong")]
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.PURCHASED
    assert adapter.calls.count("prepare_checkout") == 2


async def test_final_transient_failure_captures_evidence_before_abandon(live):
    coord, adapter, store = live
    adapter.fail_prepare = ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "checkout page without an enabled control")
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.transient and out.final_state == PurchaseState.ARMED
    idx = adapter.calls.index("capture_artifact:challenge-unknown_page")
    assert adapter.calls[idx + 1] == "abandon"  # screenshot shows the page that failed, not the product page
    assert adapter.calls.count("capture_artifact:challenge-unknown_page") == 1  # only on the last pass


async def test_access_denied_page_still_stops_the_bot(live):
    coord, adapter, store = live
    adapter.prepare_failures = [ChallengeDetected(ChallengeKind.ACCESS_DENIED, "to discuss automated access")]
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.NEEDS_ATTENTION
    assert adapter.calls.count("prepare_checkout") == 1
    idx = adapter.calls.index("capture_artifact:challenge-access_denied")
    assert adapter.calls[idx + 1] == "abandon"


async def test_transient_failures_exhaust_retries_but_stay_armed(live):
    coord, adapter, store = live
    adapter.fail_prepare = ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "target item not in cart")
    arm(store)
    for i in range(MAX_CONSECUTIVE_PRE_INTENT_FAILURES + 2):
        out = await coord.handle_trigger(event(i + 1))
        assert out.final_state == PurchaseState.ARMED, out.reason
        assert out.transient
    # 1 + MAX_TRANSIENT_RETRIES passes per trigger, never NEEDS_ATTENTION, nothing submitted.
    assert adapter.calls.count("prepare_checkout") == (MAX_CONSECUTIVE_PRE_INTENT_FAILURES + 2) * (
        1 + MAX_TRANSIENT_RETRIES
    )
    assert "submit_order" not in adapter.calls
    assert store.get_control().state == PurchaseState.ARMED


async def test_playwright_style_timeout_counts_as_transient(live):
    coord, adapter, store = live

    class TimeoutError(Exception):  # noqa: A001 - mimics playwright's TimeoutError by name
        pass

    adapter.prepare_failures = [TimeoutError("goto exceeded 15000ms")]
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.PURCHASED
    assert adapter.calls.count("prepare_checkout") == 2


async def test_captcha_is_not_retried(live):
    coord, adapter, store = live
    adapter.prepare_failures = [ChallengeDetected(ChallengeKind.CAPTCHA, "robot check")]
    arm(store)
    out = await coord.handle_trigger(event())
    assert out.final_state == PurchaseState.NEEDS_ATTENTION
    assert adapter.calls.count("prepare_checkout") == 1


async def test_watch_trigger_may_reuse_fresh_page(live):
    from amore_ocarina_sniper.coordinator import WATCH_PAGE_REUSE_S
    from amore_ocarina_sniper.watch import make_watch_event

    coord, adapter, store = live
    arm(store)
    out = await coord.handle_trigger(make_watch_event(TARGET_ID))
    assert out.final_state == PurchaseState.PURCHASED
    assert adapter.reuse_requests == [WATCH_PAGE_REUSE_S]


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
