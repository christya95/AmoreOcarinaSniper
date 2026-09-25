"""CLI smoke tests: control commands operate on SQLite directly, never via the bridge."""

from __future__ import annotations

import asyncio
import socket
import tomllib
from pathlib import Path

import pytest

from amore_ocarina_sniper import cli
from amore_ocarina_sniper.bridge import TriggerBridge, read_secret
from amore_ocarina_sniper.config import load_config
from amore_ocarina_sniper.models import PurchaseState
from amore_ocarina_sniper.store import StateStore

ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, *, ready: bool) -> Path:
    raw = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        raw["bridge"]["port"] = s.getsockname()[1]
    if ready:
        raw["policy"].update(
            {
                "max_item_price": "750.00",
                "max_total": "860.00",
                "approved_address_contains": "123 Maple",
                "approved_payment_contains": "ending in 4242",
            }
        )
    lines = []
    for section, values in raw.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            elif isinstance(v, list):
                lines.append(f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]")
            else:
                lines.append(f'{k} = "{v}"')
        lines.append("")
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(*args) -> int:
    return cli.main([str(a) for a in args])


def test_pair_writes_secret_outside_source(tmp_path, capsys):
    cfg_path = write_config(tmp_path, ready=False)
    assert run("--config", cfg_path, "pair") == 0
    cfg = load_config(cfg_path)
    secret = read_secret(cfg.paths.bridge_secret_file)
    assert len(secret) >= 32
    assert cfg.paths.bridge_secret_file.is_relative_to(tmp_path)
    # Second call does not rotate silently.
    run("--config", cfg_path, "pair")
    assert read_secret(cfg.paths.bridge_secret_file) == secret
    run("--config", cfg_path, "pair", "--rotate")
    assert read_secret(cfg.paths.bridge_secret_file) != secret


def test_arm_refuses_unready_policy(tmp_path, capsys):
    cfg_path = write_config(tmp_path, ready=False)
    assert run("--config", cfg_path, "arm") == 1
    assert "max_item_price" in capsys.readouterr().out
    assert run("--config", cfg_path, "doctor") == 1


def test_arm_disarm_status_kill_reset_cycle(tmp_path, capsys):
    cfg_path = write_config(tmp_path, ready=True)
    cfg = load_config(cfg_path)
    assert run("--config", cfg_path, "arm", "--minutes", "5") == 0
    assert run("--config", cfg_path, "arm", "--minutes", "100000") == 1  # above max
    store = StateStore(cfg.paths.db_path)
    assert store.get_control().state == PurchaseState.ARMED
    store.close()
    assert run("--config", cfg_path, "status", "--transitions") == 0
    out = capsys.readouterr().out
    assert "ARMED" in out
    assert run("--config", cfg_path, "kill") == 0
    assert cfg.paths.kill_switch_path.exists()
    assert run("--config", cfg_path, "arm") == 1  # kill switch blocks arming
    assert run("--config", cfg_path, "reset") == 1  # needs --confirm
    assert run("--config", cfg_path, "reset", "--confirm") == 0
    assert not cfg.paths.kill_switch_path.exists()
    assert run("--config", cfg_path, "disarm") == 0


def test_synthetic_trigger_reaches_bridge(tmp_path, capsys):
    cfg_path = write_config(tmp_path, ready=True)
    run("--config", cfg_path, "pair")
    cfg = load_config(cfg_path)
    secret = read_secret(cfg.paths.bridge_secret_file)
    received = []

    async def go():
        store = StateStore(cfg.paths.db_path)

        async def on_trigger(ev):
            received.append(ev)

        bridge = TriggerBridge(
            bridge_cfg=cfg.bridge, target_cfg=cfg.target, secret=secret, store=store,
            on_trigger=on_trigger, status_provider=dict,
        )
        await bridge.start()
        try:
            code = await asyncio.get_running_loop().run_in_executor(
                None, lambda: run("--config", cfg_path, "trigger")
            )
            assert code == 0
            stale = await asyncio.get_running_loop().run_in_executor(
                None, lambda: run("--config", cfg_path, "trigger", "--stale-seconds", "600")
            )
            assert stale == 1
            await asyncio.sleep(0.05)
        finally:
            await bridge.stop()
            store.close()

    asyncio.run(go())
    assert len(received) == 1 and received[0].source == "synthetic"
    out = capsys.readouterr().out
    assert "HTTP 200" in out and "HTTP 410" in out


def test_trigger_without_bridge_fails_cleanly(tmp_path):
    cfg_path = write_config(tmp_path, ready=True)
    run("--config", cfg_path, "pair")
    import aiohttp

    with pytest.raises(aiohttp.ClientConnectorError):
        run("--config", cfg_path, "trigger")
