import subprocess
import sys
import time

import pytest

from amore_ocarina_sniper.lock import LockHeld, ProcessLock
from amore_ocarina_sniper.models import PurchaseState, TriggerEvent
from amore_ocarina_sniper.store import StateError, StateStore

from .conftest import CHANNEL_ID, GUILD_ID, TARGET_ID


def ev(i: int = 1, **kw) -> TriggerEvent:
    now = int(time.time() * 1000)
    base = dict(
        event_id=f"{CHANNEL_ID}-{1000 + i}",
        message_id=str(1000 + i),
        channel_id=CHANNEL_ID,
        guild_id=GUILD_ID,
        matched_target=TARGET_ID,
        message_ts_ms=now,
        sent_at_ms=now,
        detected_at_offset_ms=5,
    )
    base.update(kw)
    return TriggerEvent(**base)


def test_initial_state_disarmed(store: StateStore):
    ctl = store.get_control()
    assert ctl.state == PurchaseState.DISARMED
    assert not ctl.purchase_disabled


def test_event_dedupe_by_event_and_message(store: StateStore):
    assert store.record_event(ev(1)) == "new"
    assert store.record_event(ev(1)) == "duplicate_event"
    assert store.record_event(ev(1, event_id="other-event-id-1234")) == "duplicate_message"
    assert store.record_event(ev(2)) == "new"


def test_arm_disarm_and_expiry(store: StateStore):
    now = int(time.time() * 1000)
    ctl = store.arm(now + 60_000, at_ms=now)
    assert ctl.state == PurchaseState.ARMED and ctl.is_armed(now)
    assert not ctl.is_armed(now + 60_001)
    with pytest.raises(StateError):
        store.arm(now - 1, at_ms=now)
    ctl = store.disarm()
    assert ctl.state == PurchaseState.DISARMED and ctl.armed_until_ms is None


def test_begin_attempt_requires_armed(store: StateStore):
    now = int(time.time() * 1000)
    assert not store.begin_attempt("e1", now)
    store.arm(now + 60_000, at_ms=now)
    assert store.begin_attempt("e1", now)
    assert store.get_control().state == PurchaseState.VERIFYING
    # Competing event while busy is refused.
    assert not store.begin_attempt("e2", now)
    # Expired armed session refuses too.
    store.finish_attempt("e1", PurchaseState.ARMED, "benign")
    assert not store.begin_attempt("e3", now + 61_000)


def test_full_purchase_flow_disables_until_reset(store: StateStore):
    now = int(time.time() * 1000)
    store.arm(now + 60_000, at_ms=now)
    assert store.begin_attempt("e1", now)
    assert store.transition("e1", {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "ok")
    assert store.persist_submission_intent("e1")
    assert store.get_control().state == PurchaseState.SUBMITTING
    store.mark_submitted("e1")
    ctl = store.finish_attempt("e1", PurchaseState.PURCHASED, "confirmed", order_id="702-1-1")
    assert ctl.state == PurchaseState.PURCHASED and ctl.purchase_disabled
    with pytest.raises(StateError):
        store.arm(now + 120_000, at_ms=now)
    assert not store.begin_attempt("e2", now)
    ctl = store.reset()
    assert ctl.state == PurchaseState.DISARMED and not ctl.purchase_disabled
    attempts = store.attempts_for("e1")
    assert attempts[0].intent_persisted_at_ms and attempts[0].submitted_at_ms
    assert attempts[0].order_id == "702-1-1"


def test_cas_transition_rejects_wrong_event_or_state(store: StateStore):
    now = int(time.time() * 1000)
    store.arm(now + 60_000, at_ms=now)
    store.begin_attempt("e1", now)
    assert not store.transition("e2", {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "x")
    assert not store.transition("e1", {PurchaseState.CHECKOUT_READY}, PurchaseState.SUBMITTING, "x")
    assert not store.persist_submission_intent("e1")  # not CHECKOUT_READY yet


def test_disarm_mid_attempt_blocks_intent_and_does_not_rearm(store: StateStore):
    now = int(time.time() * 1000)
    store.arm(now + 60_000, at_ms=now)
    store.begin_attempt("e1", now)
    store.transition("e1", {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "ok")
    store.disarm()
    assert not store.persist_submission_intent("e1")
    ctl = store.finish_attempt("e1", PurchaseState.ARMED, "offer rejected")
    assert ctl.state == PurchaseState.DISARMED


def test_recover_on_startup_rules(store: StateStore):
    now = int(time.time() * 1000)
    # ARMED -> DISARMED (explicit re-arm required)
    store.arm(now + 60_000, at_ms=now)
    assert store.recover_on_startup().state == PurchaseState.DISARMED
    # crash before intent -> DISARMED, attempt closed
    store.arm(now + 60_000, at_ms=now)
    store.begin_attempt("e1", now)
    assert store.recover_on_startup().state == PurchaseState.DISARMED
    assert store.attempts_for("e1")[0].final_state == "DISARMED"
    # crash after intent -> UNKNOWN + disabled
    store.arm(now + 60_000, at_ms=now)
    store.begin_attempt("e2", now)
    store.transition("e2", {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "ok")
    store.persist_submission_intent("e2")
    ctl = store.recover_on_startup()
    assert ctl.state == PurchaseState.UNKNOWN and ctl.purchase_disabled
    with pytest.raises(StateError):
        store.arm(now + 60_000, at_ms=now)
    assert store.attempts_for("e2")[0].final_state == "UNKNOWN"


def test_sticky_states_survive_disarm(store: StateStore):
    now = int(time.time() * 1000)
    store.arm(now + 60_000, at_ms=now)
    store.begin_attempt("e1", now)
    store.finish_attempt("e1", PurchaseState.NEEDS_ATTENTION, "captcha")
    assert store.disarm().state == PurchaseState.NEEDS_ATTENTION
    assert store.reset().state == PurchaseState.DISARMED


def test_two_processes_cannot_both_hold_the_lock(tmp_path):
    lock_path = tmp_path / "ocarina.lock"
    with ProcessLock(lock_path):
        code = (
            "import sys; from pathlib import Path; from amore_ocarina_sniper.lock import ProcessLock, LockHeld\n"
            "try:\n    ProcessLock(Path(sys.argv[1])).acquire(); print('ACQUIRED')\n"
            "except LockHeld:\n    print('HELD')\n"
        )
        out = subprocess.run([sys.executable, "-c", code, str(lock_path)], capture_output=True, text=True)
        assert "HELD" in out.stdout, out
    # released: reacquire in-process works
    lock = ProcessLock(lock_path)
    lock.acquire()
    lock.release()


def test_lock_reentry_in_same_process_is_rejected(tmp_path):
    lock_path = tmp_path / "ocarina.lock"
    a = ProcessLock(lock_path)
    a.acquire()
    try:
        with pytest.raises(LockHeld):
            ProcessLock(lock_path).acquire()
    finally:
        a.release()
