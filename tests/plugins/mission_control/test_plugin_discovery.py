"""Plugin discovery, manifest shape, and import hygiene for
mission_control.

The plugin is CLI-only: it must load through the real PluginManager as
a default-enabled bundled plugin, register exactly one CLI command,
contribute no model tools, and keep the server module's imports to the
stdlib plus the repo's own hermes_constants (TASK.md's
generic-safe-defaults contract). Nothing here runs the server.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins" / "mission_control"

# Personal/machine-specific strings that must never ship in the plugin
# (TASK.md: generic identities, paths, and deployment guidance only).
FORBIDDEN_STRINGS = (
    "/root/.hermes",
    "/root/hermes-agent",
    "yazdani",
    "Big Steve",
    "Winnie",
    "Quix",
    "sessions.yazdani.au",
    "192.168.",
    "100.64.",
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_plugin_yaml_is_backend_cli_only_and_default_enabled():
    meta = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert meta["name"] == "mission_control"
    assert meta["kind"] == "backend"
    assert meta["default_enabled"] is True
    # CLI-only: the plugin ships no agent-facing tools.
    assert "provides_tools" not in meta


def test_no_personal_or_machine_specific_strings_ship():
    for name in ("server.py", "cli.py", "__init__.py", "README.md",
                 "plugin.yaml"):
        text = (PLUGIN_DIR / name).read_text(encoding="utf-8")
        for needle in FORBIDDEN_STRINGS:
            assert needle not in text, (name, needle)


def test_no_static_assets_or_images_ship():
    # A clean install must work without any bundled imagery: the UI
    # renders letter badges, so the plugin directory carries no
    # binaries at all.
    for path in PLUGIN_DIR.rglob("*"):
        assert path.suffix.lower() not in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}, path
        assert path.name != "static", path


def test_server_imports_stdlib_plus_repo_constants_only():
    tree = ast.parse(
        (PLUGIN_DIR / "server.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                and node.module:
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"hermes_constants"}
    assert imported <= allowed, imported - allowed


def test_register_registers_one_cli_command_and_nothing_else():
    import subprocess

    subprocess_calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(args, *run_args, **run_kwargs):
        subprocess_calls.append(list(args))
        return real_run(args, *run_args, **run_kwargs)

    import plugins.mission_control as plugin

    assert not hasattr(plugin, "tools")

    class Ctx:
        def __init__(self):
            self.commands = []
            self.hooks = []

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    import unittest.mock as mock

    with mock.patch.object(subprocess, "run", counting_run):
        ctx = Ctx()
        plugin.register(ctx)
    assert len(ctx.commands) == 1
    entry = ctx.commands[0]
    assert entry["name"] == "mission_control"
    assert entry["setup_fn"] and callable(entry["setup_fn"])
    assert entry["handler_fn"] and callable(entry["handler_fn"])
    # No gateway hooks: the server runs only when a person invokes it.
    assert ctx.hooks == []
    assert subprocess_calls == []


def test_cli_registers_serve_subcommand():
    import argparse

    from plugins.mission_control.cli import mission_control_command

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    plugin_parser = subparsers.add_parser("mission_control")
    from plugins.mission_control.cli import register_cli
    register_cli(plugin_parser)

    args = parser.parse_args(
        ["mission_control", "serve", "--host", "127.0.0.1",
         "--port", "9199", "--no-discord-sync"])
    assert args.mission_control_command == "serve"
    assert args.host == "127.0.0.1"
    assert args.port == 9199
    assert args.no_discord_sync is True

    # Defaults defer to config: no flags means None, not a baked host.
    bare = parser.parse_args(["mission_control", "serve"])
    assert bare.host is None and bare.port is None

    # No subcommand -> usage + exit code 2, no server started.
    missing = parser.parse_args(["mission_control"])
    assert mission_control_command(missing) == 2


def test_bundled_default_enabled_loads_and_registers_cli(hermes_home,
                                                          monkeypatch):
    _write_config(hermes_home, {"plugins": {"enabled": []}})
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(REPO_ROOT / "plugins"))

    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("mission_control")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.error is None
    assert "mission_control" in mgr._cli_commands
    entry = mgr._cli_commands["mission_control"]
    assert entry["setup_fn"] and entry["handler_fn"]


def test_plugin_contributes_no_model_tools(hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(REPO_ROOT / "plugins"))

    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    tool_names = set(mgr._plugin_tool_names) if hasattr(
        mgr, "_plugin_tool_names") else set()
    assert not any("mission_control" in n or "mission_control" in str(n)
                   for n in tool_names)


def test_explicit_disable_wins(hermes_home, monkeypatch):
    _write_config(
        hermes_home, {"plugins": {"enabled": [], "disabled":
                                  ["mission_control"]}})
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(REPO_ROOT / "plugins"))

    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["mission_control"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"
