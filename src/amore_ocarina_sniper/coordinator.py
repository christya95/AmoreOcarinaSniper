"""Purchase coordinator: the state machine between a trigger and (at most) one order.

Ordering guarantees are enforced by the store's compare-and-set transitions plus a
process-local asyncio lock. Submission intent is persisted *before* the final click;
any failure after that point ends in UNKNOWN and permanently disables further
purchases until an explicit operator reset. This does not make browser automation
exactly-once; it makes "we clicked twice" impossible and "we do not know" explicit.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from . import killswitch
from .amazon.adapter import DryRunRefusal, PurchaseAdapter
from .config import AppConfig
from .models import (
    AttemptOutcome,
    ChallengeDetected,
    ChallengeKind,
    PurchaseState,
    TriggerEvent,
)
from .notify import PRIORITY_HIGH, PRIORITY_URGENT, Notifier
from .policy import evaluate_checkout, evaluate_offer
from .store import StateStore, now_ms
from .telemetry import Telemetry, Timeline

log = logging.getLogger("ocarina.coordinator")

MAX_CONSECUTIVE_PRE_INTENT_FAILURES = 3
# Extra passes over verify → checkout *before* the submission intent when a step timed out
# (slow page, add-to-cart not registering under load). Nothing has been submitted at that
# point, so this is not the post-intent "never auto-retry" case; it is bounded, and each pass
# still runs the full offer + review-page policy.
MAX_TRANSIENT_RETRIES = 2
# A watcher-triggered attempt may read the product page the watcher loaded moments ago
# instead of navigating again (saves one full page load); anything older is reloaded.
WATCH_PAGE_REUSE_S = 10.0
# Consecutive transient failures before pushing a heads-up (the bot stays ARMED).
TRANSIENT_STREAK_NOTIFY = 3


def is_transient(exc: BaseException) -> bool:
    """Timing failures that leave nothing submitted and are worth one more try."""
    if isinstance(exc, ChallengeDetected):
        return exc.kind == ChallengeKind.UNKNOWN_PAGE
    # Playwright's TimeoutError is not the builtin one; match by name to avoid a hard
    # dependency on the browser library in the state machine.
    return type(exc).__name__ == "TimeoutError"


class PurchaseCoordinator:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: StateStore,
        adapter: PurchaseAdapter,
        telemetry: Telemetry,
        dry_run: bool,
        kill_switch_path: Path | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        import asyncio

        self.config = config
        self.store = store
        self.adapter = adapter
        self.telemetry = telemetry
        self.notifier = notifier or Notifier(config.notify)
        self.dry_run = dry_run
        self.kill_switch_path = kill_switch_path or config.paths.kill_switch_path
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._transient_streak = 0
        self._offer_was_present = False  # set per attempt; drives the "go manual" push
        self.last_outcome: AttemptOutcome | None = None

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        ctl = self.store.get_control()
        now = now_ms()
        return {
            "state": ctl.state.value,
            "armed": ctl.is_armed(now),
            "armed_until_ms": ctl.armed_until_ms,
            "purchase_disabled": ctl.purchase_disabled,
            "disabled_reason": ctl.disabled_reason,
            "dry_run": self.dry_run,
            "kill_switch": killswitch.is_engaged(self.kill_switch_path),
            "busy": self._lock.locked(),
            "notify": self.notifier.enabled,
            "last_outcome": (
                {
                    "event_id": self.last_outcome.event_id,
                    "final_state": self.last_outcome.final_state.value,
                    "reason": self.last_outcome.reason,
                    "order_id": self.last_outcome.order_id,
                    "dry_run": self.last_outcome.dry_run,
                }
                if self.last_outcome
                else None
            ),
        }

    # -------------------------------------------------------------- triggers
    async def handle_trigger(self, event: TriggerEvent) -> AttemptOutcome:
        tl = Timeline(event.event_id)
        tl.set_delivery(
            message_ts_ms=event.message_ts_ms,
            sent_at_ms=event.sent_at_ms,
            received_wall_ms=tl.created_wall_ms,
            detected_at_offset_ms=event.detected_at_offset_ms,
        )
        if self._lock.locked():
            return self._skip(event, "busy: another attempt in flight", tl)
        async with self._lock:
            if not self.store.begin_attempt(event.event_id, tl.created_wall_ms):
                ctl = self.store.get_control()
                if ctl.purchase_disabled:
                    reason = f"purchases disabled: {ctl.disabled_reason}"
                elif ctl.state != PurchaseState.ARMED:
                    reason = f"not armed (state={ctl.state.value})"
                else:
                    reason = "armed session expired"
                return self._skip(event, reason, tl)
            tl.mark("attempt_started")
            outcome = await self._run_attempt(event, tl)
        self.last_outcome = outcome
        self.telemetry.emit_timeline(
            tl, final_state=outcome.final_state.value, reason=outcome.reason, dry_run=outcome.dry_run
        )
        log.info("attempt %s -> %s (%s)", event.event_id, outcome.final_state.value, outcome.reason)
        self._notify_outcome(event, outcome)
        return outcome

    def _notify_outcome(self, event: TriggerEvent, outcome: AttemptOutcome) -> None:
        """Push to the operator's phone. After the attempt; never influences it."""
        state = outcome.final_state
        src = event.source
        if state == PurchaseState.PURCHASED:
            self.notifier.fire(
                "ORDER PLACED — do NOT buy manually",
                f"Order {outcome.order_id} confirmed (trigger: {src}). Bot purchasing is now disabled.",
                priority=PRIORITY_URGENT,
                tags="white_check_mark",
            )
        elif state in (PurchaseState.UNKNOWN, PurchaseState.NEEDS_ATTENTION):
            self.notifier.fire(
                f"Bot {state.value}: check the browser window",
                f"{outcome.reason} (trigger: {src}). Nothing more will be attempted until you look.",
                priority=PRIORITY_URGENT,
                tags="warning",
            )
        elif self._offer_was_present and not outcome.order_id and not outcome.dry_run:
            # Stock was there and we refused: the drop is live and the bot is out. Go manual.
            self.notifier.fire(
                "Stock seen but bot REFUSED — buy manually now",
                f"{outcome.reason} (trigger: {src})",
                priority=PRIORITY_HIGH,
                tags="rotating_light",
            )
        self._offer_was_present = False

    def _skip(self, event: TriggerEvent, reason: str, tl: Timeline) -> AttemptOutcome:
        self.store.set_event_disposition(event.event_id, f"skipped: {reason}")
        self.telemetry.emit("trigger_skipped", event_id=event.event_id, reason=reason)
        log.info("trigger %s skipped: %s", event.event_id, reason)
        return AttemptOutcome(event.event_id, self.store.get_control().state, reason, dry_run=self.dry_run)

    # --------------------------------------------------------------- attempt
    async def _run_attempt(self, event: TriggerEvent, tl: Timeline) -> AttemptOutcome:
        eid = event.event_id
        for attempt in range(1 + MAX_TRANSIENT_RETRIES):
            try:
                result = await self._verify_and_prepare(event, tl, attempt)
            except Exception as exc:  # noqa: BLE001 - pre-intent failures must never leave BUSY states
                await self._abandon()
                transient = is_transient(exc)
                if transient and killswitch.is_engaged(self.kill_switch_path):
                    ctl = self._finish(eid, PurchaseState.DISARMED, "kill switch engaged during retry")
                    return AttemptOutcome(eid, ctl.state, "kill switch engaged")
                if transient and attempt < MAX_TRANSIENT_RETRIES:
                    log.warning("attempt %s: transient failure (%s); retrying (%d left)",
                                eid, exc, MAX_TRANSIENT_RETRIES - attempt)
                    self.telemetry.emit("transient_retry", event_id=eid, error=str(exc), pass_no=attempt + 1)
                    tl.mark(f"retry_{attempt + 1}")
                    continue
                if isinstance(exc, ChallengeDetected) and not transient:
                    kind = exc.kind.value
                    artifact = await self.adapter.capture_artifact(f"challenge-{kind}")
                    reason = f"challenge before submission: {exc}"
                    ctl = self._finish(eid, PurchaseState.NEEDS_ATTENTION, reason)
                    self.telemetry.emit("challenge", event_id=eid, challenge=kind, artifact=artifact)
                    return AttemptOutcome(eid, ctl.state, f"needs attention: {exc}")
                if transient:
                    artifact = await self.adapter.capture_artifact("transient-failure")
                    self.telemetry.emit("transient_failure", event_id=eid, error=str(exc), artifact=artifact)
                    return self._finish_transient(eid, f"transient failure before submission: {exc}")
                log.exception("pre-intent failure for %s", eid)
                return self._finish_pre_intent(eid, f"error before submission: {exc!r}", benign=False)
            if isinstance(result, AttemptOutcome):
                return result
            break
        return await self._commit(eid, tl)

    async def _verify_and_prepare(self, event: TriggerEvent, tl: Timeline, attempt: int):
        """Offer + review-page policy. Returns an AttemptOutcome (refusal) or None (all passed)."""
        eid = event.event_id
        policy, target = self.config.policy, self.config.target
        reuse = WATCH_PAGE_REUSE_S if (event.source == "watcher" and attempt == 0) else 0.0
        offer = await self.adapter.verify_offer(reuse_within_s=reuse)
        tl.mark("offer_verified")
        decision = evaluate_offer(offer, policy, target)
        self.telemetry.emit(
            "offer_checked",
            event_id=eid,
            ok=decision.ok,
            reasons=decision.reasons,
            price=str(offer.price),
            seller=offer.seller,
            in_stock=offer.in_stock,
            is_preorder=offer.is_preorder,
            offer=asdict(offer),
        )
        offer_present = (
            offer.in_stock or offer.is_preorder or offer.price is not None
            or offer.buy_now_available or offer.add_to_cart_available
        )
        self._offer_was_present = offer_present
        if not decision.ok:
            if offer_present:
                # The interesting case: there WAS an offer and we still refused. Keep the
                # evidence (off the critical path; the attempt is already over).
                artifact = await self.adapter.capture_artifact("offer-rejected", html=True)
                self.telemetry.emit(
                    "offer_rejected_with_offer_present", event_id=eid, reasons=decision.reasons,
                    artifact=artifact,
                )
            return self._finish_pre_intent(
                eid, "offer rejected: " + "; ".join(decision.reasons), benign=True
            )

        snapshot = await self.adapter.prepare_checkout(offer)
        tl.mark("checkout_prepared")
        decision = evaluate_checkout(snapshot, policy, target)
        self.telemetry.emit(
            "checkout_checked",
            event_id=eid,
            ok=decision.ok,
            reasons=decision.reasons,
            total=str(snapshot.total),
            snapshot=asdict(snapshot),
        )
        if not decision.ok:
            # Capture BEFORE abandon() navigates away from the review page.
            artifact = await self.adapter.capture_artifact("checkout-rejected", html=True)
            self.telemetry.emit(
                "checkout_rejected", event_id=eid, reasons=decision.reasons, artifact=artifact
            )
            await self._abandon()
            return self._finish_pre_intent(
                eid, "checkout rejected: " + "; ".join(decision.reasons), benign=True
            )
        return None

    async def _commit(self, eid: str, tl: Timeline) -> AttemptOutcome:
        """All checks passed on the review page: persist intent, click once, confirm."""
        try:
            if not self.store.transition(
                eid, {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "all policy checks passed"
            ):
                await self._abandon()
                return self._finish_state_changed(eid, "state changed during verification")
            tl.mark("checkout_ready")

            if self.dry_run:
                await self._abandon()
                self._consecutive_failures = 0
                self._transient_streak = 0
                ctl = self._finish(
                    eid, PurchaseState.ARMED, "dry-run: all checks passed; submission skipped", dry_run=True
                )
                return AttemptOutcome(eid, ctl.state, "dry-run: would have submitted", dry_run=True)

            if killswitch.is_engaged(self.kill_switch_path):
                await self._abandon()
                ctl = self._finish(eid, PurchaseState.DISARMED, "kill switch engaged before submission")
                return AttemptOutcome(eid, ctl.state, "kill switch engaged")

            if not self.store.persist_submission_intent(eid):
                await self._abandon()
                return self._finish_state_changed(eid, "disarmed/expired before submission intent")
            tl.mark("intent_persisted")
        except Exception as exc:  # noqa: BLE001 - pre-intent failures must never leave BUSY states
            log.exception("pre-intent failure for %s", eid)
            await self._abandon()
            return self._finish_pre_intent(eid, f"error before submission: {exc!r}", benign=False)

        # ---- point of no return: intent persisted; any failure => UNKNOWN ----
        try:
            await self.adapter.submit_order()
            self.store.mark_submitted(eid)
            tl.mark("submitted")
            order_id = await self.adapter.confirm_order()
            tl.mark("confirmed" if order_id else "confirmation_timeout")
        except DryRunRefusal as exc:  # defence in depth; should be unreachable
            ctl = self._finish(eid, PurchaseState.UNKNOWN, f"adapter refused: {exc}")
            return AttemptOutcome(eid, ctl.state, str(exc))
        except ChallengeDetected as exc:
            artifact = await self.adapter.capture_artifact(f"post-intent-{exc.kind.value}")
            ctl = self._finish(eid, PurchaseState.UNKNOWN, f"challenge after submission intent: {exc}")
            self.telemetry.emit("challenge", event_id=eid, challenge=exc.kind.value, artifact=artifact)
            return AttemptOutcome(eid, ctl.state, f"unknown outcome: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("post-intent failure for %s", eid)
            ctl = self._finish(eid, PurchaseState.UNKNOWN, f"error after submission intent: {exc!r}")
            return AttemptOutcome(eid, ctl.state, f"unknown outcome: {exc!r}")

        if order_id:
            ctl = self._finish(eid, PurchaseState.PURCHASED, f"order confirmed {order_id}", order_id=order_id)
            self.telemetry.emit("purchased", event_id=eid, order_id=order_id)
            return AttemptOutcome(eid, ctl.state, "purchased", order_id=order_id)
        ctl = self._finish(eid, PurchaseState.UNKNOWN, "no order confirmation observed; reconcile manually")
        return AttemptOutcome(eid, ctl.state, "unknown outcome: no confirmation")

    # --------------------------------------------------------------- helpers
    def _finish(self, eid: str, final_state: PurchaseState, reason: str, **kw):
        kw.setdefault("dry_run", self.dry_run)
        return self.store.finish_attempt(eid, final_state, reason, **kw)

    async def _abandon(self) -> None:
        try:
            await self.adapter.abandon()
        except Exception as exc:  # noqa: BLE001
            log.warning("abandon failed: %s", exc)

    def _finish_transient(self, eid: str, reason: str) -> AttemptOutcome:
        """Timed out before anything was submitted. Under a rush this is the *expected* failure,
        so the bot stays ARMED for the next trigger; a streak only earns the operator a push."""
        self._transient_streak += 1
        if self._transient_streak == TRANSIENT_STREAK_NOTIFY:
            self.notifier.fire(
                f"{self._transient_streak} attempts timed out in a row",
                f"{reason}. Amazon may be overloaded or the page changed; the bot stays armed.",
                priority=PRIORITY_HIGH,
                tags="hourglass",
            )
        ctl = self._finish(eid, PurchaseState.ARMED, reason)
        return AttemptOutcome(eid, ctl.state, reason, transient=True)

    def _finish_pre_intent(self, eid: str, reason: str, *, benign: bool) -> AttemptOutcome:
        """Nothing was submitted. Stay ARMED unless failures pile up (selector drift etc.)."""
        if benign:
            self._consecutive_failures = 0
            self._transient_streak = 0
            ctl = self._finish(eid, PurchaseState.ARMED, reason)
            return AttemptOutcome(eid, ctl.state, reason)
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_PRE_INTENT_FAILURES:
            ctl = self._finish(
                eid,
                PurchaseState.NEEDS_ATTENTION,
                f"{reason} ({self._consecutive_failures} consecutive failures)",
            )
            return AttemptOutcome(eid, ctl.state, reason)
        ctl = self._finish(eid, PurchaseState.ARMED, reason)
        return AttemptOutcome(eid, ctl.state, reason)

    def _finish_state_changed(self, eid: str, reason: str) -> AttemptOutcome:
        ctl = self.store.get_control()
        if ctl.active_event_id == eid:
            ctl = self._finish(eid, PurchaseState.DISARMED, reason)
        return AttemptOutcome(eid, ctl.state, reason)
