"""Disabled-management loader path and truthful systemd reconcile behavior."""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from plugins.auto_update.cli import (
    auto_update_command,
    cmd_enable,
    cmd_reconcile,
    cmd_status,
    management_auto_update_command,
    register_management_cli,
)
from plugins.auto_update.lifecycle import reconcile_scheduler_on_load
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    TIMER_NAME,
    ProbeOutcome,
    ProbeResult,
    ReconcileResult,
    expected_timer_disable_argv,
    reconcile_units,
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


@pytest.fixture
def repo_plugins(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    return repo_root


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


def _write_config(home: Path, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _management_subcommand_names() -> set[str]:
    root = argparse.ArgumentParser()
    top = root.add_subparsers(dest="top")
    auto = top.add_parser("auto_update")
    register_management_cli(auto)
    for action in auto._actions:
        if getattr(action, "dest", None) == "auto_update_command":
            return set(action.choices)
    raise AssertionError("auto_update subcommands not registered")


def _timer_mutation_calls(calls: list[list[str]]) -> list[list[str]]:
    """Stop/disable/enable/daemon-reload only — excludes read-only probes."""
    return [
        c
        for c in calls
        if len(c) >= 2 and c[-2] in ("stop", "disable", "enable", "daemon-reload")
    ]


def _gateway_start_hook_from_manager(mgr: PluginManager):
    hooks = mgr.iter_hook_callbacks("on_gateway_start")
    assert hooks, "expected on_gateway_start hook from auto_update disabled-management"
    # Other bundled plugins register on_gateway_start too (dev_pipeline's
    # executor self-install) — select auto_update's callback, not just the
    # last-registered one. Module match covers both load shapes: the enabled
    # path (hermes_plugins.auto_update / plugins.auto_update) and the
    # disabled-management shim (hermes_disabled_mgmt.hermes_plugins_auto_update.*).
    for callback in hooks:
        if "auto_update" in (getattr(callback, "__module__", "") or ""):
            return callback
    raise AssertionError("no auto_update on_gateway_start hook registered")


def test_disabled_bundled_plugin_registers_management_cli_and_hook(
    hermes_home, repo_plugins,
):
    _write_config(hermes_home, {"plugins": {"disabled": ["auto_update"]}})

    mgr = PluginManager()
    mgr.discover_and_load()

    loaded = mgr._plugins["auto_update"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"
    assert "auto_update" in mgr._cli_commands
    assert "on_gateway_start" in loaded.hooks_registered
    assert loaded.module is not None
    assert loaded.module.__name__.startswith("hermes_disabled_mgmt.")
    assert _management_subcommand_names() == {"status", "enable", "disable", "reconcile"}


def test_disabled_user_plugin_with_disabled_management_stays_unimported(
    hermes_home, monkeypatch, tmp_path
):
    plugin_dir = hermes_home / "plugins" / "evil"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "evil",
                "version": "1.0.0",
                "disabled_management": "disabled_management",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_hook('on_session_start', lambda **k: None)\n",
        encoding="utf-8",
    )
    (plugin_dir / "disabled_management.py").write_text(
        "def register_disabled(ctx):\n"
        "    raise AssertionError('user plugin disabled_management must not run')\n",
        encoding="utf-8",
    )
    _write_config(
        hermes_home,
        {"plugins": {"enabled": ["evil"], "disabled": ["evil"]}},
    )
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))

    mgr = PluginManager()
    mgr.discover_and_load()

    loaded = mgr._plugins["evil"]
    assert loaded.enabled is False
    assert loaded.error == "disabled via config"
    assert "evil" not in mgr._cli_commands
    assert loaded.hooks_registered == []


def test_gateway_start_under_plugins_disabled_stops_timer_only(
    hermes_home, repo_plugins, user_scope, monkeypatch
):
    _write_config(hermes_home, {"plugins": {"disabled": ["auto_update"]}})

    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {
            "enabled": True,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )

    mgr = PluginManager()
    mgr.discover_and_load()
    hook = _gateway_start_hook_from_manager(mgr)
    hook(scope=user_scope, run_systemctl=fake_systemctl)

    assert _timer_mutation_calls(calls) == expected_timer_disable_argv(user_scope)
    assert not any("enable" in c for c in calls)
    assert not any(TIMER_NAME in c and "start" in c for c in calls)


def test_management_cli_status_and_enable_reachable_while_disabled(
    hermes_home, repo_plugins, user_scope, monkeypatch, capsys
):
    _write_config(hermes_home, {"plugins": {"disabled": ["auto_update"]}})

    monkeypatch.setattr(
        "plugins.auto_update.cli.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.plugin_explicitly_disabled", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.load_auto_update_config",
        lambda: {
            "enabled": False,
            "idle_minutes": 8,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.probe_timer_is_active",
        lambda scope: ProbeResult(ProbeOutcome.FALSE),
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load",
        lambda **kw: ReconcileResult(
            supported=True,
            scope=user_scope,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(),
        ),
    )

    mgr = PluginManager()
    mgr.discover_and_load()
    assert "auto_update" in mgr._cli_commands

    assert cmd_status() == 0
    out = capsys.readouterr().out
    assert "Explicit disable: yes" in out

    enable_out = StringIO()
    monkeypatch.setattr(sys, "stdout", enable_out)
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load",
        lambda **kw: ReconcileResult(
            supported=True,
            scope=user_scope,
            changed=True,
            enabled=True,
            timer_active=True,
            legacy=(),
            warnings=(),
        ),
    )
    assert cmd_enable() == 0
    assert "Scheduler installed" in enable_out.getvalue()


