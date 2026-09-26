"""Always-on runner: process lock, durable state, browser, bridge, coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from .amazon.adapter import AmazonAdapter
from .bridge import TriggerBridge, read_secret
from .config import AppConfig
from .coordinator import PurchaseCoordinator
from .lock import ProcessLock
from .models import PurchaseState
from .store import StateStore, now_ms
from .telemetry import Telemetry
from .watch import ProductWatcher

log = logging.getLogger("ocarina.app")


async def _expiry_watch(store: StateStore, stop: asyncio.Event) -> None:
    """Flip ARMED -> DISARMED once the armed session expires (status clarity only)."""
    while not stop.is_set():
        ctl = store.get_control()
        if (
            ctl.state == PurchaseState.ARMED
            and ctl.armed_until_ms is not None
            and ctl.armed_until_ms <= now_ms()
        ):
            store.disarm("armed session expired")
            log.info("armed session expired; now DISARMED")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


async def run_forever(config: AppConfig, *, dry_run: bool, headless: bool) -> int:
    paths = config.paths
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    secret = read_secret(paths.bridge_secret_file)

    with ProcessLock(paths.lock_path):
        store = StateStore(paths.db_path)
        ctl = store.recover_on_startup()
        log.info("startup state: %s (disabled=%s)", ctl.state.value, ctl.purchase_disabled)
        if dry_run:
            log.warning("DRY-RUN: the purchaser will never click Place Order in this session")
        else:
            log.warning("LIVE MODE: purchases will be submitted when armed and all checks pass")

        telemetry = Telemetry(paths.log_dir, config.telemetry.enabled, config.telemetry.verbose)
        adapter = AmazonAdapter(config, dry_run=dry_run, headless=headless)
        await adapter.start()
        try:
            await adapter.ensure_ready()
        except Exception as exc:  # noqa: BLE001 - readiness is best effort at startup
            log.warning("product tab not ready at startup: %s", exc)

        coordinator = PurchaseCoordinator(
            config=config, store=store, adapter=adapter, telemetry=telemetry, dry_run=dry_run
        )
        bridge = TriggerBridge(
            bridge_cfg=config.bridge,
            target_cfg=config.target,
            secret=secret,
            store=store,
            on_trigger=coordinator.handle_trigger,
            status_provider=coordinator.status,
        )
        await bridge.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        tasks = [asyncio.create_task(_expiry_watch(store, stop))]
        if config.watch.enabled:
            product_watcher = ProductWatcher(
                config=config,
                store=store,
                adapter=adapter,
                telemetry=telemetry,
                on_trigger=coordinator.handle_trigger,
                is_busy=lambda: coordinator.status()["busy"],
            )
            tasks.append(asyncio.create_task(product_watcher.run(stop)))
        log.info(
            "ready. state=%s dry_run=%s bridge=127.0.0.1:%d watch=%s — arm with `ocarina arm`",
            store.get_control().state.value,
            dry_run,
            config.bridge.port,
            f"every {config.watch.interval_s}s" if config.watch.enabled else "off",
        )
        try:
            if sys.platform == "win32":
                # add_signal_handler is unsupported on Windows; a short wait_for loop lets
                # KeyboardInterrupt surface between waits.
                while not stop.is_set():
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
            else:
                await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await bridge.stop()
            await adapter.stop()
            store.close()
    return 0
