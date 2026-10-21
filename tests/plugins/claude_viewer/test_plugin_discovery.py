"""Plugin discovery, config gates, and unsupported-platform import safety."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


def _stub_systemd_side_effects(monkeypatch, unit_dir: Path):
    scope = type(
        "Scope",
        (),
        {
            "system": False,
            "unit_dir": unit_dir,
            "systemctl_prefix": ("systemctl", "--user"),
        },
    )()

    monkeypatch.setattr(
        "plugins.claude_viewer.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.claude_viewer.systemd.detect_install_scope", lambda: scope
    )
    monkeypatch.setattr(
        "plugins.claude_viewer.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.claude_viewer.systemd.probe_port_state",
        lambda *a, **kw: __import__(
            "plugins.claude_viewer.port", fromlist=["PortState"]
        ).PortState("free", "port is free"),
    )
    monkeypatch.setattr(
        "plugins.claude_viewer.systemd.default_systemctl_runner",
        lambda args: (0, "active\n", ""),
    )
    return scope


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_plugin_yaml_is_backend_and_default_enabled():
    repo_root = Path(__file__).resolve().parents[3]
    import yaml

    meta = yaml.safe_load(
        (repo_root / "plugins" / "claude_viewer" / "plugin.yaml").read_text()
    )
    assert meta["kind"] == "backend"
    assert meta["default_enabled"] is True
    # Hook + CLI only: the viewer ships no agent tools.
    assert "provides_tools" not in meta


def test_bundled_viewer_assets_are_present():
    repo_root = Path(__file__).resolve().parents[3]
    viewer = repo_root / "plugins" / "claude_viewer" / "viewer"
    for name in ("server.py", "ui.html"):
        assert (viewer / name).is_file(), name
    # stdlib-only: server.py must not import anything outside the stdlib.
    import ast

    tree = ast.parse((viewer / "server.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(
        sys.stdlib_module_names
    )


def test_register_performs_zero_subprocess_and_writes(
    hermes_home, monkeypatch, tmp_path
):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub_systemd_side_effects(monkeypatch, unit_dir)

    import subprocess

    subprocess_calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(args, *run_args, **run_kwargs):
        subprocess_calls.append(list(args))
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    from plugins.claude_viewer import register

    class Ctx:
        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            return None

    register(Ctx())
    assert subprocess_calls == []
    assert list(unit_dir.iterdir()) == []


def test_bundled_default_enabled_loads(hermes_home, monkeypatch, tmp_path):
    _write_config(hermes_home, {"plugins": {"enabled": []}})
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub_systemd_side_effects(monkeypatch, unit_dir)
    reconcile_calls: list[int] = []
    monkeypatch.setattr(
        "plugins.claude_viewer.lifecycle.reconcile_viewer_on_load",
        lambda **kw: reconcile_calls.append(1),
    )

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("claude_viewer")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.error is None
    assert reconcile_calls == []
    assert list(unit_dir.iterdir()) == []
    assert "claude_viewer" in mgr._cli_commands


def test_explicit_disable_wins(hermes_home, monkeypatch, tmp_path):
    _write_config(
        hermes_home,
        {
            "plugins": {"enabled": [], "disabled": ["claude_viewer"]},
            "delegation": {"claude_viewer": {"enabled": True}},
        },
    )
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub_systemd_side_effects(monkeypatch, unit_dir)

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["claude_viewer"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"
    assert "claude_viewer" in mgr._cli_commands
    assert "on_gateway_start" in loaded.hooks_registered
    assert list(unit_dir.iterdir()) == []


def test_config_enabled_false_wins(hermes_home):
    from plugins.claude_viewer.config import plugin_explicitly_disabled

    _write_config(
        hermes_home,
        {"delegation": {"claude_viewer": {"enabled": False}}},
    )
    assert plugin_explicitly_disabled() is True


def test_import_on_unsupported_platform_does_not_explode(monkeypatch):
    monkeypatch.setattr(
        "plugins.claude_viewer.systemd.platform_supported", lambda: False
    )
    mod = importlib.import_module("plugins.claude_viewer")
    assert hasattr(mod, "register")

    class _Ctx:
        commands = []
        hooks = []

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    ctx = _Ctx()
    mod.register(ctx)
    assert len(ctx.commands) == 1
    assert len(ctx.hooks) == 1
    assert ctx.hooks[0][0] == "on_gateway_start"


@pytest.mark.parametrize(
    "module_name",
    [
        "plugins.claude_viewer.config",
        "plugins.claude_viewer.port",
        "plugins.claude_viewer.lifecycle",
        "plugins.claude_viewer.systemd",
        "tools.claude_viewer_url",
    ],
)
def test_optional_imports_on_windows_platform_gate(module_name, monkeypatch):
    monkeypatch.setitem(
        sys.modules, module_name, importlib.import_module(module_name)
    )
    mod = importlib.import_module(module_name)
    assert mod is not None
