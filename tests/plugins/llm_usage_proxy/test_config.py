"""Usage-proxy config defaults (default-on), explicit opt-outs, and coercions."""

from __future__ import annotations

import yaml

from plugins.llm_usage_proxy.config import (
    load_llm_usage_proxy_config,
    manage_keys_enabled,
    plugin_explicitly_disabled,
)


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_missing_section_defaults_to_enabled():
    """A fresh install with no llm_usage_proxy section meters usage."""
    cfg = load_llm_usage_proxy_config({})
    assert cfg["enabled"] is True
    assert cfg["manage_keys"] is False


def test_empty_config_file_on_disk_defaults_to_enabled(hermes_home):
    # No config.yaml at all — the rawest fresh-install state.
    cfg = load_llm_usage_proxy_config()
    assert cfg["enabled"] is True
    assert plugin_explicitly_disabled() is False

    _write_config(hermes_home, {"something_else": {"tree": "/x"}})
    assert load_llm_usage_proxy_config()["enabled"] is True
    assert plugin_explicitly_disabled() is False


def test_explicit_enabled_false_disables(hermes_home):
    assert load_llm_usage_proxy_config({"enabled": False})["enabled"] is False
    _write_config(hermes_home, {"llm_usage_proxy": {"enabled": False}})
    assert load_llm_usage_proxy_config()["enabled"] is False
    assert plugin_explicitly_disabled() is True


def test_string_false_counts_as_explicit_disable():
    assert load_llm_usage_proxy_config({"enabled": "false"})["enabled"] is False
    assert plugin_explicitly_disabled({"llm_usage_proxy": {"enabled": "false"}}) is True
    assert load_llm_usage_proxy_config({"enabled": "true"})["enabled"] is True
    assert plugin_explicitly_disabled({"llm_usage_proxy": {"enabled": "true"}}) is False


def test_null_enabled_falls_back_to_default_on():
    assert load_llm_usage_proxy_config({"enabled": None})["enabled"] is True
    assert plugin_explicitly_disabled({"llm_usage_proxy": {"enabled": None}}) is False


def test_plugins_deny_list_disables():
    cfg = {
        "plugins": {"disabled": ["llm_usage_proxy"]},
        "llm_usage_proxy": {"enabled": True},
    }
    assert plugin_explicitly_disabled(cfg) is True
    assert plugin_explicitly_disabled(
        {"plugins": {"disabled": ["other_plugin"]}}
    ) is False


def test_manage_keys_stays_off_by_default():
    """manage_keys is an explicit decision, never flipped by the default-on change."""
    for raw in ({}, {"enabled": True}, {"enabled": False}, {"port": 1234}):
        cfg = load_llm_usage_proxy_config(raw)
        assert cfg["manage_keys"] is False
        assert manage_keys_enabled(cfg) is False
    assert load_llm_usage_proxy_config({"manage_keys": True})["manage_keys"] is True
    assert manage_keys_enabled({"manage_keys": "on"}) is True


def test_port_and_upstreams_still_normalized():
    cfg = load_llm_usage_proxy_config({})
    assert cfg["port"] > 0
    assert cfg["upstreams"] == {}
    assert load_llm_usage_proxy_config({"port": "bogus"})["port"] == cfg["port"]
    cfg = load_llm_usage_proxy_config(
        {"port": 9000, "upstreams": {"zai": " https://z.ai/v4 ", "bad": 7}}
    )
    assert cfg["port"] == 9000
    assert cfg["upstreams"] == {"zai": "https://z.ai/v4"}


def test_non_mapping_section_is_ignored():
    cfg = load_llm_usage_proxy_config("nope")  # type: ignore[arg-type]
    assert cfg["enabled"] is True
    assert plugin_explicitly_disabled({"llm_usage_proxy": "nope"}) is False
