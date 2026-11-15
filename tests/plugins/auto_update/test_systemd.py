"""Systemd unit lifecycle and reconcile invariants."""

from __future__ import annotations

from types import SimpleNamespace
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


def test_timer_renders_six_hour_schedule_with_zero_delay(cfg):
    body = render_timer_unit(
        schedule=cfg["schedule"],
        randomized_delay_sec=cfg["randomized_delay_sec"],
        accuracy_sec=cfg["accuracy_sec"],
    )
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in body
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
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in timer_body
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


@pytest.mark.parametrize("system", [True, False], ids=["machine", "user"])
def test_reconcile_refuses_foreign_owned_units(
    system, cfg, monkeypatch, tmp_path
):
    """A disposable home must not take over another home's installed scheduler."""
    scope = InstallScope(
        system=system,
        unit_dir=tmp_path / ("system" if system else "user"),
        systemctl_prefix=("systemctl",) if system else ("systemctl", "--user"),
    )
    scope.unit_dir.mkdir()
    service_path = service_unit_path(scope)
    timer_path = timer_unit_path(scope)
    production_service = (
        render_service_unit(
            hermes_home="/root/.hermes",
            exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
            scope=scope,
        ).encode()
        + b"# production service bytes\n"
    )
    production_timer = (
        render_timer_unit(
            schedule=cfg["schedule"],
            randomized_delay_sec=cfg["randomized_delay_sec"],
            accuracy_sec=cfg["accuracy_sec"],
        ).encode()
        + b"# production timer bytes\n"
    )
    service_path.write_bytes(production_service)
    timer_path.write_bytes(production_timer)

    calls = []
    migration_calls = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    def fake_migrate(*_args, **_kwargs):
        migration_calls.append(True)
        return SimpleNamespace(warnings=(), disabled_units=())

    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home",
        lambda: tmp_path / "disposable-home",
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr("plugins.auto_update.systemd.migrate_legacy_units", fake_migrate)

    result = reconcile_units(
        cfg,
        enabled=True,
        scope=scope,
        run_systemctl=fake_systemctl,
    )

    assert service_path.read_bytes() == production_service
    assert timer_path.read_bytes() == production_timer
    assert migration_calls == []
    mutating_commands = {"daemon-reload", "enable", "disable", "start", "stop", "restart"}
    assert not [call for call in calls if mutating_commands.intersection(call)]
    assert result.supported is True
    assert result.scope == scope
    assert result.changed is False
    assert result.enabled is True
    assert result.timer_active is True
    assert result.enabled_known is True
    assert result.timer_active_known is True
    warning = "\n".join(result.warnings)
    assert "refusing" in warning.lower()
    assert "/root/.hermes" in warning
    assert "auto_update.systemd_takeover: true" in warning


def _stub_enabled_reconcile(monkeypatch, home: Path, migration_calls: list) -> None:
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.systemd.get_hermes_home", lambda: home)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )

    def fake_migrate(*_args, **_kwargs):
        migration_calls.append(True)
        return SimpleNamespace(warnings=(), disabled_units=())

    monkeypatch.setattr("plugins.auto_update.systemd.migrate_legacy_units", fake_migrate)


def _recording_systemctl(calls: list):
    def run(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        return 0, "", ""

    return run


def _write_installed_units(scope: InstallScope, cfg, home: str) -> tuple[bytes, bytes]:
    service = render_service_unit(
        hermes_home=home,
        exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
        scope=scope,
    ).encode()
    timer = render_timer_unit(
        schedule=cfg["schedule"],
        randomized_delay_sec=cfg["randomized_delay_sec"],
        accuracy_sec=cfg["accuracy_sec"],
    ).encode()
    scope.unit_dir.mkdir(parents=True, exist_ok=True)
    service_unit_path(scope).write_bytes(service)
    timer_unit_path(scope).write_bytes(timer)
    return service, timer


def test_reconcile_allows_deliberate_takeover(cfg, monkeypatch, tmp_path):
    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "takeover" / "user",
        systemctl_prefix=("systemctl", "--user"),
    )
    old_service, old_timer = _write_installed_units(scope, cfg, "/root/.hermes")
    current_home = tmp_path / "deliberate-home"
    calls = []
    migration_calls = []
    _stub_enabled_reconcile(monkeypatch, current_home, migration_calls)
    takeover_cfg = load_auto_update_config({"systemd_takeover": "yes"})

    result = reconcile_units(
        takeover_cfg,
        enabled=True,
        scope=scope,
        run_systemctl=_recording_systemctl(calls),
    )

    assert service_unit_path(scope).read_bytes() != old_service
    assert timer_unit_path(scope).read_bytes() == old_timer
    assert str(current_home.resolve()) in service_unit_path(scope).read_text(encoding="utf-8")
    assert result.changed is True
    assert migration_calls == [True]
    assert any("daemon-reload" in call for call in calls)
    assert any(call[-3:] == ["enable", "--now", TIMER_NAME] for call in calls)
    assert not any("refusing" in warning.lower() for warning in result.warnings)


