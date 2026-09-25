import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from amore_ocarina_sniper.bridge import (
    RejectedTrigger,
    TriggerBridge,
    generate_secret,
    synthetic_payload,
    validate_trigger_payload,
)
from amore_ocarina_sniper.models import TriggerEvent

from .conftest import CHANNEL_ID, GUILD_ID, TARGET_ID

SECRET = generate_secret()


def payload(**over):
    now = int(time.time() * 1000)
    base = {
        "schema": 1,
        "event_id": f"{CHANNEL_ID}-1234567890",
        "message_id": "1234567890",
        "channel_id": CHANNEL_ID,
        "guild_id": GUILD_ID,
        "matched_target": TARGET_ID,
        "message_ts_ms": now - 2000,
        "sent_at_ms": now,
        "detected_at_offset_ms": 12,
        "attempt": 1,
        "source": "extension",
    }
    base.update(over)
    return base


@pytest.fixture
async def client(config, store):
    received: list[TriggerEvent] = []

    async def on_trigger(event: TriggerEvent) -> None:
        received.append(event)

    bridge = TriggerBridge(
        bridge_cfg=config.bridge,
        target_cfg=config.target,
        secret=SECRET,
        store=store,
        on_trigger=on_trigger,
        status_provider=lambda: {"state": "DISARMED", "dry_run": True},
    )
    server = TestServer(bridge.app, host="127.0.0.1")
    async with TestClient(server) as c:
        c.received = received  # type: ignore[attr-defined]
        yield c


def auth(origin: str | None = "chrome-extension://abcdefghijklmnopabcdefghijklmnop", secret=SECRET):
    h = {"Authorization": f"Bearer {secret}"}
    if origin:
        h["Origin"] = origin
    return h


async def test_accepts_valid_trigger_and_dispatches(client):
    r = await client.post("/v1/trigger", json=payload(), headers=auth())
    assert r.status == 200
    body = await r.json()
    assert body == {"ack": True, "event_id": f"{CHANNEL_ID}-1234567890", "disposition": "accepted"}
    await asyncio.sleep(0.05)
    assert len(client.received) == 1
    assert client.received[0].message_id == "1234567890"


async def test_retry_same_event_is_acked_as_duplicate_without_redispatch(client):
    await client.post("/v1/trigger", json=payload(), headers=auth())
    r = await client.post("/v1/trigger", json=payload(attempt=2), headers=auth())
    assert r.status == 200
    assert (await r.json())["disposition"] == "duplicate_event"
    r = await client.post("/v1/trigger", json=payload(event_id="different-id-0001"), headers=auth())
    assert (await r.json())["disposition"] == "duplicate_message"
    await asyncio.sleep(0.05)
    assert len(client.received) == 1


async def test_rejects_missing_or_wrong_secret(client):
    r = await client.post("/v1/trigger", json=payload())
    assert r.status == 401
    r = await client.post("/v1/trigger", json=payload(), headers=auth(secret="x" * 43))
    assert r.status == 401
    assert (await r.json())["terminal"] is True
    assert client.received == []


async def test_rejects_non_extension_origin(client):
    r = await client.post("/v1/trigger", json=payload(), headers=auth(origin="https://discord.com"))
    assert r.status == 403
    r = await client.post("/v1/trigger", json=payload(), headers=auth(origin="http://127.0.0.1:1"))
    assert r.status == 403


async def test_rejects_non_loopback_host(client):
    h = auth()
    h["Host"] = "evil.example:80"
    r = await client.post("/v1/trigger", json=payload(), headers=h)
    assert r.status == 403


async def test_rejects_oversize_and_bad_json(client):
    big = payload(matched_target="x" * 9000)
    r = await client.post("/v1/trigger", json=big, headers=auth())
    assert r.status == 413
    r = await client.post("/v1/trigger", data=b"{not json", headers=auth())
    assert r.status == 400


async def test_rejects_stale_and_skewed(client):
    r = await client.post("/v1/trigger", json=payload(message_ts_ms=int(time.time() * 1000) - 120_000), headers=auth())
    assert r.status == 410 and (await r.json())["code"] == "stale"
    r = await client.post("/v1/trigger", json=payload(sent_at_ms=int(time.time() * 1000) - 60_000), headers=auth())
    assert r.status == 410 and (await r.json())["code"] == "expired"
    assert client.received == []


async def test_rejects_wrong_target(client):
    r = await client.post("/v1/trigger", json=payload(matched_target="other"), headers=auth())
    assert r.status == 400 and (await r.json())["code"] == "target"


async def test_status_requires_auth(client):
    r = await client.get("/v1/status")
    assert r.status == 401
    r = await client.get("/v1/status", headers=auth())
    assert r.status == 200 and (await r.json())["state"] == "DISARMED"
    r = await client.get("/v1/health")
    assert r.status == 200


async def test_no_cors_wildcard(client):
    r = await client.post("/v1/trigger", json=payload(), headers=auth())
    assert "Access-Control-Allow-Origin" not in r.headers


def test_validate_schema_edge_cases(config):
    now = int(time.time() * 1000)
    kw = dict(now_ms=now, bridge_cfg=config.bridge, target_cfg=config.target)
    validate_trigger_payload(payload(), **kw)
    for bad in [
        payload(schema=2),
        payload(event_id="short"),
        payload(message_id="abc"),
        payload(attempt=0),
        payload(source="bot"),
        payload(message_ts_ms=now + 120_000),
        "not a dict",
    ]:
        with pytest.raises(RejectedTrigger):
            validate_trigger_payload(bad, **kw)


def test_channel_pin(config):
    from dataclasses import replace

    target = replace(config.target, discord_channel_id="999")
    now = int(time.time() * 1000)
    with pytest.raises(RejectedTrigger) as exc:
        validate_trigger_payload(payload(), now_ms=now, bridge_cfg=config.bridge, target_cfg=target)
    assert exc.value.status == 403


def test_synthetic_payload_validates(config):
    p = synthetic_payload(config.target.target_id)
    ev = validate_trigger_payload(
        p, now_ms=int(time.time() * 1000), bridge_cfg=config.bridge, target_cfg=config.target
    )
    assert ev.source == "synthetic"


async def test_bridge_binds_loopback_only(config, store):
    """The bind host is a constant, not configuration; the real server binds it."""
    import socket

    from amore_ocarina_sniper.config import BRIDGE_BIND_HOST, BridgeConfig

    assert BRIDGE_BIND_HOST == "127.0.0.1"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    bridge = TriggerBridge(
        bridge_cfg=BridgeConfig(port=port),
        target_cfg=config.target,
        secret=SECRET,
        store=store,
        on_trigger=lambda e: asyncio.sleep(0),
        status_provider=dict,
    )
    await bridge.start()
    try:
        sockets = [sock for site in bridge._runner.sites for sock in site._server.sockets]
        assert sockets and all(sock.getsockname()[0] == "127.0.0.1" for sock in sockets)
    finally:
        await bridge.stop()
