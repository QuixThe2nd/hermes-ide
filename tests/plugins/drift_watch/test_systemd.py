"""Systemd unit rendering, reconciliation, and probe invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.platform import InstallScope
from plugins.drift_watch.config import load_drift_watch_config
from plugins.drift_watch.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    disable_timer,
    format_environment,
    format_exec_start,
    format_status,
    is_oneshot_run_invocation,
    reconcile_scheduler_on_load,
    reconcile_units,
    render_service_unit,
    render_timer_unit,
    service_unit_path,
    timer_unit_path,
)

EXEC = ["/usr/bin/python3", "-m", "hermes_cli.main", "drift_watch", "run"]


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(
        system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user")
    )


@pytest.fixture
def cfg(tmp_path):
    return load_drift_watch_config(
        {"tree": str(tmp_path / "live-tree"), "state_dir": str(tmp_path / "state")}
    )


def _stub_platform(monkeypatch, user_scope, tmp_path, *, linger=None):
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.unit_exec_start_argv", lambda: list(EXEC)
    )
    if linger is None:
        monkeypatch.setattr(
            "plugins.drift_watch.systemd.linger_warning", lambda scope: None
        )


def test_timer_unit_renders_default_schedule_with_zero_delay(cfg):
    body = render_timer_unit(schedule=cfg["schedule"])
    assert "OnCalendar=*-*-* *:07,37:00" in body
    assert "RandomizedDelaySec=0" in body
    assert "AccuracySec=1s" in body
    assert "Persistent=true" in body
    assert f"Unit={SERVICE_NAME}" in body
    assert "WantedBy=timers.target" in body


def test_service_unit_shape(user_scope, cfg):
    body = render_service_unit(
        hermes_home="/tmp/hermes-home",
        tree=str(cfg["tree"]),
        exec_start=EXEC,
        scope=user_scope,
    )
    assert "Type=oneshot" in body
    assert "TimeoutStartSec=10min" in body
    assert f"ExecStart={format_exec_start(EXEC)}" in body
    assert f"WorkingDirectory={cfg['tree']}" in body
    assert "Environment=HERMES_HOME=/tmp/hermes-home" in body
    assert f"Environment=HERMES_PROJECT={cfg['tree']}" in body
    assert "WantedBy=default.target" in body
    assert "PartOf=" not in body
    assert "BindsTo=" not in body


def test_exec_start_quotes_paths_with_spaces():
    line = format_exec_start(["/opt/my hermes/bin/python", "drift_watch", "run"])
    assert '"/opt/my hermes/bin/python"' in line


def test_environment_values_escape_percent_sign():
    env_line = format_environment("HERMES_PROJECT", "/tmp/weird%tree")
    assert env_line == 'Environment="HERMES_PROJECT=/tmp/weird%%tree"'


def test_system_scope_group_from_st_gid(tmp_path):
    import grp
    import os
    import pwd

    home = tmp_path / "hermes-home"
    home.mkdir()
    uid = os.getuid()
    gid = os.getgid()
    os.chown(home, uid, gid)
    scope = InstallScope(
        system=True,
        unit_dir=tmp_path / "systemd",
        systemctl_prefix=("systemctl",),
    )
    body = render_service_unit(
        hermes_home=str(home),
        tree="/srv/live-tree",
        exec_start=EXEC,
        scope=scope,
    )
    assert f"User={pwd.getpwuid(uid).pw_name}" in body
    assert f"Group={grp.getgrgid(gid).gr_name}" in body
    assert "WantedBy=multi-user.target" in body


def test_reconcile_enable_writes_units_idempotently(
    user_scope, cfg, monkeypatch, tmp_path
):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)

    first = reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    service_path = service_unit_path(user_scope)
    timer_path = timer_unit_path(user_scope)
    service_bytes = service_path.read_bytes()
    timer_bytes = timer_path.read_bytes()

    second = reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    assert service_path.read_bytes() == service_bytes
    assert timer_path.read_bytes() == timer_bytes
    assert first.changed is True
    assert second.changed is False
    assert first.enabled is True
    assert first.timer_active is True
    assert first.warnings == ()

    enable_calls = [c for c in calls if "enable" in c and TIMER_NAME in c]
    assert enable_calls
    assert not any("start" in c and SERVICE_NAME in c for c in calls)
    assert not any("stop" in c and SERVICE_NAME in c for c in calls)


def test_reconcile_renders_configured_schedule(user_scope, monkeypatch, tmp_path):
    def fake_systemctl(args):
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)
    cfg = load_drift_watch_config(
        {"tree": str(tmp_path / "live-tree"), "schedule": "*-*-* 03:15:00"}
    )
    reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    assert "OnCalendar=*-*-* 03:15:00" in timer_unit_path(user_scope).read_text(
        encoding="utf-8"
    )


def test_reconcile_partial_cfg_uses_canonical_defaults(
    user_scope, monkeypatch, tmp_path
):
    def fake_systemctl(args):
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)
    reconcile_units(
        {"tree": str(tmp_path / "live-tree")},
        enabled=True,
        run_systemctl=fake_systemctl,
    )
    timer_body = timer_unit_path(user_scope).read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:07,37:00" in timer_body
    assert "RandomizedDelaySec=0" in timer_body
    assert "AccuracySec=1s" in timer_body


def test_reconcile_partial_failure_warns_and_reports_unknown(
    user_scope, cfg, monkeypatch, tmp_path
):
    def fake_systemctl(args):
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "", "Failed to connect to bus: No medium found"
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "", "Failed to connect to bus: No medium found"
        if "enable" in args:
            return 1, "", "Failed to enable unit: File exists"
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)
    result = reconcile_units(cfg, enabled=True, run_systemctl=fake_systemctl)
    assert result.enabled_known is False
    assert result.timer_active_known is False
    assert result.enabled is False
    assert any("failed to enable timer" in w for w in result.warnings)
    assert any("failed to query timer enabled state" in w for w in result.warnings)
    status = format_status(result)
    assert "unknown (probe failed)" in status
    assert "Warning: failed to enable timer" in status


def test_disabled_reconcile_stops_timer_only(user_scope, cfg, monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)
    result = reconcile_units(cfg, enabled=False, run_systemctl=fake_systemctl)
    assert result.enabled is False
    assert result.timer_active is False
    assert result.warnings == ()
    assert any("disable" in c and TIMER_NAME in c for c in calls)
    assert not any("enable" in c for c in calls)
    assert not any("stop" in c and SERVICE_NAME in c for c in calls)


def test_disable_timer_never_stops_service(user_scope):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    disable_timer(user_scope, run_systemctl=fake_systemctl)
    assert any("disable" in c and TIMER_NAME in c for c in calls)
    assert not any(SERVICE_NAME in c and "stop" in c for c in calls)


def test_empty_tree_is_inert_not_broken(user_scope, monkeypatch, tmp_path):
    def fake_systemctl(args):
        raise AssertionError(f"inert reconcile must not touch systemctl: {args}")

    _stub_platform(monkeypatch, user_scope, tmp_path)
    result = reconcile_units({"tree": ""}, enabled=True, run_systemctl=fake_systemctl)
    assert result.supported is False
    assert result.inert is True
    assert "tree" in " ".join(result.warnings)
    assert list(user_scope.unit_dir.iterdir()) == []
    assert format_status(result).startswith("Hermes drift-watch is inert")


def test_unsupported_platform_reports_unsupported(user_scope, cfg, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.platform_supported", lambda: False
    )
    result = reconcile_units(cfg, enabled=True)
    assert result.supported is False
    assert result.inert is False
    assert "unavailable on this platform" in format_status(result)


def test_missing_user_manager_reports_unsupported(cfg, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.detect_install_scope", lambda: None
    )
    result = reconcile_units(cfg, enabled=True)
    assert result.supported is False
    assert any("user manager unavailable" in w for w in result.warnings)


def test_is_oneshot_run_invocation():
    assert is_oneshot_run_invocation(["hermes", "drift_watch", "run"]) is True
    assert (
        is_oneshot_run_invocation(
            ["python", "-m", "hermes_cli.main", "drift_watch", "run"]
        )
        is True
    )
    assert is_oneshot_run_invocation(["hermes", "drift_watch", "status"]) is False
    assert is_oneshot_run_invocation(["hermes", "drift_watch"]) is False


def test_reconcile_scheduler_on_load_gates(
    user_scope, cfg, monkeypatch, tmp_path
):
    calls: list[dict] = []
    probe = {"state": "enabled"}

    def fake_systemctl(args):
        calls.append({"argv": list(args)})
        if args[-2:] == ["is-active", TIMER_NAME]:
            if probe["state"] == "enabled":
                return 0, "active\n", ""
            return 3, "inactive\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            if probe["state"] == "enabled":
                return 0, "enabled\n", ""
            return 1, "disabled\n", ""
        return 0, "", ""

    _stub_platform(monkeypatch, user_scope, tmp_path)
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.load_drift_watch_config", lambda: dict(cfg)
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.plugin_explicitly_disabled", lambda: False
    )

    result = reconcile_scheduler_on_load(run_systemctl=fake_systemctl, scope=user_scope)
    assert result is not None and result.supported is True
    assert result.enabled is True
    assert any(c["argv"][-1] == TIMER_NAME for c in calls)

    # Explicit disable stops the timer instead of installing it.
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.plugin_explicitly_disabled", lambda: True
    )
    probe["state"] = "disabled"
    calls.clear()
    result = reconcile_scheduler_on_load(run_systemctl=fake_systemctl, scope=user_scope)
    assert result is not None and result.enabled is False
    assert any("disable" in c["argv"] for c in calls)

    # The oneshot entrypoint never reconciles from inside its own run.
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr("sys.argv", ["hermes", "drift_watch", "run"])
    assert reconcile_scheduler_on_load(run_systemctl=fake_systemctl) is None


def test_reconcile_scheduler_on_load_swallows_errors(cfg, monkeypatch):
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.load_drift_watch_config", lambda: dict(cfg)
    )
    monkeypatch.setattr(
        "plugins.drift_watch.systemd.plugin_explicitly_disabled", lambda: False
    )

    def boom(*args, **kwargs):
        raise RuntimeError("unit dir unwritable")

    monkeypatch.setattr("plugins.drift_watch.systemd.reconcile_units", boom)
    assert reconcile_scheduler_on_load() is None