def test_reconcile_accepts_same_owner_with_renderer_escaped_home(
    cfg, monkeypatch, tmp_path
):
    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "same-owner" / "user",
        systemctl_prefix=("systemctl", "--user"),
    )
    current_home = tmp_path / "same owner%$'\"\\$\\"
    service, timer = _write_installed_units(scope, cfg, str(current_home.resolve()))
    calls = []
    migration_calls = []
    _stub_enabled_reconcile(monkeypatch, current_home, migration_calls)

    result = reconcile_units(
        cfg,
        enabled=True,
        scope=scope,
        run_systemctl=_recording_systemctl(calls),
    )

    assert service_unit_path(scope).read_bytes() == service
    assert timer_unit_path(scope).read_bytes() == timer
    assert result.changed is False
    assert migration_calls == [True]
    assert not any("daemon-reload" in call for call in calls)
    assert not any("refusing" in warning.lower() for warning in result.warnings)


def test_reconcile_fresh_install_needs_no_existing_ownership(
    cfg, monkeypatch, tmp_path
):
    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "fresh" / "user",
        systemctl_prefix=("systemctl", "--user"),
    )
    calls = []
    migration_calls = []
    _stub_enabled_reconcile(monkeypatch, tmp_path / "fresh-home", migration_calls)

    result = reconcile_units(
        cfg,
        enabled=True,
        scope=scope,
        run_systemctl=_recording_systemctl(calls),
    )

    assert service_unit_path(scope).is_file()
    assert timer_unit_path(scope).is_file()
    assert result.changed is True
    assert migration_calls == [True]
    assert any(call[-3:] == ["enable", "--now", TIMER_NAME] for call in calls)
    assert not any("refusing" in warning.lower() for warning in result.warnings)


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize(
    "service_body",
    [
        "[Service]\nType=oneshot\n",
        '[Service]\nEnvironment="HERMES_HOME=/root/.hermes\n',
        "[Service]\nEnvironment=HERMES_HOME=relative/home\n",
        "[Service]\nEnvironment=HERMES_HOME=%h\n",
    ],
    ids=["missing", "malformed", "relative", "dynamic"],
)
def test_reconcile_refuses_invalid_ownership_evidence(
    service_body, takeover, cfg, monkeypatch, tmp_path
):
    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "invalid" / "user",
        systemctl_prefix=("systemctl", "--user"),
    )
    scope.unit_dir.mkdir(parents=True)
    original_service = service_body.encode()
    original_timer = b"administrator-owned timer\n"
    service_unit_path(scope).write_bytes(original_service)
    timer_unit_path(scope).write_bytes(original_timer)
    calls = []
    migration_calls = []
    _stub_enabled_reconcile(monkeypatch, tmp_path / "disposable", migration_calls)
    test_cfg = dict(cfg)
    test_cfg["systemd_takeover"] = takeover

    result = reconcile_units(
        test_cfg,
        enabled=True,
        scope=scope,
        run_systemctl=_recording_systemctl(calls),
    )

    assert service_unit_path(scope).read_bytes() == original_service
    assert timer_unit_path(scope).read_bytes() == original_timer
    assert result.changed is False
    assert result.enabled is True
    assert result.timer_active is True
    assert migration_calls == []
    mutating_commands = {"daemon-reload", "enable", "disable", "start", "stop", "restart"}
    assert not [call for call in calls if mutating_commands.intersection(call)]
    assert any("could not verify" in warning for warning in result.warnings)


def test_reconcile_refuses_unreadable_ownership_evidence(
    cfg, monkeypatch, tmp_path
):
    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "unreadable" / "user",
        systemctl_prefix=("systemctl", "--user"),
    )
    service, timer = _write_installed_units(scope, cfg, "/root/.hermes")
    service_path = service_unit_path(scope)
    original_read_text = Path.read_text

    def unreadable_service(path, *args, **kwargs):
        if path == service_path:
            raise PermissionError("read denied by test")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_service)
    calls = []
    migration_calls = []
    _stub_enabled_reconcile(monkeypatch, tmp_path / "disposable", migration_calls)

    result = reconcile_units(
        cfg,
        enabled=True,
        scope=scope,
        run_systemctl=_recording_systemctl(calls),
    )

    assert service_path.read_bytes() == service
    assert timer_unit_path(scope).read_bytes() == timer
    assert result.changed is False
    assert migration_calls == []
    mutating_commands = {"daemon-reload", "enable", "disable", "start", "stop", "restart"}
    assert not [call for call in calls if mutating_commands.intersection(call)]
    assert any("could not read" in warning for warning in result.warnings)
