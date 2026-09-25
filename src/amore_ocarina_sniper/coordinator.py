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
    PurchaseState,
    TriggerEvent,
)
from .policy import evaluate_checkout, evaluate_offer
from .store import StateStore, now_ms
from .telemetry import Telemetry, Timeline

log = logging.getLogger("ocarina.coordinator")

MAX_CONSECUTIVE_PRE_INTENT_FAILURES = 3


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
    ) -> None:
        import asyncio

        self.config = config
        self.store = store
        self.adapter = adapter
        self.telemetry = telemetry
        self.dry_run = dry_run
        self.kill_switch_path = kill_switch_path or config.paths.kill_switch_path
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
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
        return outcome

    def _skip(self, event: TriggerEvent, reason: str, tl: Timeline) -> AttemptOutcome:
        self.store.set_event_disposition(event.event_id, f"skipped: {reason}")
        self.telemetry.emit("trigger_skipped", event_id=event.event_id, reason=reason)
        log.info("trigger %s skipped: %s", event.event_id, reason)
        return AttemptOutcome(event.event_id, self.store.get_control().state, reason, dry_run=self.dry_run)

    # --------------------------------------------------------------- attempt
    async def _run_attempt(self, event: TriggerEvent, tl: Timeline) -> AttemptOutcome:
        eid = event.event_id
        policy, target = self.config.policy, self.config.target
        try:
            offer = await self.adapter.verify_offer()
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
            )
            if not decision.ok:
                if offer.in_stock:
                    # The interesting case: stock was there and we still refused. Keep the
                    # evidence (off the critical path; the attempt is already over).
                    artifact = await self.adapter.capture_artifact("offer-rejected", html=True)
                    self.telemetry.emit(
                        "offer_rejected_in_stock", event_id=eid, reasons=decision.reasons,
                        offer=asdict(offer), artifact=artifact,
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

            if not self.store.transition(
                eid, {PurchaseState.VERIFYING}, PurchaseState.CHECKOUT_READY, "all policy checks passed"
            ):
                await self._abandon()
                return self._finish_state_changed(eid, "state changed during verification")
            tl.mark("checkout_ready")

            if self.dry_run:
                await self._abandon()
                self._consecutive_failures = 0
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
        except ChallengeDetected as exc:
            await self._abandon()
            artifact = await self.adapter.capture_artifact(f"challenge-{exc.kind.value}")
            ctl = self._finish(eid, PurchaseState.NEEDS_ATTENTION, f"challenge before submission: {exc}")
            self.telemetry.emit("challenge", event_id=eid, challenge=exc.kind.value, artifact=artifact)
            return AttemptOutcome(eid, ctl.state, f"needs attention: {exc}")
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

    def _finish_pre_intent(self, eid: str, reason: str, *, benign: bool) -> AttemptOutcome:
        """Nothing was submitted. Stay ARMED unless failures pile up (selector drift etc.)."""
        if benign:
            self._consecutive_failures = 0
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