def test_updater_run_subcommand_absent_in_disabled_management(monkeypatch):
    assert "run" not in _management_subcommand_names()

    from unittest.mock import Mock

    fail_mock = Mock(side_effect=AssertionError("run_scheduled_update must not run"))
    monkeypatch.setattr("plugins.auto_update.cli.run_scheduled_update", fail_mock)

    assert management_auto_update_command(
        argparse.Namespace(auto_update_command="run")
    ) == 2
    fail_mock.assert_not_called()


def test_failed_enable_reports_truthful_state_and_nonzero_exit(
    hermes_home, user_scope, monkeypatch, capsys
):
    _write_config(hermes_home, {"auto_update": {"enabled": True}})

    def fake_systemctl(args):
        if args[-3:] == ["enable", "--now", TIMER_NAME]:
            return 1, "", "permission denied"
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "disabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "inactive\n", ""
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    monkeypatch.setattr("plugins.auto_update.cli.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.cli.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.load_auto_update_config",
        lambda: {
            "enabled": True,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load", lambda **kw: None
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: hermes_home
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.default_systemctl_runner", fake_systemctl
    )

    exit_code = cmd_enable()
    captured = capsys.readouterr().out
    assert exit_code != 0
    assert "Scheduler installed" not in captured
    assert "Enabled: no" in captured
    assert "Timer active: no" in captured
    assert "permission denied" in captured


@pytest.mark.parametrize(
    ("responses", "want_enabled", "expected_enabled", "expected_active", "expect_warning"),
    [
        (
            {"enable": (1, "", "nope"), "is-enabled": (1, "disabled\n", ""), "is-active": (1, "inactive\n", "")},
            True,
            False,
            False,
            True,
        ),
        (
            {"enable": (0, "", ""), "is-enabled": (0, "enabled\n", ""), "is-active": (1, "inactive\n", "")},
            True,
            True,
            False,
            True,
        ),
        (
            {"stop": (0, "", ""), "disable": (1, "", "denied"), "is-enabled": (0, "enabled\n", ""), "is-active": (0, "active\n", "")},
            False,
            True,
            True,
            True,
        ),
        (
            {"stop": (0, "", ""), "disable": (0, "", ""), "is-enabled": (1, "disabled\n", ""), "is-active": (1, "inactive\n", "")},
            False,
            False,
            False,
            False,
        ),
    ],
)
def test_reconcile_partial_systemctl_outcomes(
    user_scope,
    monkeypatch,
    responses,
    want_enabled,
    expected_enabled,
    expected_active,
    expect_warning,
):
    cfg = {
        "schedule": "*-*-* 04,05,06,07:00:00",
        "randomized_delay_sec": 1800,
        "accuracy_sec": "1s",
    }

    def fake_systemctl(args):
        if args[-3:] == ["enable", "--now", TIMER_NAME]:
            return responses.get("enable", (0, "", ""))
        if args[-2:] == ["stop", TIMER_NAME]:
            return responses.get("stop", (0, "", ""))
        if args[-2:] == ["disable", TIMER_NAME]:
            return responses.get("disable", (0, "", ""))
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return responses.get("is-enabled", (1, "disabled\n", ""))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return responses.get("is-active", (1, "inactive\n", ""))
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return (1, "disabled\n", "")
        return (0, "", "")

    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home",
        lambda: user_scope.unit_dir.parent / ".hermes",
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )

    result = reconcile_units(cfg, enabled=want_enabled, run_systemctl=fake_systemctl)
    assert result.enabled is expected_enabled
    assert result.timer_active is expected_active
    assert bool(result.warnings) is expect_warning


def test_reconcile_preserves_disabled_state_across_runs(user_scope, monkeypatch):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "disabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "inactive\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {
            "enabled": False,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )

    first = reconcile_scheduler_on_load(
        scope=user_scope, run_systemctl=fake_systemctl
    )
    second = reconcile_scheduler_on_load(
        scope=user_scope, run_systemctl=fake_systemctl
    )
    assert first is not None and first.enabled is False and first.timer_active is False
    assert second is not None and second.enabled is False and second.timer_active is False
    expected = expected_timer_disable_argv(user_scope)
    assert _timer_mutation_calls(calls) == expected + expected
    assert not any("enable" in c for c in calls)


def test_cmd_reconcile_nonzero_on_enable_failure(user_scope, monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load",
        lambda **kw: ReconcileResult(
            supported=True,
            scope=user_scope,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=("failed to enable timer: permission denied",),
        ),
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli._effective_enabled", lambda: True
    )
    assert cmd_reconcile() != 0
    assert "permission denied" in capsys.readouterr().out
