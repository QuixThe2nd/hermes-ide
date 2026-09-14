"""Systemctl probe failure handling for auto_update reconcile and CLI."""

from __future__ import annotations

import pytest
import yaml

from plugins.auto_update.cli import _management_failed, cmd_disable, cmd_enable
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    TIMER_NAME,
    ProbeOutcome,
    format_status,
    probe_timer_is_active,
    probe_timer_is_enabled,
    reconcile_units,
)

DBUS_ERR = "Failed to connect to bus: No such file or directory"
TIMEOUT_ERR = "Command '['systemctl', '--user', 'is-enabled']' timed out after 120 seconds"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


def _cfg():
    return {
        "schedule": "*-*-* 04,05,06,07:00:00",
        "randomized_delay_sec": 1800,
        "accuracy_sec": "1s",
    }


def _fake_runner(responses: dict[tuple[str, ...], tuple[int, str, str]]):
    def fake_systemctl(args):
        key = tuple(args[-2:])
        if key in responses:
            return responses[key]
        if args[-2:] == ["is-enabled", "hermes-auto-update.timer"]:
            return 1, "disabled\n", ""
        return 0, "", ""

    return fake_systemctl


def _patch_reconcile_env(monkeypatch, hermes_home, user_scope):
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
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


def test_clean_not_found_and_disabled_probes_remain_false(user_scope):
    enabled = probe_timer_is_enabled(
        user_scope,
        run_systemctl=_fake_runner(
            {
                ("is-enabled", TIMER_NAME): (4, "not-found\n", ""),
            }
        ),
    )
    active = probe_timer_is_active(
        user_scope,
        run_systemctl=_fake_runner(
            {
                ("is-active", TIMER_NAME): (1, "inactive\n", ""),
            }
        ),
    )
    assert enabled.outcome == ProbeOutcome.FALSE
    assert active.outcome == ProbeOutcome.FALSE


@pytest.mark.parametrize(
    ("probe", "stderr", "stdout", "code"),
    [
        ("enabled", DBUS_ERR, "", 1),
        ("active", DBUS_ERR, "", 1),
        ("enabled", "Connection timed out", "", 1),
        ("active", TIMEOUT_ERR, "", 1),
        ("enabled", "Access denied", "", 1),
        ("active", "Permission denied", "", 1),
    ],
)
def test_disable_reconcile_surfaces_probe_failures(
    probe,
    stderr,
    stdout,
    code,
    user_scope,
    hermes_home,
    monkeypatch,
):
    responses = {
        ("stop", TIMER_NAME): (0, "", ""),
        ("disable", TIMER_NAME): (0, "", ""),
    }
    if probe == "enabled":
        responses[("is-enabled", TIMER_NAME)] = (code, stdout, stderr)
        responses[("is-active", TIMER_NAME)] = (1, "inactive\n", "")
    else:
        responses[("is-enabled", TIMER_NAME)] = (1, "disabled\n", "")
        responses[("is-active", TIMER_NAME)] = (code, stdout, stderr)

    _patch_reconcile_env(monkeypatch, hermes_home, user_scope)
    result = reconcile_units(
        _cfg(),
        enabled=False,
        run_systemctl=_fake_runner(responses),
    )
    assert f"failed to query timer {probe} state:" in "\n".join(result.warnings)
    assert _management_failed(result, want_enabled=False) is True
    assert result.enabled_known is (probe != "enabled")
    assert result.timer_active_known is (probe != "active")


def test_disable_reconcile_surfaces_both_probe_failures(
    user_scope, hermes_home, monkeypatch
):
    responses = {
        ("stop", TIMER_NAME): (0, "", ""),
        ("disable", TIMER_NAME): (0, "", ""),
        ("is-enabled", TIMER_NAME): (1, "", DBUS_ERR),
        ("is-active", TIMER_NAME): (1, "", DBUS_ERR),
    }
    _patch_reconcile_env(monkeypatch, hermes_home, user_scope)
    result = reconcile_units(
        _cfg(),
        enabled=False,
        run_systemctl=_fake_runner(responses),
    )
    joined = "\n".join(result.warnings)
    assert "failed to query timer enabled state:" in joined
    assert "failed to query timer active state:" in joined
    assert _management_failed(result, want_enabled=False) is True


def test_cmd_disable_nonzero_without_success_line_on_probe_failure(
    hermes_home, user_scope, monkeypatch, capsys
):
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"auto_update": {"enabled": True}}),
        encoding="utf-8",
    )

    def fake_systemctl(args):
        if args[-2:] == ["stop", TIMER_NAME]:
            return 0, "", ""
        if args[-2:] == ["disable", TIMER_NAME]:
            return 0, "", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "", DBUS_ERR
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "inactive\n", ""
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
        lambda: {**_cfg(), "enabled": False},
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load", lambda **kw: None
    )
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.detect_install_scope", lambda: user_scope
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.default_systemctl_runner", fake_systemctl
    )

    exit_code = cmd_disable()
    captured = capsys.readouterr().out
    assert exit_code != 0
    assert "Hermes auto-update disabled; timer stopped." not in captured
    assert DBUS_ERR in captured
    assert "unknown (probe failed)" in captured


def test_enable_path_probe_failure_exits_nonzero_with_diagnostic(
    hermes_home, user_scope, monkeypatch, capsys
):
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"auto_update": {"enabled": True}}),
        encoding="utf-8",
    )

    def fake_systemctl(args):
        if args[-3:] == ["enable", "--now", TIMER_NAME]:
            return 0, "", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "", DBUS_ERR
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
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
        lambda: {**_cfg(), "enabled": True},
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load", lambda **kw: None
    )
    _patch_reconcile_env(monkeypatch, hermes_home, user_scope)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.default_systemctl_runner", fake_systemctl
    )

    exit_code = cmd_enable()
    captured = capsys.readouterr().out
    assert exit_code != 0
    assert "Scheduler installed" not in captured
    assert DBUS_ERR in captured
    assert "unknown (probe failed)" in captured


def test_format_status_shows_unknown_for_failed_probes(user_scope):
    from plugins.auto_update.systemd import ReconcileResult

    result = ReconcileResult(
        supported=True,
        scope=user_scope,
        changed=False,
        enabled=False,
        timer_active=False,
        legacy=(),
        warnings=("failed to query timer enabled state: dbus down",),
        enabled_known=False,
        timer_active_known=True,
    )
    text = format_status(result)
    assert "Enabled: unknown (probe failed)" in text
    assert "Timer active: no" in text


def test_successful_disable_still_prints_success_line(
    hermes_home, user_scope, monkeypatch, capsys
):
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"auto_update": {"enabled": True}}),
        encoding="utf-8",
    )

    def fake_systemctl(args):
        if args[-2:] == ["stop", TIMER_NAME]:
            return 0, "", ""
        if args[-2:] == ["disable", TIMER_NAME]:
            return 0, "", ""
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "disabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "inactive\n", ""
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
        lambda: {**_cfg(), "enabled": False},
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load",
        lambda **kw: reconcile_units(
            _cfg(),
            enabled=False,
            run_systemctl=fake_systemctl,
            scope=user_scope,
        ),
    )

    assert cmd_disable() == 0
    assert "Hermes auto-update disabled; timer stopped." in capsys.readouterr().out
