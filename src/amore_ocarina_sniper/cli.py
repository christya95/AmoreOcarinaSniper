"""`ocarina` command line. Control commands write the SQLite state directly; they are
deliberately separate from the trigger bridge so a trigger can never arm or reset."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__, killswitch
from .amazon import selectors as S
from .bridge import generate_secret, post_trigger, read_secret, synthetic_payload, write_secret
from .config import AppConfig, ConfigError, load_config
from .models import PurchaseState
from .store import StateError, StateStore, now_ms


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def _load(args) -> AppConfig:
    try:
        return load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _store(cfg: AppConfig) -> StateStore:
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    return StateStore(cfg.paths.db_path)


# ------------------------------------------------------------------ commands
def cmd_pair(args) -> int:
    cfg = _load(args)
    path = cfg.paths.bridge_secret_file
    if path.exists() and not args.rotate:
        print(f"pairing secret already exists at {path} (use --rotate to replace)")
        secret = read_secret(path)
    else:
        secret = generate_secret()
        write_secret(path, secret)
        print(f"wrote new pairing secret to {path}")
    print("\nExtension options -> Bridge port:", cfg.bridge.port)
    print("Extension options -> Pairing secret:", secret)
    print("Extension options -> Target id:", cfg.target.target_id)
    print("\nThe secret is stored outside version control; never paste it into Discord.")
    return 0


def cmd_setup(args) -> int:
    cfg = _load(args)
    from .amazon.adapter import AmazonAdapter

    async def go() -> int:
        adapter = AmazonAdapter(cfg, dry_run=True, headless=False)
        await adapter.start()
        try:
            print("A Chromium window opened with the dedicated profile.")
            print("Sign in to Amazon.ca yourself (including any MFA prompt), then verify a saved")
            print("shipping address and payment method exist in Your Account. Nothing is typed")
            print("or stored by this tool. Waiting up to 15 minutes...")
            ok = await adapter.interactive_login(timeout_s=args.timeout)
            print("signed in: detected" if ok else "sign-in not detected before timeout")
            return 0 if ok else 1
        finally:
            await adapter.stop()

    return asyncio.run(go())


def cmd_doctor(args) -> int:
    cfg = _load(args)
    problems: list[str] = []
    print(f"config: {cfg.source_path}")
    print(f"data_dir: {cfg.paths.data_dir}")
    print(f"target: {cfg.target.asin} {cfg.target.url}")
    print(f"bridge: 127.0.0.1:{cfg.bridge.port} freshness={cfg.bridge.message_freshness_s}s")
    if not cfg.paths.bridge_secret_file.exists():
        problems.append("pairing secret missing: run `ocarina pair`")
    for p in cfg.policy.readiness_problems():
        problems.append(p)
    from .amazon.adapter import profile_exists

    if not profile_exists(cfg):
        problems.append("browser profile missing: run `ocarina setup`")
    store = _store(cfg)
    ctl = store.get_control()
    print(
        f"state: {ctl.state.value} armed_until={_fmt_ms(ctl.armed_until_ms)}"
        f" disabled={ctl.purchase_disabled} ({ctl.disabled_reason or '-'})"
    )
    print(f"kill switch: {'ENGAGED' if killswitch.is_engaged(cfg.paths.kill_switch_path) else 'clear'}")
    store.close()
    print("\nselector verification:")
    for page, field, status in S.verification_report():
        print(f"  {page:9s} {field:14s} {status}")
    if args.browser:
        from .amazon.adapter import AmazonAdapter

        async def probe() -> None:
            adapter = AmazonAdapter(cfg, dry_run=True, headless=args.headless)
            await adapter.start()
            try:
                status = await adapter.session_status()
                print(
                    f"\nbrowser: url={status['url']} challenge={status['challenge']}"
                    f" signed_in={status['signed_in']}"
                )
                offer = await adapter.verify_offer()
                print(
                    "offer:",
                    json.dumps({k: str(v) for k, v in offer.__dict__.items() if k != "raw"}, indent=2),
                )
                from .policy import evaluate_offer

                decision = evaluate_offer(offer, cfg.policy, cfg.target)
                print("offer policy:", "OK" if decision.ok else "; ".join(decision.reasons))
                if args.checkout_probe and decision.ok:
                    print("probing checkout surface (dry-run; nothing is submitted)...")
                    snap = await adapter.prepare_checkout(offer)
                    print(
                        "checkout:",
                        json.dumps({k: str(v) for k, v in snap.__dict__.items() if k != "raw"}, indent=2),
                    )
                    from .policy import evaluate_checkout

                    d2 = evaluate_checkout(snap, cfg.policy, cfg.target)
                    print("checkout policy:", "OK" if d2.ok else "; ".join(d2.reasons))
                    await adapter.abandon()
            finally:
                await adapter.stop()

        try:
            asyncio.run(probe())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"browser probe failed: {exc}")
    print()
    if problems:
        print("NOT READY:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("READY (policy configured, secret present, profile present)")
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    from .app import run_forever

    dry_run = not args.live
    # The runner is the unattended, always-on process: mirror its console log to a file so
    # an overnight attempt can be reviewed regardless of how the terminal was launched.
    log_path = cfg.paths.log_dir / "runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger("ocarina.cli").info(
        "runner log: %s (mode=%s)", log_path, "LIVE" if args.live else "dry-run"
    )
    try:
        return asyncio.run(run_forever(cfg, dry_run=dry_run, headless=args.headless))
    except KeyboardInterrupt:
        return 0


def cmd_arm(args) -> int:
    cfg = _load(args)
    problems = cfg.policy.readiness_problems()
    if problems:
        print("refusing to arm; policy is not purchase-ready:")
        for p in problems:
            print(f"  - {p}")
        return 1
    minutes = args.minutes or cfg.policy.default_arm_minutes
    if minutes > cfg.policy.max_arm_minutes:
        print(f"refusing to arm for {minutes} min (max_arm_minutes={cfg.policy.max_arm_minutes})")
        return 1
    if killswitch.is_engaged(cfg.paths.kill_switch_path):
        print("kill switch is engaged; run `ocarina reset --confirm` first")
        return 1
    store = _store(cfg)
    try:
        ctl = store.arm(now_ms() + minutes * 60_000)
    except StateError as exc:
        print(f"cannot arm: {exc}")
        return 1
    finally:
        store.close()
    print(f"ARMED until {_fmt_ms(ctl.armed_until_ms)} ({minutes} min).")
    print(
        "Note: the runner must be started with --live for a purchase to be submitted;"
        " a dry-run runner will verify and stop before Place Order."
    )
    return 0


def cmd_disarm(args) -> int:
    cfg = _load(args)
    store = _store(cfg)
    ctl = store.disarm()
    store.close()
    print(f"state: {ctl.state.value}")
    return 0


def cmd_kill(args) -> int:
    cfg = _load(args)
    killswitch.engage(cfg.paths.kill_switch_path)
    store = _store(cfg)
    store.disarm("kill switch engaged")
    store.close()
    print(f"kill switch engaged at {cfg.paths.kill_switch_path}; state DISARMED.")
    print("This prevents the final click. It cannot retract a request already sent to Amazon.")
    return 0


def cmd_status(args) -> int:
    cfg = _load(args)
    store = _store(cfg)
    ctl = store.get_control()
    now = now_ms()
    print(f"state:            {ctl.state.value}")
    print(f"armed:            {ctl.is_armed(now)} (until {_fmt_ms(ctl.armed_until_ms)})")
    print(f"purchase_disabled:{ctl.purchase_disabled} {ctl.disabled_reason or ''}")
    print(f"kill switch:      {'ENGAGED' if killswitch.is_engaged(cfg.paths.kill_switch_path) else 'clear'}")
    last = store.last_attempt()
    if last:
        print(
            f"last attempt:     event={last.event_id} final={last.final_state} "
            f"reason={last.reason} order={last.order_id or '-'} dry_run={last.dry_run}"
        )
    print("recent events:")
    for ev in store.recent_events(args.limit):
        print(
            f"  {_fmt_ms(ev['received_at_ms'])} {ev['event_id']} ch={ev['channel_id']}"
            f" msg={ev['message_id']} -> {ev['disposition']}"
        )
    if args.transitions:
        print("transitions:")
        for at, frm, to, eid, reason in store.transitions(args.limit):
            print(f"  {_fmt_ms(at)} {frm or '-':>16} -> {to:<16} {eid or ''} {reason}")
    store.close()
    return 0


def cmd_trigger(args) -> int:
    cfg = _load(args)
    secret = read_secret(cfg.paths.bridge_secret_file)
    payload = synthetic_payload(
        cfg.target.target_id,
        channel_id=cfg.target.discord_channel_id or args.channel_id or "",
        guild_id=args.guild_id or "",
    )
    if args.stale_seconds:
        payload["message_ts_ms"] -= args.stale_seconds * 1000
    status, body = asyncio.run(post_trigger(port=cfg.bridge.port, secret=secret, payload=payload))
    print(f"HTTP {status}: {json.dumps(body)}")
    return 0 if status == 200 else 1


def cmd_reset(args) -> int:
    cfg = _load(args)
    if not args.confirm:
        print("reset clears PURCHASED/UNKNOWN/NEEDS_ATTENTION, re-enables purchasing (DISARMED),")
        print("and removes the kill switch. Re-run with --confirm after reconciling order history.")
        return 1
    store = _store(cfg)
    try:
        ctl = store.reset()
    except StateError as exc:
        print(f"cannot reset: {exc}")
        return 1
    finally:
        store.close()
    cleared = killswitch.clear(cfg.paths.kill_switch_path)
    print(f"state: {ctl.state.value}; kill switch {'removed' if cleared else 'was clear'}")
    return 0


def cmd_reconcile(args) -> int:
    cfg = _load(args)
    from .amazon.adapter import AmazonAdapter
    from .amazon.extract import find_order_id
    from .matching import normalize_text

    async def go() -> int:
        adapter = AmazonAdapter(cfg, dry_run=True, headless=args.headless)
        await adapter.start()
        try:
            text = await adapter.recent_orders_text()
        finally:
            await adapter.stop()
        norm = normalize_text(text)
        hit = all(normalize_text(f) in norm for f in cfg.target.title_must_contain)
        order_id = find_order_id(text)
        print(f"order history mentions target title: {hit}; first order id seen: {order_id or '-'}")
        print("Review the order history page yourself before running `ocarina reset --confirm`.")
        return 0

    return asyncio.run(go())


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ocarina", description="AmoreOcarinaSniper control")
    p.add_argument("--config", help="path to config.toml (default ./config.toml or $OCARINA_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pair", help="generate/print the extension pairing secret")
    s.add_argument("--rotate", action="store_true")
    s.set_defaults(fn=cmd_pair)

    s = sub.add_parser("setup", help="open the dedicated browser profile for manual Amazon login")
    s.add_argument("--timeout", type=int, default=900)
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("doctor", help="readiness report")
    s.add_argument("--browser", action="store_true", help="also open the browser and read the offer")
    s.add_argument(
        "--checkout-probe",
        action="store_true",
        help="with --browser: open the checkout surface in dry-run and report fields",
    )
    s.add_argument("--headless", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("run", help="start the always-on runner (dry-run unless --live)")
    s.add_argument("--live", action="store_true", help="allow real submission when armed")
    s.add_argument("--headless", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("arm", help="arm purchasing for a bounded time")
    s.add_argument("--minutes", type=int)
    s.set_defaults(fn=cmd_arm)

    s = sub.add_parser("disarm", help="disarm purchasing")
    s.set_defaults(fn=cmd_disarm)

    s = sub.add_parser("kill", help="engage the kill switch and disarm")
    s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("status", help="show state, last attempt, recent events")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--transitions", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("trigger", help="send a synthetic trigger to the running bridge")
    s.add_argument("--channel-id", default="")
    s.add_argument("--guild-id", default="")
    s.add_argument("--stale-seconds", type=int, default=0, help="age the message timestamp")
    s.set_defaults(fn=cmd_trigger)

    s = sub.add_parser("reset", help="explicitly clear sticky states and re-enable purchasing")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("reconcile", help="open order history (read-only) to help resolve UNKNOWN")
    s.add_argument("--headless", action="store_true")
    s.set_defaults(fn=cmd_reconcile)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.fn(args) or 0)


__all__ = ["main", "build_parser", "PurchaseState"]
