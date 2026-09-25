"""Authenticated localhost trigger bridge (aiohttp).

Binds 127.0.0.1 only. Every request must carry the pairing secret. Origin (when
present) must be a chrome-extension:// origin; Host must be loopback. Payloads are
size-limited, schema-checked, freshness-checked, and deduplicated durably before the
acknowledgment is sent. 4xx responses are terminal (the extension must not retry);
5xx/network errors are retried by the extension with the same event id.

The bridge is a *trigger* surface only: it cannot arm, disarm, or change policy.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import BridgeConfig, TargetConfig
from .models import TriggerEvent
from .store import StateStore

log = logging.getLogger("ocarina.bridge")

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^(?:\d{5,25}|synthetic-[A-Za-z0-9_-]{1,64})$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}

TriggerCallback = Callable[[TriggerEvent], Awaitable[None]]
StatusProvider = Callable[[], dict[str, Any]]


class RejectedTrigger(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}".rstrip(": "))
        self.status = status
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------- secrets
def generate_secret() -> str:
    return secrets.token_urlsafe(32)


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")


def read_secret(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"pairing secret not found at {path}; run `ocarina pair` and configure the extension"
        )
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise ValueError("pairing secret is too short; regenerate with `ocarina pair`")
    return value


# ---------------------------------------------------------------- validation
def validate_trigger_payload(
    payload: Any,
    *,
    now_ms: int,
    bridge_cfg: BridgeConfig,
    target_cfg: TargetConfig,
) -> TriggerEvent:
    if not isinstance(payload, dict):
        raise RejectedTrigger(400, "schema", "body must be a JSON object")
    if payload.get("schema") != SCHEMA_VERSION:
        raise RejectedTrigger(400, "schema", "unsupported schema version")

    def str_field(name: str, pattern: re.Pattern[str] | None = None) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise RejectedTrigger(400, "schema", f"{name} must be a non-empty string")
        if pattern and not pattern.match(value):
            raise RejectedTrigger(400, "schema", f"{name} has an invalid format")
        return value

    def int_field(name: str, lo: int = 0, hi: int = 1 << 53) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
            raise RejectedTrigger(400, "schema", f"{name} must be an integer in range")
        return value

    event_id = str_field("event_id", _EVENT_ID_RE)
    message_id = str_field("message_id", _ID_RE)
    channel_id = str_field("channel_id", _ID_RE)
    guild_id = str_field("guild_id", _ID_RE)
    matched_target = str_field("matched_target")
    message_ts_ms = int_field("message_ts_ms", lo=1_400_000_000_000)
    sent_at_ms = int_field("sent_at_ms", lo=1_400_000_000_000)
    detected_at_offset_ms = int_field("detected_at_offset_ms", lo=0, hi=3_600_000)
    attempt = int_field("attempt", lo=1, hi=50)
    source = payload.get("source", "extension")
    if source not in {"extension", "synthetic"}:
        raise RejectedTrigger(400, "schema", "source must be extension or synthetic")

    if matched_target != target_cfg.target_id:
        raise RejectedTrigger(400, "target", "matched_target does not match configuration")
    if target_cfg.discord_channel_id and channel_id != target_cfg.discord_channel_id:
        raise RejectedTrigger(403, "channel", "channel_id is not the configured channel")

    skew_ms = bridge_cfg.clock_skew_tolerance_s * 1000
    if abs(now_ms - sent_at_ms) > skew_ms:
        raise RejectedTrigger(410, "expired", "sent_at outside clock skew tolerance")
    age_ms = now_ms - message_ts_ms
    if age_ms > bridge_cfg.message_freshness_s * 1000:
        raise RejectedTrigger(410, "stale", f"message is {age_ms} ms old")
    if age_ms < -skew_ms:
        raise RejectedTrigger(400, "schema", "message timestamp is in the future")

    return TriggerEvent(
        event_id=event_id,
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id,
        matched_target=matched_target,
        message_ts_ms=message_ts_ms,
        sent_at_ms=sent_at_ms,
        detected_at_offset_ms=detected_at_offset_ms,
        attempt=attempt,
        source=source,
    )


# ------------------------------------------------------------------ server
class TriggerBridge:
    def __init__(
        self,
        *,
        bridge_cfg: BridgeConfig,
        target_cfg: TargetConfig,
        secret: str,
        store: StateStore,
        on_trigger: TriggerCallback,
        status_provider: StatusProvider,
    ) -> None:
        self.cfg = bridge_cfg
        self.target = target_cfg
        self._secret = secret
        self.store = store
        self._on_trigger = on_trigger
        self._status_provider = status_provider
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.app = self._build_app()

    # ----------------------------------------------------------- lifecycle
    def _build_app(self) -> web.Application:
        app = web.Application(client_max_size=self.cfg.max_body_bytes)
        app.router.add_post("/v1/trigger", self.handle_trigger)
        app.router.add_get("/v1/status", self.handle_status)
        app.router.add_get("/v1/health", self.handle_health)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.cfg.host, self.cfg.port, reuse_address=True)
        await self._site.start()
        log.info("bridge listening on http://%s:%d", self.cfg.host, self.cfg.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ---------------------------------------------------------- guards
    def _check_auth(self, request: web.Request) -> None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise RejectedTrigger(401, "auth", "missing bearer token")
        presented = header[len("Bearer ") :].strip()
        if not hmac.compare_digest(presented.encode(), self._secret.encode()):
            raise RejectedTrigger(401, "auth", "invalid pairing secret")

    def _check_origin_host(self, request: web.Request) -> None:
        host = request.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
        if hostname not in _LOOPBACK_HOSTS:
            raise RejectedTrigger(403, "host", "non-loopback Host header")
        origin = request.headers.get("Origin")
        if origin is not None:
            if self.cfg.allowed_extension_origin:
                if origin != self.cfg.allowed_extension_origin:
                    raise RejectedTrigger(403, "origin", "origin not allowed")
            elif not origin.startswith("chrome-extension://"):
                raise RejectedTrigger(403, "origin", "origin not allowed")

    @staticmethod
    def _error(exc: RejectedTrigger, event_id: str | None = None) -> web.Response:
        body = {"ack": False, "terminal": True, "code": exc.code, "detail": exc.detail}
        if event_id:
            body["event_id"] = event_id
        return web.json_response(body, status=exc.status)

    # ---------------------------------------------------------- handlers
    async def handle_health(self, request: web.Request) -> web.Response:
        # Unauthenticated liveness only; reveals nothing but that a bridge is here.
        return web.json_response({"ok": True, "service": "ocarina-bridge"})

    async def handle_status(self, request: web.Request) -> web.Response:
        try:
            self._check_origin_host(request)
            self._check_auth(request)
        except RejectedTrigger as exc:
            return self._error(exc)
        return web.json_response(self._status_provider())

    async def handle_trigger(self, request: web.Request) -> web.Response:
        received_ms = int(time.time() * 1000)
        payload: Any = None
        try:
            self._check_origin_host(request)
            self._check_auth(request)
            if request.content_length is not None and request.content_length > self.cfg.max_body_bytes:
                raise RejectedTrigger(413, "size", "payload too large")
            raw = await request.content.read(self.cfg.max_body_bytes + 1)
            if len(raw) > self.cfg.max_body_bytes:
                raise RejectedTrigger(413, "size", "payload too large")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RejectedTrigger(400, "schema", "invalid JSON") from exc
            event = validate_trigger_payload(
                payload, now_ms=received_ms, bridge_cfg=self.cfg, target_cfg=self.target
            )
        except RejectedTrigger as exc:
            event_id = payload.get("event_id") if isinstance(payload, dict) else None
            log.info("trigger rejected %s: %s", exc.code, exc.detail)
            return self._error(exc, event_id if isinstance(event_id, str) else None)

        disposition = self.store.record_event(event, received_at_ms=received_ms)
        if disposition != "new":
            return web.json_response({"ack": True, "event_id": event.event_id, "disposition": disposition})
        # Acknowledge as soon as the event is durable; processing is off the response path.
        asyncio.get_running_loop().create_task(self._dispatch(event, received_ms))
        return web.json_response({"ack": True, "event_id": event.event_id, "disposition": "accepted"})

    async def _dispatch(self, event: TriggerEvent, received_ms: int) -> None:
        try:
            await self._on_trigger(event)
        except Exception:  # pragma: no cover - defensive; coordinator handles its own errors
            log.exception("trigger dispatch failed for %s", event.event_id)


# ------------------------------------------------------------------ client
async def post_trigger(
    *,
    port: int,
    secret: str,
    payload: dict[str, Any],
    timeout_s: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    """Used by the synthetic `trigger` CLI command."""
    import aiohttp

    url = f"http://127.0.0.1:{port}/v1/trigger"
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers={"Authorization": f"Bearer {secret}"}) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = {"raw": await resp.text()}
            return resp.status, body


def synthetic_payload(target_id: str, *, channel_id: str = "", guild_id: str = "") -> dict[str, Any]:
    now = int(time.time() * 1000)
    suffix = secrets.token_hex(6)
    return {
        "schema": SCHEMA_VERSION,
        "event_id": f"synthetic-{suffix}",
        "message_id": f"synthetic-{suffix}",
        "channel_id": channel_id or f"synthetic-{suffix}",
        "guild_id": guild_id or f"synthetic-{suffix}",
        "matched_target": target_id,
        "message_ts_ms": now,
        "sent_at_ms": now,
        "detected_at_offset_ms": 0,
        "attempt": 1,
        "source": "synthetic",
    }
