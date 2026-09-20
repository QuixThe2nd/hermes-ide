"""Bundled plugin discovery and tool registration E2E."""

from __future__ import annotations

import sys

import pytest
import yaml


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


@pytest.fixture(autouse=True)
def _restore_plugin_modules():
    prefixes = ("plugins.quota_channels", "hermes_cli.plugins")
    saved = {k: m for k, m in sys.modules.items() if k.startswith(prefixes)}
    yield
    for key in list(sys.modules):
        if key.startswith(prefixes):
            del sys.modules[key]
    sys.modules.update(saved)
    for key, mod in saved.items():
        if "." in key:
            parent_name, attr = key.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, mod)


def _minimal_quota_config():
    return {
        "quota_channels": {
            "guild_id": "111",
            "category_id": "222",
            "channel_ids": {"codex": "333"},
            "enabled_providers": ["codex"],
        }
    }


class TestQuotaChannelsPluginLoad:
    def test_discover_registers_tool_and_check_fn(self, _isolate_env):
        for key in list(sys.modules):
            if key.startswith(("plugins.quota_channels", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import PluginManager
        from tools.registry import invalidate_check_fn_cache, registry

        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        assert "quota_channels" in mgr._plugins
        loaded = mgr._plugins["quota_channels"]
        assert loaded.enabled is True
        assert loaded.error is None

        entry = registry.get_entry("quota_channels_tick")
        assert entry is not None
        assert entry.toolset == "quota_channels"

        invalidate_check_fn_cache()
        assert entry.check_fn() is False

        (_isolate_env / "config.yaml").write_text(
            yaml.safe_dump(_minimal_quota_config())
        )
        invalidate_check_fn_cache()
        assert entry.check_fn() is True
