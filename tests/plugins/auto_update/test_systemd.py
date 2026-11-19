"""Systemd unit lifecycle and reconcile invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.config import load_auto_update_config
from plugins.auto_update.legacy import LEGACY_KNOWN_REFERENCE_TIMER, LEGACY_TIMER
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    disable_timer,
    format_environment,
    format_exec_start,
    prestamp_timer,
    reconcile_units,
    render_service_unit,
    render_timer_unit,
    service_unit_path,
    timer_unit_path,
    _parse_unit_environment_home,
)


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


@pytest.fixture
def system_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))


def _prepare_reconcile(monkeypatch, hermes_home: Path):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        if args[-2:] == ["is-enabled", LEGACY_TIMER]:
            return 0, "enabled\n", ""
        return 0, "", ""

    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home", lambda: hermes_home
    )
    monkeypatch.setattr(
        "plugins.auto_update.legacy.get_hermes_home", lambda: hermes_home
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr("plugins.auto_update.systemd.profile_cli_args", lambda: [])
    monkeypatch.setattr(
        "plugins.auto_update.systemd.timer_stamp_path",
        lambda scope, timer_name=TIMER_NAME: hermes_home / "stamps" / timer_name,
    )
    return calls, fake_systemctl


def _install_units(scope: InstallScope, hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    service_unit_path(scope).write_text(
        render_service_unit(
            hermes_home=str(hermes_home),
            exec_start=[
                "/usr/bin/python3",
                "-m",
                "hermes_cli.main",
                "auto_update",
                "run",
            ],
            scope=scope,
        ),
        encoding="utf-8",
    )
    timer_unit_path(scope).write_text(
        render_timer_unit(schedule="daily", randomized_delay_sec=0, accuracy_sec="1s"),
        encoding="utf-8",
    )


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


@pytest.mark.parametrize("scope_fixture", ["user_scope", "system_scope"])
def test_enabled_reconcile_refuses_foreign_home_before_any_mutation(
    scope_fixture, request, cfg, monkeypatch, tmp_path
):
    scope = request.getfixturevalue(scope_fixture)
    production_home = tmp_path / "production home"
    disposable_home = tmp_path / "disposable home"
    _install_units(scope, production_home)
    legacy_path = scope.unit_dir / LEGACY_TIMER
    legacy_path.write_text(LEGACY_KNOWN_REFERENCE_TIMER, encoding="utf-8")
    service_before = service_unit_path(scope).read_bytes()
    timer_before = timer_unit_path(scope).read_bytes()
    legacy_before = legacy_path.read_bytes()
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)

    result = reconcile_units(cfg, enabled=True, scope=scope, run_systemctl=runner)

    assert result.changed is False
    assert result.enabled_known is False
    assert result.timer_active_known is False
    warning = "\n".join(result.warnings)
    assert str(production_home.resolve()) in warning
    assert str(disposable_home.resolve()) in warning
    assert "auto_update.systemd_takeover: true" in warning
    assert calls == []
    assert service_unit_path(scope).read_bytes() == service_before
    assert timer_unit_path(scope).read_bytes() == timer_before
    assert legacy_path.read_bytes() == legacy_before
    assert not (disposable_home / "auto-update" / "legacy-units").exists()


def test_systemd_takeover_allows_intentional_foreign_home_move(
    system_scope, monkeypatch, tmp_path
):
    production_home = tmp_path / "production"
    disposable_home = tmp_path / "disposable"
    _install_units(system_scope, production_home)
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)
    takeover_cfg = load_auto_update_config({"systemd_takeover": "true"})

    result = reconcile_units(
        takeover_cfg, enabled=True, scope=system_scope, run_systemctl=runner
    )

    assert result.changed is True
    assert service_unit_path(system_scope).read_text(
        encoding="utf-8"
    ) == render_service_unit(
        hermes_home=str(disposable_home.resolve()),
        exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
        scope=system_scope,
    )
    assert any("daemon-reload" in call for call in calls)
    assert any("enable" in call and TIMER_NAME in call for call in calls)


def test_same_home_with_escaped_multiple_environment_assignments_reconciles(
    user_scope, cfg, monkeypatch, tmp_path
):
    hermes_home = tmp_path / "same home\\100%"
    _install_units(user_scope, hermes_home)
    service_path = service_unit_path(user_scope)
    original = service_path.read_text(encoding="utf-8")
    environment_line = next(
        line
        for line in original.splitlines()
        if line.startswith("Environment=") and "HERMES_HOME=" in line
    )
    assignments = environment_line.removeprefix("Environment=")
    service_path.write_text(
        original.replace(
            environment_line,
            f'Environment="BEFORE=left" {assignments} "AFTER=right value"',
        ),
        encoding="utf-8",
    )
    calls, runner = _prepare_reconcile(monkeypatch, hermes_home)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.changed is True
    assert not any("refusing" in warning for warning in result.warnings)
    assert service_path.read_text(encoding="utf-8") == original
    assert any("daemon-reload" in call for call in calls)


@pytest.mark.parametrize(
    "home",
    [
        pytest.param("/tmp/hermes home", id="space"),
        pytest.param("/tmp/hermes'home", id="single-quote"),
        pytest.param('/tmp/hermes"home', id="double-quote"),
        pytest.param(r"/tmp/hermes\home", id="backslash"),
        pytest.param(r"/tmp/hermes\ home", id="backslash-before-space"),
        pytest.param(r"/tmp/hermes\$home", id="backslash-before-dollar"),
        pytest.param(r"/tmp/hermes\%home", id="backslash-before-percent"),
        pytest.param("/tmp/hermes\\", id="trailing-backslash"),
        pytest.param("/tmp/hermes$home", id="dollar"),
        pytest.param("/tmp/hermes%home", id="percent"),
        pytest.param("/tmp/hermes#home", id="hash"),
        pytest.param("/tmp/hermes=home", id="equals"),
        pytest.param("/tmp/hermes/", id="trailing-slash"),
        pytest.param("/tmp/a b\\\\c%d$e", id="reported-regression"),
    ],
)
def test_format_environment_home_round_trips_through_ownership_parser(home):
    environment = format_environment("HERMES_HOME", home)

    assert _parse_unit_environment_home(f"[Service]\n{environment}\n") == home


def test_format_environment_exact_escaping_round_trips():
    home = "/tmp/a b\\\\c%d$e"

    environment = format_environment("HERMES_HOME", home)

    assert environment == r'Environment="HERMES_HOME=/tmp/a b\\\\c%%d\$e"'
    assert _parse_unit_environment_home(f"[Service]\n{environment}\n") == home


def test_format_environment_quotes_lone_single_quote():
    environment = format_environment("HERMES_HOME", "/tmp/hermes'home")

    assert environment == "Environment=\"HERMES_HOME=/tmp/hermes'home\""


def test_same_home_with_special_characters_reconciles(
    user_scope, cfg, monkeypatch, tmp_path
):
    hermes_home = tmp_path / "a b\\\\c%d$e"
    _install_units(user_scope, hermes_home)
    service_path = service_unit_path(user_scope)
    service_before = service_path.read_bytes()
    calls, runner = _prepare_reconcile(monkeypatch, hermes_home)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.enabled is True
    assert result.timer_active is True
    assert result.enabled_known is True
    assert result.timer_active_known is True
    assert not any("refusing" in warning for warning in result.warnings)
    assert not any("cannot determine" in warning for warning in result.warnings)
    assert service_path.read_bytes() == service_before
    assert any("enable" in call and TIMER_NAME in call for call in calls)


def test_environment_home_supports_multiple_assignments():
    body = (
        "[Service]\n"
        'Environment="BEFORE=left value" "HERMES_HOME=/tmp/current home" '
        '"AFTER=right value"\n'
    )

    assert _parse_unit_environment_home(body) == "/tmp/current home"


def test_environment_home_supports_line_continuation_and_comments():
    body = (
        "[Service]\n"
        'Environment="BEFORE=left" \\\n'
        "# ignored during continuation\n"
        ' "HERMES_HOME=/tmp/continued home" \\\n'
        ' "AFTER=right"\n'
    )

    assert _parse_unit_environment_home(body) == "/tmp/continued home"


def test_environment_home_reset_followed_by_assignment():
    body = (
        "[Service]\n"
        'Environment="HERMES_HOME=/tmp/foreign"\n'
        "Environment=\n"
        'Environment="HERMES_HOME=/tmp/current home"\n'
    )

    assert _parse_unit_environment_home(body) == "/tmp/current home"


def test_environment_home_reset_without_new_assignment_fails_closed():
    body = (
        "[Service]\n"
        'Environment="HERMES_HOME=/tmp/foreign"\n'
        "Environment=\n"
    )

    with pytest.raises(ValueError, match="no HERMES_HOME"):
        _parse_unit_environment_home(body)


def test_environment_home_repeated_assignment_last_wins():
    body = (
        "[Service]\n"
        "Environment=HERMES_HOME=/tmp/first HERMES_HOME=/tmp/second\n"
        'Environment="HERMES_HOME=/tmp/final home"\n'
    )

    assert _parse_unit_environment_home(body) == "/tmp/final home"


@pytest.mark.parametrize(
    "environment",
    [
        pytest.param(r'"HERMES_HOME=/tmp/\q"', id="unknown-escape"),
        pytest.param(r'"HERMES_HOME=/tmp/\x4G"', id="malformed-hex"),
        pytest.param(r'"HERMES_HOME=/tmp/\u12G4"', id="malformed-unicode"),
        pytest.param(r'"HERMES_HOME=/tmp/\U00110000"', id="invalid-codepoint"),
        pytest.param(r'"HERMES_HOME=/tmp/\000"', id="nul"),
        pytest.param(r"HERMES_HOME=/tmp/\ ", id="escaped-space"),
    ],
)
def test_environment_home_rejects_malformed_escapes(environment):
    with pytest.raises(ValueError):
        _parse_unit_environment_home(f"[Service]\nEnvironment={environment}\n")


@pytest.mark.parametrize(
    "environment",
    [
        pytest.param("HERMES_HOME=/tmp/%h", id="dynamic-specifier"),
        pytest.param("HERMES_HOME=/tmp/%", id="trailing-percent"),
        pytest.param("HERMES_HOME=/tmp/%%%h", id="literal-then-dynamic-specifier"),
    ],
)
def test_environment_home_rejects_malformed_specifiers(environment):
    with pytest.raises(ValueError, match="specifier"):
        _parse_unit_environment_home(f"[Service]\nEnvironment={environment}\n")


@pytest.mark.parametrize(
    "service_body",
    [
        pytest.param("[Service]\nType=oneshot\n", id="missing-home"),
        pytest.param(
            '[Service]\nEnvironment="HERMES_HOME=/unterminated\n',
            id="malformed-quote",
        ),
        pytest.param(
            "[Service]\nEnvironment=HERMES_HOME=relative/home\n",
            id="relative-home",
        ),
        pytest.param(
            '[Service]\nEnvironment="HERMES_HOME=/home/%h/hermes"\n',
            id="dynamic-specifier",
        ),
    ],
)
def test_enabled_reconcile_refuses_ambiguous_existing_ownership(
    service_body, user_scope, cfg, monkeypatch, tmp_path
):
    production_home = tmp_path / "production"
    disposable_home = tmp_path / "disposable"
    _install_units(user_scope, production_home)
    service_path = service_unit_path(user_scope)
    service_path.write_text(service_body, encoding="utf-8")
    service_before = service_path.read_bytes()
    timer_before = timer_unit_path(user_scope).read_bytes()
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.changed is False
    warning = "\n".join(result.warnings)
    assert "cannot determine HERMES_HOME ownership" in warning
    assert "auto_update.systemd_takeover: true" in warning
    assert calls == []
    assert service_path.read_bytes() == service_before
    assert timer_unit_path(user_scope).read_bytes() == timer_before


def test_enabled_reconcile_refuses_unreadable_existing_ownership(
    user_scope, cfg, monkeypatch, tmp_path
):
    production_home = tmp_path / "production"
    disposable_home = tmp_path / "disposable"
    _install_units(user_scope, production_home)
    service_path = service_unit_path(user_scope)
    service_before = service_path.read_bytes()
    timer_before = timer_unit_path(user_scope).read_bytes()
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)
    original_read_text = Path.read_text

    def deny_service_read(path, *args, **kwargs):
        if path == service_path:
            raise PermissionError("denied for regression")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_service_read)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.changed is False
    warning = "\n".join(result.warnings)
    assert "cannot determine HERMES_HOME ownership" in warning
    assert "denied for regression" in warning
    assert calls == []
    assert service_path.read_bytes() == service_before
    assert timer_unit_path(user_scope).read_bytes() == timer_before


def test_disabled_reconcile_refuses_foreign_owned_timer(
    user_scope, cfg, monkeypatch, tmp_path
):
    production_home = tmp_path / "production"
    disposable_home = tmp_path / "disposable"
    _install_units(user_scope, production_home)
    service_before = service_unit_path(user_scope).read_bytes()
    timer_before = timer_unit_path(user_scope).read_bytes()
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)

    result = reconcile_units(cfg, enabled=False, scope=user_scope, run_systemctl=runner)

    assert result.changed is False
    assert result.enabled_known is False
    assert result.timer_active_known is False
    warning = "\n".join(result.warnings)
    assert str(production_home.resolve()) in warning
    assert str(disposable_home.resolve()) in warning
    assert "auto_update.systemd_takeover: true" in warning
    assert calls == []
    assert service_unit_path(user_scope).read_bytes() == service_before
    assert timer_unit_path(user_scope).read_bytes() == timer_before


def test_disabled_reconcile_disables_own_timer(user_scope, cfg, monkeypatch, tmp_path):
    hermes_home = tmp_path / "disposable"
    _install_units(user_scope, hermes_home)
    calls, runner = _prepare_reconcile(monkeypatch, hermes_home)

    result = reconcile_units(cfg, enabled=False, scope=user_scope, run_systemctl=runner)

    assert result.changed is False
    assert any("stop" in call and TIMER_NAME in call for call in calls)
    assert any("disable" in call and TIMER_NAME in call for call in calls)
    assert not any("enable" in call for call in calls)
    assert not any("takeover" in warning for warning in result.warnings)


def test_disabled_takeover_allows_disabling_foreign_timer(
    user_scope, monkeypatch, tmp_path
):
    production_home = tmp_path / "production"
    disposable_home = tmp_path / "disposable"
    _install_units(user_scope, production_home)
    calls, runner = _prepare_reconcile(monkeypatch, disposable_home)
    takeover_cfg = load_auto_update_config({"systemd_takeover": "true"})

    result = reconcile_units(
        takeover_cfg, enabled=False, scope=user_scope, run_systemctl=runner
    )

    assert any("stop" in call and TIMER_NAME in call for call in calls)
    assert any("disable" in call and TIMER_NAME in call for call in calls)
    assert not any("takeover" in warning for warning in result.warnings)


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
