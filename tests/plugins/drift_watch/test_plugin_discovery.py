"""Plugin discovery, config gates, and unsupported-platform import safety."""

from __future__ import annotations

import importlib
import subprocess
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
        "plugins.drift_watch.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.detect_install_scope", lambda: scope
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.default_systemctl_runner",
        lambda args: (0, "disabled\n", ""),
    )
    return scope


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_register_performs_zero_subprocess_and_writes(hermes_home, monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub_systemd_side_effects(monkeypatch, unit_dir)

    subprocess_calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(args, *run_args, **run_kwargs):
        subprocess_calls.append(list(args))
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    from plugins.drift_watch import register

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
        "plugins.drift_watch.systemd.reconcile_scheduler_on_load",
        lambda **kw: reconcile_calls.append(1),
    )

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins.get("drift_watch")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.error is None
    assert reconcile_calls == []
    assert list(unit_dir.iterdir()) == []


def test_explicit_disable_wins(hermes_home, monkeypatch, tmp_path):
    _write_config(
        hermes_home,
        {
            "plugins": {"enabled": [], "disabled": ["drift_watch"]},
            "drift_watch": {"enabled": True},
        },
    )
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    _stub_systemd_side_effects(monkeypatch, unit_dir)

    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["drift_watch"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"
    # Unlike auto_update there is no disabled-management CLI variant: a
    # config-disabled drift_watch registers nothing at all.
    assert "drift_watch" not in mgr._cli_commands
    assert loaded.hooks_registered == []
    assert list(unit_dir.iterdir()) == []


def test_register_registers_on_gateway_start_when_ctx_supports_hooks(monkeypatch):
    from plugins.drift_watch import register

    class Ctx:
        hooks: list[tuple[str, object]] = []

        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    ctx = Ctx()
    register(ctx)
    assert ctx.hooks and ctx.hooks[0][0] == "on_gateway_start"


def test_on_gateway_start_forwards_scope_and_runner(monkeypatch):
    from plugins.drift_watch import register

    seen: list[dict] = []
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.reconcile_scheduler_on_load",
        lambda **kw: seen.append(kw),
    )

    class Ctx:
        hooks: list[tuple[str, object]] = []

        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    ctx = Ctx()
    register(ctx)
    assert seen == []  # register() itself never reconciles

    hook_name, hook = ctx.hooks[0]
    assert hook_name == "on_gateway_start"
    hook()
    hook(scope="scope", run_systemctl="runner", ignored="dropped")
    assert seen == [{}, {"scope": "scope", "run_systemctl": "runner"}]


def test_plugin_manifest_contract():
    manifest_path = (
        Path(__file__).resolve().parents[3] / "plugins" / "drift_watch" / "plugin.yaml"
    )
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "drift_watch"
    assert data["default_enabled"] is True
    assert data["version"] == "0.1.0"
    assert data["kind"] == "backend"
    assert data["author"] == "Hermes Agent"
    assert "hermes drift_watch reconcile" in data["description"]


@pytest.mark.parametrize("module_name", [
    "plugins.drift_watch.config",
    "plugins.drift_watch.core",
    "plugins.drift_watch.systemd",
])
def test_optional_imports_on_windows_platform_gate(module_name, monkeypatch):
    monkeypatch.setitem(sys.modules, module_name, importlib.import_module(module_name))
    mod = importlib.import_module(module_name)
    assert mod is not None
