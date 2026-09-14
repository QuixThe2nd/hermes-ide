"""Systemd unit lifecycle and reconcile invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.config import load_auto_update_config
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    disable_timer,
    format_exec_start,
    prestamp_timer,
    reconcile_units,
    render_service_unit,
    render_timer_unit,
    service_unit_path,
    timer_unit_path,
)


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


@pytest.fixture
def cfg():
    return load_auto_update_config({})


def test_service_unit_has_no_gateway_coupling(user_scope):
    body = render_service_unit(
        hermes_home="/tmp/hermes",
        exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
        scope=user_scope,
    )
    assert "Type=oneshot" in body
    assert "PartOf=" not in body
    assert "BindsTo=" not in body


def test_exec_start_quotes_paths_with_spaces():
    line = format_exec_start(["/opt/my hermes/bin/python", "auto_update", "run"])
    assert '"/opt/my hermes/bin/python"' in line


def test_timer_renders_every_30_minutes_schedule_with_zero_delay(cfg):
    body = render_timer_unit(
        schedule=cfg["schedule"],
        randomized_delay_sec=cfg["randomized_delay_sec"],
        accuracy_sec=cfg["accuracy_sec"],
    )
    assert "OnCalendar=*-*-* *:00,30:00" in body
    assert "Persistent=true" in body
    assert "RandomizedDelaySec=0" in body
    assert "AccuracySec=1s" in body
    assert f"Unit={SERVICE_NAME}" in body


def test_reconcile_partial_cfg_uses_canonical_defaults(user_scope, monkeypatch, tmp_path):
    def fake_systemctl(args):
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    stamp_dir = tmp_path / "stamps"
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: stamp_dir / f"stamp-{timer_name}",
    )

    reconcile_units({}, enabled=True, run_systemctl=fake_systemctl)
    timer_body = timer_unit_path(user_scope).read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:00,30:00" in timer_body
    assert "RandomizedDelaySec=0" in timer_body
    assert "AccuracySec=1s" in timer_body


def test_reconcile_legacy_hour_window_renders_requested_schedule(
    user_scope, monkeypatch, tmp_path
):
    def fake_systemctl(args):
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    stamp_dir = tmp_path / "stamps"
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: stamp_dir / f"stamp-{timer_name}",
    )

    legacy_cfg = load_auto_update_config(
        {"schedule_start_hour": 4, "schedule_end_hour": 8}
    )
    reconcile_units(legacy_cfg, enabled=True, run_systemctl=fake_systemctl)
    timer_body = timer_unit_path(user_scope).read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04,05,06,07:00:00" in timer_body


def test_reconcile_idempotent_and_atomic(user_scope, cfg, monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    stamp_dir = tmp_path / "stamps"
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: stamp_dir / f"stamp-{timer_name}",
    )

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
    enable_calls = [c for c in calls if "enable" in c and TIMER_NAME in c]
    assert enable_calls
    assert not any("start" in c and SERVICE_NAME in c for c in calls)
    assert not any("stop" in c and SERVICE_NAME in c for c in calls)


def test_first_enable_prestamps_and_does_not_start_service(
    user_scope, cfg, monkeypatch, tmp_path
):
    calls: list[list[str]] = []
    stamp_writes: list[tuple[Path, str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "disabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    def fake_write(path: Path, payload: str) -> None:
        stamp_writes.append((path, payload))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: tmp_path / ".hermes"
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    stamp_dir = tmp_path / "stamps"
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: stamp_dir / f"stamp-{timer_name}",
    )

    reconcile_units(
        cfg,
        enabled=True,
        run_systemctl=fake_systemctl,
        write_stamp=fake_write,
    )
    assert stamp_writes
    assert stamp_writes[0][0].name == f"stamp-{TIMER_NAME}"
    assert not any("start" in c and SERVICE_NAME in c for c in calls)


def test_disabled_reconcile_stops_timer_only(user_scope, cfg, monkeypatch):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.systemd.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    result = reconcile_units(cfg, enabled=False, run_systemctl=fake_systemctl)
    assert result.enabled is False
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


def test_prestamp_timer_writes_usec_stamp(user_scope, tmp_path, monkeypatch):
    stamp_dir = tmp_path / "stamps"
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: stamp_dir / f"stamp-{timer_name}",
    )
    prestamp_timer(user_scope, now_usec=1234567890000000)
    stamp = stamp_dir / f"stamp-{TIMER_NAME}"
    assert stamp.read_text(encoding="utf-8") == "1234567890000000"


def test_environment_values_escape_percent_sign():
    from plugins.auto_update.systemd import format_environment, render_service_unit

    env_line = format_environment("HERMES_HOME", "/tmp/weird%home")
    assert env_line == 'Environment="HERMES_HOME=/tmp/weird%%home"'
    body = render_service_unit(
        hermes_home="/tmp/weird%home",
        exec_start=["/usr/bin/python3", "auto_update", "run"],
        scope=None,
    )
    assert env_line in body


def test_system_scope_group_from_st_gid(tmp_path, monkeypatch):
    import grp
    import os
    import pwd

    home = tmp_path / "hermes-home"
    home.mkdir()
    uid = os.getuid()
    gid = os.getgid()
    os.chown(home, uid, gid)
    user = pwd.getpwuid(uid).pw_name
    group = grp.getgrgid(gid).gr_name
    scope = InstallScope(
        system=True,
        unit_dir=tmp_path / "systemd",
        systemctl_prefix=("systemctl",),
    )
    body = render_service_unit(
        hermes_home=str(home),
        exec_start=["/usr/bin/python3", "auto_update", "run"],
        scope=scope,
    )
    assert f"User={user}" in body
    assert f"Group={group}" in body
