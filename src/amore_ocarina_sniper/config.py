"""Configuration and purchase policy loading (TOML, strict, fail closed).

Policy values are never inferred from alerts. Money is Decimal. Missing or malformed
policy fields raise ``ConfigError`` at load time so `run`/`arm` refuse to start.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .matching import DEFAULT_REQUIRED_PHRASES
from .money import decimal_from_config

BRIDGE_BIND_HOST = "127.0.0.1"  # not configurable by design


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path
    browser_profile_dir: Path
    bridge_secret_file: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ocarina.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "ocarina.lock"

    @property
    def kill_switch_path(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@dataclass(frozen=True)
class BridgeConfig:
    port: int
    max_body_bytes: int = 8192
    clock_skew_tolerance_s: int = 30
    message_freshness_s: int = 90
    allowed_extension_origin: str = ""

    @property
    def host(self) -> str:
        return BRIDGE_BIND_HOST


@dataclass(frozen=True)
class TargetConfig:
    asin: str
    url: str
    target_id: str
    title_must_contain: tuple[str, ...]
    required_alert_phrases: tuple[str, ...] = DEFAULT_REQUIRED_PHRASES
    # Optional defence in depth: bridge rejects triggers from any other channel id.
    discord_channel_id: str = ""


@dataclass(frozen=True)
class PurchasePolicy:
    max_item_price: Decimal
    max_total: Decimal
    currency: str
    quantity: int
    condition: str
    allowed_sellers: tuple[str, ...]
    fulfillment: str  # "amazon" | "any"
    approved_address_contains: str
    approved_payment_contains: str
    default_arm_minutes: int
    max_arm_minutes: int
    # Accept listings whose availability announces a release date ("Pre-order now").
    # Off by default: a pre-order is a commitment to buy on release, not an in-stock purchase.
    allow_preorder: bool = False

    def readiness_problems(self) -> list[str]:
        """Reasons this policy can never allow a purchase (reported by `doctor`/`arm`)."""
        problems: list[str] = []
        if self.max_item_price <= 0:
            problems.append("policy.max_item_price must be > 0")
        if self.max_total <= 0:
            problems.append("policy.max_total must be > 0")
        if self.max_total < self.max_item_price:
            problems.append("policy.max_total must be >= policy.max_item_price")
        if not self.allowed_sellers:
            problems.append("policy.allowed_sellers must list at least one seller")
        if not self.approved_address_contains.strip():
            problems.append("policy.approved_address_contains is empty (fails closed)")
        if not self.approved_payment_contains.strip():
            problems.append("policy.approved_payment_contains is empty (fails closed)")
        if self.quantity != 1:
            problems.append("policy.quantity must be exactly 1")
        return problems


@dataclass(frozen=True)
class CheckoutConfig:
    strategy: str = "buy_now"
    navigation_timeout_ms: int = 15000
    element_timeout_ms: int = 8000
    confirmation_timeout_ms: int = 20000
    block_heavy_assets: bool = False


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = True
    verbose: bool = False


@dataclass(frozen=True)
class AppConfig:
    paths: PathsConfig
    bridge: BridgeConfig
    target: TargetConfig
    policy: PurchasePolicy
    checkout: CheckoutConfig = field(default_factory=CheckoutConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    source_path: Path | None = None


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if value is None:
        raise ConfigError(f"missing [{name}] section")
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _req(section: dict, section_name: str, key: str, kind: type):
    if key not in section:
        raise ConfigError(f"[{section_name}].{key} is required")
    value = section[key]
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"[{section_name}].{key} must be an integer")
    if not isinstance(value, kind):
        raise ConfigError(f"[{section_name}].{key} must be {kind.__name__}")
    return value


def _opt(section: dict, section_name: str, key: str, kind: type, default):
    if key not in section:
        return default
    return _req(section, section_name, key, kind)


def _str_tuple(section: dict, section_name: str, key: str, default=None) -> tuple[str, ...]:
    if key not in section:
        if default is None:
            raise ConfigError(f"[{section_name}].{key} is required")
        return tuple(default)
    value = section[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[{section_name}].{key} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def load_config_dict(raw: dict, base_dir: Path, source_path: Path | None = None) -> AppConfig:
    paths_raw = _section(raw, "paths")
    bridge_raw = _section(raw, "bridge")
    target_raw = _section(raw, "target")
    policy_raw = _section(raw, "policy")
    checkout_raw = raw.get("checkout", {}) or {}
    telemetry_raw = raw.get("telemetry", {}) or {}

    def resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (base_dir / path).resolve()

    paths = PathsConfig(
        data_dir=resolve(_opt(paths_raw, "paths", "data_dir", str, "./runtime")),
        browser_profile_dir=resolve(
            _opt(paths_raw, "paths", "browser_profile_dir", str, "./profiles/amazon")
        ),
        bridge_secret_file=resolve(
            _opt(paths_raw, "paths", "bridge_secret_file", str, "./secrets/bridge_secret")
        ),
    )

    port = _req(bridge_raw, "bridge", "port", int)
    if not (1024 <= port <= 65535):
        raise ConfigError("[bridge].port must be between 1024 and 65535")
    origin = _opt(bridge_raw, "bridge", "allowed_extension_origin", str, "").strip()
    if origin and not origin.startswith("chrome-extension://"):
        raise ConfigError("[bridge].allowed_extension_origin must start with chrome-extension://")
    bridge = BridgeConfig(
        port=port,
        max_body_bytes=_opt(bridge_raw, "bridge", "max_body_bytes", int, 8192),
        clock_skew_tolerance_s=_opt(bridge_raw, "bridge", "clock_skew_tolerance_s", int, 30),
        message_freshness_s=_opt(bridge_raw, "bridge", "message_freshness_s", int, 90),
        allowed_extension_origin=origin,
    )
    if bridge.message_freshness_s <= 0:
        raise ConfigError("[bridge].message_freshness_s must be > 0")

    asin = _req(target_raw, "target", "asin", str).strip().upper()
    if len(asin) != 10 or not asin.isalnum():
        raise ConfigError("[target].asin must be a 10-character ASIN")
    url = _req(target_raw, "target", "url", str).strip()
    if not url.startswith("https://www.amazon.ca/"):
        raise ConfigError("[target].url must start with https://www.amazon.ca/")
    if asin not in url:
        raise ConfigError("[target].url must contain the configured ASIN")
    target = TargetConfig(
        asin=asin,
        url=url,
        target_id=_req(target_raw, "target", "target_id", str).strip(),
        title_must_contain=_str_tuple(target_raw, "target", "title_must_contain"),
        required_alert_phrases=_str_tuple(
            target_raw, "target", "required_alert_phrases", DEFAULT_REQUIRED_PHRASES
        ),
        discord_channel_id=_opt(target_raw, "target", "discord_channel_id", str, "").strip(),
    )
    if not target.target_id:
        raise ConfigError("[target].target_id must not be empty")

    fulfillment = _opt(policy_raw, "policy", "fulfillment", str, "amazon").strip().lower()
    if fulfillment not in {"amazon", "any"}:
        raise ConfigError('[policy].fulfillment must be "amazon" or "any"')
    condition = _opt(policy_raw, "policy", "condition", str, "new").strip().lower()
    if condition != "new":
        raise ConfigError('[policy].condition only supports "new"')
    currency = _opt(policy_raw, "policy", "currency", str, "CAD").strip().upper()
    if currency != "CAD":
        raise ConfigError("[policy].currency must be CAD for Amazon.ca")
    try:
        max_item_price = decimal_from_config(
            policy_raw.get("max_item_price", "0.00"), "policy.max_item_price"
        )
        max_total = decimal_from_config(policy_raw.get("max_total", "0.00"), "policy.max_total")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    policy = PurchasePolicy(
        max_item_price=max_item_price,
        max_total=max_total,
        currency=currency,
        quantity=_opt(policy_raw, "policy", "quantity", int, 1),
        condition=condition,
        allowed_sellers=tuple(
            s.lower() for s in _str_tuple(policy_raw, "policy", "allowed_sellers", ["amazon.ca"])
        ),
        fulfillment=fulfillment,
        approved_address_contains=_opt(policy_raw, "policy", "approved_address_contains", str, ""),
        approved_payment_contains=_opt(policy_raw, "policy", "approved_payment_contains", str, ""),
        default_arm_minutes=_opt(policy_raw, "policy", "default_arm_minutes", int, 120),
        max_arm_minutes=_opt(policy_raw, "policy", "max_arm_minutes", int, 720),
        allow_preorder=_opt(policy_raw, "policy", "allow_preorder", bool, False),
    )
    if policy.default_arm_minutes <= 0 or policy.max_arm_minutes <= 0:
        raise ConfigError("[policy] arm minutes must be > 0")
    if policy.default_arm_minutes > policy.max_arm_minutes:
        raise ConfigError("[policy].default_arm_minutes must be <= max_arm_minutes")

    strategy = _opt(checkout_raw, "checkout", "strategy", str, "buy_now").strip().lower()
    if strategy not in {"buy_now", "cart"}:
        raise ConfigError('[checkout].strategy must be "buy_now" or "cart"')
    checkout = CheckoutConfig(
        strategy=strategy,
        navigation_timeout_ms=_opt(checkout_raw, "checkout", "navigation_timeout_ms", int, 15000),
        element_timeout_ms=_opt(checkout_raw, "checkout", "element_timeout_ms", int, 8000),
        confirmation_timeout_ms=_opt(checkout_raw, "checkout", "confirmation_timeout_ms", int, 20000),
        block_heavy_assets=_opt(checkout_raw, "checkout", "block_heavy_assets", bool, False),
    )
    telemetry = TelemetryConfig(
        enabled=_opt(telemetry_raw, "telemetry", "enabled", bool, True),
        verbose=_opt(telemetry_raw, "telemetry", "verbose", bool, False),
    )
    return AppConfig(
        paths=paths,
        bridge=bridge,
        target=target,
        policy=policy,
        checkout=checkout,
        telemetry=telemetry,
        source_path=source_path,
    )


def default_config_path() -> Path:
    env = os.environ.get("OCARINA_CONFIG")
    return Path(env) if env else Path.cwd() / "config.toml"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy config.example.toml to config.toml and edit it."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return load_config_dict(raw, base_dir=path.parent, source_path=path)
