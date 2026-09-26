"""Product watcher: a second, gentle trigger source that does not depend on Discord.

While the purchaser is ARMED, re-read the product page every ``interval_s`` (jittered) on
a dedicated tab. When the page shows an offer that the purchase policy would accept, hand
the coordinator a synthetic ``TriggerEvent`` — the exact same path a Discord alert takes,
so arming, single-purchase, fail-closed policy and intent persistence all still apply.

What this deliberately does *not* do: poll faster than ``WATCH_MIN_INTERVAL_S``, poll while
disarmed or while an attempt is in flight, touch the main tab, retry on challenges, or
change any control state. A challenge on the watch tab is logged and backed off; the
Discord path decides what to do about it if/when a real alert arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable

from .amazon.adapter import PurchaseAdapter
from .config import AppConfig
from .models import AttemptOutcome, ChallengeDetected, OfferSnapshot, PurchaseState, TriggerEvent
from .notify import PRIORITY_HIGH, Notifier
from .policy import evaluate_offer
from .store import StateStore, now_ms
from .telemetry import Telemetry

log = logging.getLogger("ocarina.watch")

WATCH_CHANNEL_ID = "watch"  # synthetic channel id so store dedupe and status rows read clearly
MAX_BACKOFF_S = 600.0
HEARTBEAT_EVERY = 20  # polls between "still alive" log lines (~10 min at the default interval)


def make_watch_event(target_id: str, at_ms: int | None = None) -> TriggerEvent:
    at_ms = at_ms or now_ms()
    return TriggerEvent(
        event_id=f"watch-{at_ms}",
        message_id=str(at_ms),
        channel_id=WATCH_CHANNEL_ID,
        guild_id="",
        matched_target=target_id,
        message_ts_ms=at_ms,
        sent_at_ms=at_ms,
        detected_at_offset_ms=0,
        source="watcher",
    )


class ProductWatcher:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: StateStore,
        adapter: PurchaseAdapter,
        telemetry: Telemetry,
        on_trigger: Callable[[TriggerEvent], Awaitable[AttemptOutcome]],
        is_busy: Callable[[], bool],
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.adapter = adapter
        self.telemetry = telemetry
        self.on_trigger = on_trigger
        self.is_busy = is_busy
        self.notifier = notifier
        self._failures = 0
        self._last_signature: tuple | None = None
        self._cooldown_until = 0.0
        self.polls = 0
        self.triggers = 0

    # ---------------------------------------------------------------- timing
    def _delay(self) -> float:
        base = float(self.config.watch.interval_s)
        if self._failures:
            base = min(MAX_BACKOFF_S, base * (2**self._failures))
        return base * random.uniform(0.8, 1.2)  # jitter so the cadence is not metronomic

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "product watcher on: every ~%ds while ARMED (cooldown %ds after a refused attempt)",
            self.config.watch.interval_s,
            self.config.watch.retrigger_cooldown_s,
        )
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._delay())
            if stop.is_set():
                return
            await self.tick()

    async def tick(self) -> bool:
        """One poll. Returns True when a trigger was handed to the coordinator."""
        ctl = self.store.get_control()
        if ctl.state != PurchaseState.ARMED or not ctl.is_armed(now_ms()) or self.is_busy():
            return False
        if time.monotonic() < self._cooldown_until:
            return False
        try:
            offer = await self.adapter.poll_offer()
        except ChallengeDetected as exc:
            self._failures += 1
            log.warning("watch: challenge on product page (%s); backing off", exc)
            self.telemetry.emit("watch_challenge", challenge=exc.kind.value, failures=self._failures)
            if self._failures == 1 and self.notifier is not None:
                self.notifier.fire(
                    f"Amazon challenge on the watch tab: {exc.kind.value}",
                    "Polling is backing off. Solve it in the browser window; the Discord path is unaffected.",
                    priority=PRIORITY_HIGH,
                    tags="warning",
                )
            return False
        except Exception as exc:  # noqa: BLE001 - the watcher must never die
            self._failures += 1
            log.warning("watch: poll failed (%r); backing off", exc)
            self.telemetry.emit("watch_error", error=repr(exc), failures=self._failures)
            return False
        self._failures = 0
        self.polls += 1
        self._note_change(offer)
        if self.polls % HEARTBEAT_EVERY == 0:
            log.info("watch: alive, %d polls so far; availability=%r", self.polls, offer.availability)

        decision = evaluate_offer(offer, self.config.policy, self.config.target)
        if not decision.ok:
            return False
        event = make_watch_event(self.config.target.target_id)
        self.store.record_event(event)
        self.triggers += 1
        log.info("watch: purchasable offer seen (price=%s); triggering %s", offer.price, event.event_id)
        self.telemetry.emit("watch_trigger", event_id=event.event_id, price=str(offer.price))
        outcome = await self.on_trigger(event)
        if outcome.final_state != PurchaseState.PURCHASED:
            # Refused or failed: do not hammer the same page every interval with a full attempt.
            self._cooldown_until = time.monotonic() + self.config.watch.retrigger_cooldown_s
        return True

    def _note_change(self, offer: OfferSnapshot) -> None:
        sig = (offer.in_stock, offer.is_preorder, offer.availability, str(offer.price), offer.seller)
        if sig != self._last_signature:
            self._last_signature = sig
            log.info(
                "watch: page now in_stock=%s preorder=%s price=%s seller=%s availability=%r",
                offer.in_stock, offer.is_preorder, offer.price, offer.seller, offer.availability,
            )
            self.telemetry.emit(
                "watch_changed",
                in_stock=offer.in_stock,
                is_preorder=offer.is_preorder,
                price=str(offer.price),
                seller=offer.seller,
                availability=offer.availability,
            )
