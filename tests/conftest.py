from __future__ import annotations

import http.server
import socket
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from amore_ocarina_sniper.config import (
    AppConfig,
    BridgeConfig,
    CheckoutConfig,
    PathsConfig,
    PurchasePolicy,
    TargetConfig,
    TelemetryConfig,
)
from amore_ocarina_sniper.store import StateStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXTENSION = ROOT / "extension"

TARGET_ASIN = "B0HJ6F8L6V"
TARGET_URL = f"https://www.amazon.ca/dp/{TARGET_ASIN}"
TARGET_ID = "switch2-zelda-40th"
CHANNEL_ID = "111111111111111111"
GUILD_ID = "222222222222222222"


def make_config(tmp_path: Path, **policy_overrides) -> AppConfig:
    policy_kwargs = dict(
        max_item_price=Decimal("750.00"),
        max_total=Decimal("860.00"),
        currency="CAD",
        quantity=1,
        condition="new",
        allowed_sellers=("amazon.ca",),
        fulfillment="amazon",
        approved_address_contains="123 Maple Street",
        approved_payment_contains="ending in 4242",
        default_arm_minutes=60,
        max_arm_minutes=720,
    )
    policy_kwargs.update(policy_overrides)
    return AppConfig(
        paths=PathsConfig(
            data_dir=tmp_path / "runtime",
            browser_profile_dir=tmp_path / "profile",
            bridge_secret_file=tmp_path / "secrets" / "bridge_secret",
        ),
        bridge=BridgeConfig(port=0, message_freshness_s=90, clock_skew_tolerance_s=30),
        target=TargetConfig(
            asin=TARGET_ASIN,
            url=TARGET_URL,
            target_id=TARGET_ID,
            title_must_contain=("nintendo switch 2", "legend of zelda", "40th anniversary"),
            discord_channel_id="",
        ),
        policy=PurchasePolicy(**policy_kwargs),
        checkout=CheckoutConfig(
            navigation_timeout_ms=5000, element_timeout_ms=2000, confirmation_timeout_ms=1500
        ),
        telemetry=TelemetryConfig(enabled=False),
    )


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    cfg = make_config(tmp_path)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def store(config: AppConfig):
    s = StateStore(config.paths.db_path)
    yield s
    s.close()


# ------------------------------------------------------------ fixture server
class _FixtureHandler(http.server.SimpleHTTPRequestHandler):
    """Serves tests/fixtures; any /channels/<g>/<c> path returns the Discord fixture."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def translate_path(self, path: str) -> str:
        if path.startswith("/channels/"):
            return str(FIXTURES / "discord" / "channel.html")
        if path.startswith("/extension/"):
            return str(EXTENSION / path[len("/extension/") :])
        return super().translate_path(path)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture(scope="session")
def fixture_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _FixtureHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
async def browser():
    # Function-scoped: pytest-asyncio runs each test in its own loop by default and a
    # session-scoped Playwright object would be bound to a closed loop.
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    b = await pw.chromium.launch(headless=True)
    yield b
    await b.close()
    await pw.stop()
