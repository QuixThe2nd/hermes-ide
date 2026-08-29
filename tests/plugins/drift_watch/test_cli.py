"""CLI wiring for ``hermes drift_watch {run,status,reconcile}``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from plugins.drift_watch.cli import (
    _management_failed,
    cmd_reconcile,
    cmd_run,
    cmd_status,
    drift_watch_command,
    register_cli,
)
from plugins.drift_watch.core import ERROR_PREFIX
from plugins.drift_watch.systemd import ProbeOutcome, ProbeResult, ReconcileResult

CFG = {
    "enabled": True,
    "tree": "/srv/live-tree",
    "state_dir": "/tmp/dw-state",
    "schedule": "*-*-* *:07,37:00",
    "retain_days": 90,
    "max_captures": 50,
}


@pytest.fixture
def wired(monkeypatch):
    """Point every cli.py collaborator at fakes; returns the call journal."""
    journal: dict[str, list] = {
        "run": [],
        "notify": [],
        "reconcile": [],
        "units": [],
    }
    monkeypatch.setattr(
        "plugins.drift_watch.cli.load_drift_watch_config", lambda: dict(CFG)
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.plugin_explicitly_disabled", lambda: False
    )

    class _Scope:
        system = False

    monkeypatch.setattr(
        "plugins.drift_watch.cli.detect_install_scope", lambda: _Scope()
    )
    monkeypatch.setattr("plugins.drift_watch.cli.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.drift_watch.cli.linger_warning", lambda scope: None
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.probe_timer_is_active",
        lambda scope: ProbeResult(ProbeOutcome.TRUE),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_drift_count", lambda state_dir: 3
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_capture_dir",
        lambda state_dir: Path("/tmp/dw-state/captures/20260829-100700"),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.run_drift_watch",
        lambda *a, **kw: journal["run"].append((a, kw)) or "",
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.emit_notification",
        lambda text: journal["notify"].append(text),
    )
    return journal


def test_status_prints_config_scope_timer_and_last_capture(wired, capsys):
    assert cmd_status() == 0
    out = capsys.readouterr().out
    assert "Timer unit: hermes-drift-watch.timer" in out
    assert "Service unit: hermes-drift-watch.service" in out
    assert "Timer active: yes" in out
    assert "Config enabled: yes" in out
    assert "Tree: /srv/live-tree" in out
    assert "State dir: /tmp/dw-state" in out
    assert "Schedule: *-*-* *:07,37:00" in out
    assert "Retain days: 90" in out
    assert "Max captures: 50" in out
    assert "Last inventory drift: 3" in out
    assert "Last capture: /tmp/dw-state/captures/20260829-100700" in out


def test_status_reports_no_inventory_yet(monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.drift_watch.cli.load_drift_watch_config", lambda: dict(CFG)
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.detect_install_scope", lambda: None
    )
    monkeypatch.setattr("plugins.drift_watch.cli.platform_supported", lambda: False)
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_drift_count", lambda state_dir: None
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_capture_dir", lambda state_dir: None
    )
    assert cmd_status() == 0
    out = capsys.readouterr().out
    assert "unavailable on this platform" in out
    assert "Last inventory drift: (no inventory yet)" in out
    assert "Last capture: (none)" in out


def test_status_warns_when_timer_probe_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.drift_watch.cli.load_drift_watch_config", lambda: dict(CFG)
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.plugin_explicitly_disabled", lambda: False
    )

    class _Scope:
        system = False

    monkeypatch.setattr(
        "plugins.drift_watch.cli.detect_install_scope", lambda: _Scope()
    )
    monkeypatch.setattr("plugins.drift_watch.cli.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.drift_watch.cli.linger_warning", lambda scope: None
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.probe_timer_is_active",
        lambda scope: ProbeResult(ProbeOutcome.QUERY_FAILED, "bus gone"),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_drift_count", lambda state_dir: 0
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.last_capture_dir", lambda state_dir: None
    )
    assert cmd_status() == 0
    out = capsys.readouterr().out
    assert "Timer active: unknown (probe failed)" in out
    assert "failed to query timer active state: bus gone" in out


def test_run_silent_when_unchanged(wired, capsys):
    assert cmd_run() == 0
    assert capsys.readouterr().out == ""
    assert wired["run"] and wired["notify"] == []


def test_run_is_a_noop_when_disabled(wired, capsys, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.cli.plugin_explicitly_disabled", lambda: True
    )
    assert cmd_run() == 0
    assert capsys.readouterr().out == ""
    assert wired["run"] == []

    monkeypatch.setattr(
        "plugins.drift_watch.cli.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.load_drift_watch_config",
        lambda: {**CFG, "enabled": False},
    )
    assert cmd_run() == 0
    assert wired["run"] == []


def test_run_prints_alerts_and_notifies(wired, capsys, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.cli.run_drift_watch",
        lambda *a, **kw: "drift detected in /srv/live-tree: 1 path(s)\n```x\n```",
    )
    assert cmd_run() == 0
    out = capsys.readouterr().out
    assert out.startswith("drift detected")
    assert wired["notify"] == [out.rstrip("\n")]


def test_run_surfaces_error_strings_as_failure(wired, capsys, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.cli.run_drift_watch",
        lambda *a, **kw: f"{ERROR_PREFIX} tree not found: /srv/live-tree",
    )
    assert cmd_run() == 1
    assert capsys.readouterr().out.startswith(ERROR_PREFIX)


def test_reconcile_ok_exits_zero(wired, capsys, monkeypatch):
    ok = ReconcileResult(
        supported=True,
        scope=None,
        changed=True,
        enabled=True,
        timer_active=True,
        warnings=(),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.reconcile_scheduler_on_load", lambda: ok
    )
    assert cmd_reconcile() == 0
    out = capsys.readouterr().out
    assert "Timer unit: hermes-drift-watch.timer" in out
    assert "Enabled: yes" in out


def test_reconcile_inert_tree_exits_zero(wired, capsys, monkeypatch):
    inert = ReconcileResult(
        supported=False,
        scope=None,
        changed=False,
        enabled=False,
        timer_active=False,
        warnings=("no tree configured (set drift_watch.tree or HERMES_PROJECT);",),
        inert=True,
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.reconcile_scheduler_on_load", lambda: inert
    )
    assert cmd_reconcile() == 0
    assert "inert" in capsys.readouterr().out


def test_reconcile_unsupported_platform_exits_one(wired, capsys, monkeypatch):
    unsupported = ReconcileResult(
        supported=False,
        scope=None,
        changed=False,
        enabled=False,
        timer_active=False,
        warnings=(),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.reconcile_scheduler_on_load", lambda: unsupported
    )
    assert cmd_reconcile() == 1


def test_reconcile_operational_failure_exits_one(wired, capsys, monkeypatch):
    broken = ReconcileResult(
        supported=True,
        scope=None,
        changed=False,
        enabled=True,
        timer_active=True,
        warnings=("failed to enable timer: boom",),
    )
    monkeypatch.setattr(
        "plugins.drift_watch.cli.reconcile_scheduler_on_load", lambda: broken
    )
    assert cmd_reconcile() == 1
    assert "Warning: failed to enable timer: boom" in capsys.readouterr().out


def test_management_failed_matrix():
    ok = ReconcileResult(
        supported=True, scope=None, changed=False, enabled=True,
        timer_active=True, warnings=(),
    )
    assert _management_failed(ok, want_enabled=True) is False
    assert _management_failed(ok, want_enabled=False) is True

    probe_unknown = ReconcileResult(
        supported=True, scope=None, changed=False, enabled=True,
        timer_active=True, warnings=(), enabled_known=False,
    )
    assert _management_failed(probe_unknown, want_enabled=True) is True

    not_active = ReconcileResult(
        supported=True, scope=None, changed=False, enabled=True,
        timer_active=False, warnings=("timer enabled but not active",),
    )
    assert _management_failed(not_active, want_enabled=True) is True

    inert = ReconcileResult(
        supported=False, scope=None, changed=False, enabled=False,
        timer_active=False, warnings=(), inert=True,
    )
    assert _management_failed(inert, want_enabled=True) is False

    disabled_ok = ReconcileResult(
        supported=True, scope=None, changed=False, enabled=False,
        timer_active=False, warnings=(),
    )
    assert _management_failed(disabled_ok, want_enabled=False) is False

    still_enabled = ReconcileResult(
        supported=True, scope=None, changed=False, enabled=True,
        timer_active=True, warnings=(),
    )
    assert _management_failed(still_enabled, want_enabled=False) is True


def test_cli_dispatch_and_usage(capsys):
    parser = argparse.ArgumentParser(prog="hermes drift_watch")
    register_cli(parser)
    assert parser.parse_args(["run"]).drift_watch_command == "run"
    assert parser.parse_args(["status"]).drift_watch_command == "status"
    assert parser.parse_args(["reconcile"]).drift_watch_command == "reconcile"
    assert parser.parse_args([]).drift_watch_command is None

    assert drift_watch_command(argparse.Namespace(drift_watch_command=None)) == 2
    assert "usage: hermes drift_watch {run,status,reconcile}" in capsys.readouterr().out
