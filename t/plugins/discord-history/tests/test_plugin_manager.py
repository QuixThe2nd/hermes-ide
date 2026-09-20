from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    yield home


def _install_plugin_copy(home: Path) -> Path:
    plugins_root = home / "plugins"
    plugins_root.mkdir(parents=True)
    dest = plugins_root / "discord-history"
    shutil.copytree(
        PLUGIN_ROOT,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    return plugins_root


def test_plugin_manager_registers_discord_history_without_live_secrets(isolated_home):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    secret_path = isolated_home / "secrets" / "discord-history.env"
    assert not secret_path.exists()

    plugins_root = _install_plugin_copy(isolated_home)
    entries_before = {entry.name: entry for entry in registry._snapshot_entries()}
    manager = PluginManager()
    manifests = manager._scan_directory(plugins_root, source="user")
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.name == "discord-history"

    manager._load_plugin(manifest)
    loaded = manager._plugins["discord-history"]
    assert loaded.enabled
    assert not loaded.error
    assert "discord_history" in loaded.tools_registered

    entry = registry.get_entry("discord_history")
    assert entry is not None
    assert entry.toolset == "discord_history"
    assert not secret_path.exists()

    entries_after = {entry.name: entry for entry in registry._snapshot_entries()}
    for name in set(entries_before) | set(entries_after):
        previous = entries_before.get(name)
        current = entries_after.get(name)
        if current is not previous:
            if previous is None:
                registry._tools.pop(name, None)
            else:
                registry._tools[name] = previous
