import tomllib
from pathlib import Path

import pytest

from amore_ocarina_sniper.config import ConfigError, load_config, load_config_dict

ROOT = Path(__file__).resolve().parents[1]


def _example() -> dict:
    return tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))


def test_example_config_loads_but_is_not_purchase_ready(tmp_path):
    cfg = load_config_dict(_example(), base_dir=tmp_path)
    assert cfg.target.asin == "B0HJ6F8L6V"
    assert cfg.bridge.host == "127.0.0.1"
    problems = cfg.policy.readiness_problems()
    assert any("max_item_price" in p for p in problems)
    assert any("approved_address_contains" in p for p in problems)


def test_ready_policy_has_no_problems(tmp_path):
    raw = _example()
    raw["policy"].update(
        {
            "max_item_price": "750.00",
            "max_total": "860.00",
            "approved_address_contains": "123 Maple",
            "approved_payment_contains": "ending in 4242",
        }
    )
    cfg = load_config_dict(raw, base_dir=tmp_path)
    assert cfg.policy.readiness_problems() == []


def test_allow_preorder_defaults_false_and_parses(tmp_path):
    raw = _example()
    assert load_config_dict(raw, base_dir=tmp_path).policy.allow_preorder is False
    raw["policy"]["allow_preorder"] = True
    assert load_config_dict(raw, base_dir=tmp_path).policy.allow_preorder is True
    raw["policy"]["allow_preorder"] = "yes"
    with pytest.raises(ConfigError):
        load_config_dict(raw, base_dir=tmp_path)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda r: r["policy"].update({"max_item_price": 709.99}), "decimal string"),
        (lambda r: r["policy"].update({"quantity": 2}), "quantity"),
        (lambda r: r["policy"].update({"condition": "used"}), "condition"),
        (lambda r: r["policy"].update({"currency": "USD"}), "currency"),
        (lambda r: r["target"].update({"asin": "SHORT"}), "ASIN"),
        (lambda r: r["target"].update({"url": "https://www.amazon.com/dp/B0HJ6F8L6V"}), "amazon.ca"),
        (lambda r: r["bridge"].update({"port": 80}), "port"),
        (lambda r: r["bridge"].update({"allowed_extension_origin": "https://evil"}), "chrome-extension"),
        (lambda r: r.pop("policy"), "[policy]"),
    ],
)
def test_invalid_configs_rejected(tmp_path, mutate, fragment):
    raw = _example()
    mutate(raw)
    with pytest.raises(ConfigError) as exc:
        if fragment == "quantity":
            cfg = load_config_dict(raw, base_dir=tmp_path)
            assert any("quantity" in p for p in cfg.policy.readiness_problems())
            raise ConfigError("quantity")
        load_config_dict(raw, base_dir=tmp_path)
    assert fragment.lower() in str(exc.value).lower()


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.toml")
    assert "config.example.toml" in str(exc.value)
