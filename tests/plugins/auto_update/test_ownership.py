"""Scheduler ownership, disposable-home safety, and takeover contracts."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from plugins.auto_update.cli import (
    auto_update_command,
    cmd_reconcile,
    management_auto_update_command,
    register_management_cli,
)
from plugins.auto_update.legacy import LegacyMigrationResult
from plugins.auto_update.lifecycle import reconcile_scheduler_on_load
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    TIMER_NAME,
    reconcile_units,
    render_service_unit,
    render_timer_unit,
    scheduler_owner_path,
    service_unit_path,
    timer_unit_path,
)

STABLE_HOME = Path("/home/hermes-auto-update-tests/.hermes")
ALTERNATE_HOME = Path("/home/hermes-auto-update-tests/alternate/.hermes")


@pytest.fixture
def user_scope(tmp_path) -> InstallScope:
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    return InstallScope(
        system=False,
        unit_dir=unit_dir,
        systemctl_prefix=("systemctl", "--user"),
    )


@pytest.fixture
def disposable_home():
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="hermes-auto-update-disposable-"
    ) as home:
        yield Path(home)


@pytest.fixture
def cfg():
    return {
        "schedule": "*-*-* 00,06,12,18:00:00",
        "randomized_delay_sec": 0,
        "accuracy_sec": "1s",
    }


def _owner_path(scope: InstallScope) -> Path:
    return scheduler_owner_path(scope)


def _owner_data(scope: InstallScope) -> dict:
    return json.loads(_owner_path(scope).read_text(encoding="utf-8"))


def _set_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr("plugins.auto_update.systemd.get_hermes_home", lambda: home)
    monkeypatch.setattr("plugins.auto_update.lifecycle.get_hermes_home", lambda: home)

def _patch_reconcile(
    monkeypatch,
    scope: InstallScope,
    home: Path,
    calls: list[list[str]],
    *,
    fail_enable: bool = False,
):
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.systemd.linger_warning", lambda _scope: None)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.migrate_legacy_units",
        lambda *_args, **_kwargs: LegacyMigrationResult(
            disabled_units=(), warnings=(), refused=(), backed_up_units=()
        ),
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    _set_home(monkeypatch, home)

    def fake_systemctl(args):
        calls.append(list(args))
        if args[-3:] == ["enable", "--now", TIMER_NAME] and fail_enable:
            return 1, "", "enable rejected"
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 0, "enabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 0, "active\n", ""
        return 0, "", ""

    return fake_systemctl


def _patch_lifecycle(monkeypatch, cfg) -> None:
    monkeypatch.setattr("plugins.auto_update.lifecycle.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: False)
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {**cfg, "enabled": True},
    )


def _reconcile_lifecycle(
    monkeypatch, cfg, scope: InstallScope, runner, *, takeover: bool = False
):
    _patch_lifecycle(monkeypatch, cfg)
    return reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=runner,
        takeover=takeover,
    )

def _assert_final_mutation_is_reload(calls: list[list[str]]) -> None:
    mutations = [
        call
        for call in calls
        if {"enable", "disable", "stop", "daemon-reload"}.intersection(call)
    ]
    assert mutations[-1][-1] == "daemon-reload"


def _write_legacy_units(scope: InstallScope, home: Path, cfg, *, environment=True):
    service = render_service_unit(
        hermes_home=str(home),
        exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
        scope=scope,
    )
    if not environment:
        service = "\n".join(
            line for line in service.splitlines() if not line.startswith("Environment=HERMES_HOME=")
        ) + "\n"
    service_unit_path(scope).write_text(service, encoding="utf-8")
    timer_unit_path(scope).write_text(
        render_timer_unit(
            schedule=cfg["schedule"],
            randomized_delay_sec=cfg["randomized_delay_sec"],
            accuracy_sec=cfg["accuracy_sec"],
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "home",
    [
        Path("/tmp/hermes-disposable"),
        Path("/var/tmp/hermes-disposable"),
        Path("/var/folders/ab/cd/T/.hermes"),
        Path("/run/user/1000/.hermes"),
        Path("/dev/shm/.hermes"),
        Path("/home/test/pytest-of-user/pytest-0/test_run0/.hermes"),
    ],
)
def test_disposable_home_refused_before_any_mutation(
    home, user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, home, calls)

    result = _reconcile_lifecycle(monkeypatch, cfg, user_scope, runner)

    assert result.changed is False
    assert calls == []
    assert not service_unit_path(user_scope).exists()
    assert not timer_unit_path(user_scope).exists()
    assert not _owner_path(user_scope).exists()
    warning = "\n".join(result.warnings)
    assert "failed to" in warning
    assert "--takeover" in warning


@pytest.mark.parametrize("variable", ["TMPDIR", "TMP", "TEMP"])
def test_configured_temp_root_refused(
    variable, user_scope, cfg, monkeypatch
):
    temp_root = Path("/home/hermes-auto-update-tests/environment-transient")
    home = temp_root / "profile"
    monkeypatch.setenv(variable, str(temp_root))
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, home, calls)

    result = _reconcile_lifecycle(monkeypatch, cfg, user_scope, runner)

    assert calls == []
    assert not service_unit_path(user_scope).exists()
    assert "--takeover" in "\n".join(result.warnings)


def test_runtime_tempdir_refused(user_scope, cfg, monkeypatch):
    temp_root = Path("/home/hermes-auto-update-tests/runtime-transient")
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.tempfile.gettempdir", lambda: str(temp_root)
    )
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, temp_root / "home", calls)

    result = _reconcile_lifecycle(monkeypatch, cfg, user_scope, runner)

    assert calls == []
    assert not service_unit_path(user_scope).exists()
    assert "--takeover" in "\n".join(result.warnings)


def test_disposable_gateway_home_preserves_production_scheduler(
    user_scope, cfg, monkeypatch, disposable_home
):
    production_home = Path("/home/hermes-auto-update-tests/production")
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, production_home, calls)
    _patch_lifecycle(monkeypatch, cfg)

    from plugins.auto_update import register

    hooks: list[tuple[str, object]] = []

    class Ctx:
        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            hooks.append((hook_name, callback))

    register(Ctx())
    callback = hooks[0][1]
    callback(scope=user_scope, run_systemctl=runner)

    service_path = service_unit_path(user_scope)
    timer_path = timer_unit_path(user_scope)
    production_service = service_path.read_text(encoding="utf-8")
    production_timer = timer_path.read_text(encoding="utf-8")
    assert f"WorkingDirectory={production_home}" in production_service
    assert f"Environment=HERMES_HOME={production_home}" in production_service

    calls.clear()
    _set_home(monkeypatch, disposable_home)
    callback(scope=user_scope, run_systemctl=runner)

    assert service_path.read_text(encoding="utf-8") == production_service
    assert timer_path.read_text(encoding="utf-8") == production_timer
    mutations = {"daemon-reload", "enable", "disable", "stop"}
    assert not any(mutations.intersection(call) for call in calls)


@pytest.mark.parametrize(
    "home",
    [
        Path("/home/test/.hermes"),
        Path("/root/.hermes"),
        Path("/home/test/.hermes/profiles/named"),
    ],
)
def test_stable_home_and_named_profile_can_own_fresh_units(
    home, user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, home, calls)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.enabled is True
    assert _owner_data(user_scope) == {"format": 1, "hermes_home": str(home)}


def test_foreign_stable_home_preserves_units_and_owner(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), _owner_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)

    _set_home(monkeypatch, ALTERNATE_HOME)
    calls.clear()
    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert calls == []
    assert tuple(path.read_bytes() for path in paths) == before
    assert _owner_data(user_scope)["hermes_home"] == str(STABLE_HOME)
    assert "--takeover" in "\n".join(result.warnings)


def test_same_home_reconcile_is_idempotent_including_owner(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    first = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), _owner_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)

    second = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert first.changed is True
    assert second.changed is False
    assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize("environment", [True, False])
def test_legacy_same_home_units_are_adopted_with_systemd_quoting(
    environment, user_scope, cfg, monkeypatch
):
    home = Path("/home/hermes tests/.hermes%cache")
    _write_legacy_units(user_scope, home, cfg, environment=environment)
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, home, calls)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.enabled is True
    assert _owner_data(user_scope)["hermes_home"] == str(home)
    assert any("enable" in call for call in calls)


def test_legacy_foreign_home_units_are_preserved(user_scope, cfg, monkeypatch):
    _write_legacy_units(user_scope, STABLE_HOME, cfg)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, ALTERNATE_HOME, calls)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert calls == []
    assert tuple(path.read_bytes() for path in paths) == before
    assert not _owner_path(user_scope).exists()
    assert "--takeover" in "\n".join(result.warnings)


@pytest.mark.parametrize("owner_state", ["corrupt", "unreadable"])
def test_bad_owner_record_never_authorizes_foreign_overwrite(
    owner_state, user_scope, cfg, monkeypatch
):
    _write_legacy_units(user_scope, STABLE_HOME, cfg)
    owner = _owner_path(user_scope)
    owner.write_text("{not json", encoding="utf-8")
    if owner_state == "unreadable":
        original_read_text = Path.read_text

        def guarded_read_text(path, *args, **kwargs):
            if path == owner:
                raise PermissionError("owner record denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded_read_text)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), owner)
    before = tuple(path.read_bytes() for path in paths)
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, ALTERNATE_HOME, calls)

    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert calls == []
    assert tuple(path.read_bytes() for path in paths) == before
    assert "--takeover" in "\n".join(result.warnings)


def test_takeover_transfers_only_after_successful_enable(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    _set_home(monkeypatch, ALTERNATE_HOME)
    owners_at_enable: list[str] = []

    def takeover_runner(args):
        if args[-3:] == ["enable", "--now", TIMER_NAME]:
            owners_at_enable.append(_owner_data(user_scope)["hermes_home"])
        return runner(args)

    calls.clear()
    result = reconcile_units(
        cfg,
        enabled=True,
        takeover=True,
        scope=user_scope,
        run_systemctl=takeover_runner,
    )

    assert result.enabled is True
    assert owners_at_enable == [str(STABLE_HOME)]
    assert _owner_data(user_scope)["hermes_home"] == str(ALTERNATE_HOME)
    assert f"Environment=HERMES_HOME={ALTERNATE_HOME}" in service_unit_path(
        user_scope
    ).read_text(encoding="utf-8")


def test_takeover_allows_disposable_home_explicitly(user_scope, cfg, monkeypatch):
    disposable = Path("/tmp/hermes-explicit-takeover")
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, disposable, calls)

    result = _reconcile_lifecycle(
        monkeypatch, cfg, user_scope, runner, takeover=True
    )

    assert result.enabled is True
    assert _owner_data(user_scope)["hermes_home"] == str(disposable)


def test_failed_takeover_retains_old_owner_and_blocks_implicit_retry(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), _owner_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)
    calls.clear()

    _set_home(monkeypatch, ALTERNATE_HOME)
    failed_runner = _patch_reconcile(
        monkeypatch, user_scope, ALTERNATE_HOME, calls, fail_enable=True
    )
    reload_states = []

    def observe_reloads(args):
        if args[-1:] == ["daemon-reload"]:
            reload_states.append(tuple(path.read_bytes() for path in paths))
        return failed_runner(args)

    failed = reconcile_units(
        cfg,
        enabled=True,
        takeover=True,
        scope=user_scope,
        run_systemctl=observe_reloads,
    )

    assert "failed to enable timer" in "\n".join(failed.warnings)
    assert tuple(path.read_bytes() for path in paths) == before
    assert reload_states[-1] == before
    _assert_final_mutation_is_reload(calls)
    assert _owner_data(user_scope)["hermes_home"] == str(STABLE_HOME)
    calls.clear()
    retry = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert calls == []
    assert tuple(path.read_bytes() for path in paths) == before
    assert "--takeover" in "\n".join(retry.warnings)


def test_owner_write_failure_retains_previous_owner(user_scope, cfg, monkeypatch):
    from plugins.auto_update import systemd

    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), _owner_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)
    calls.clear()
    original_atomic_write = systemd.atomic_write

    def fail_owner_write(path, content):
        if path == _owner_path(user_scope):
            raise OSError("owner write denied")
        return original_atomic_write(path, content)

    monkeypatch.setattr("plugins.auto_update.systemd.atomic_write", fail_owner_write)
    _set_home(monkeypatch, ALTERNATE_HOME)
    reload_states = []

    def observe_reloads(args):
        if args[-1:] == ["daemon-reload"]:
            reload_states.append(tuple(path.read_bytes() for path in paths))
        return runner(args)

    result = reconcile_units(
        cfg, enabled=True, takeover=True, scope=user_scope, run_systemctl=observe_reloads
    )

    assert "failed to record scheduler ownership" in "\n".join(result.warnings)
    assert tuple(path.read_bytes() for path in paths) == before
    assert reload_states[-1] == before
    _assert_final_mutation_is_reload(calls)


def test_fresh_owner_write_failure_leaves_no_unowned_units(
    user_scope, cfg, monkeypatch
):
    from plugins.auto_update import systemd

    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    original_atomic_write = systemd.atomic_write

    def fail_owner_write(path, content):
        if path == _owner_path(user_scope):
            raise OSError("owner write denied")
        return original_atomic_write(path, content)

    monkeypatch.setattr("plugins.auto_update.systemd.atomic_write", fail_owner_write)
    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert "failed to record scheduler ownership" in "\n".join(result.warnings)
    assert not service_unit_path(user_scope).exists()
    assert not timer_unit_path(user_scope).exists()
    assert not _owner_path(user_scope).exists()
    _assert_final_mutation_is_reload(calls)


def test_failed_first_install_leaves_no_owner_and_same_home_can_retry(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    failed_runner = _patch_reconcile(
        monkeypatch, user_scope, STABLE_HOME, calls, fail_enable=True
    )
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=failed_runner)
    assert not _owner_path(user_scope).exists()

    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    result = reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)

    assert result.enabled is True
    assert _owner_data(user_scope)["hermes_home"] == str(STABLE_HOME)


def test_disable_from_disposable_home_preserves_units_and_owner(
    user_scope, cfg, monkeypatch
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(monkeypatch, user_scope, STABLE_HOME, calls)
    reconcile_units(cfg, enabled=True, scope=user_scope, run_systemctl=runner)
    paths = (service_unit_path(user_scope), timer_unit_path(user_scope), _owner_path(user_scope))
    before = tuple(path.read_bytes() for path in paths)

    _set_home(monkeypatch, Path("/tmp/hermes-disable-disposable"))
    calls.clear()

    def disabled_runner(args):
        if args[-2:] == ["is-enabled", TIMER_NAME]:
            return 1, "disabled\n", ""
        if args[-2:] == ["is-active", TIMER_NAME]:
            return 1, "inactive\n", ""
        return runner(args)

    result = reconcile_units(
        cfg, enabled=False, scope=user_scope, run_systemctl=disabled_runner
    )

    assert result.enabled is False
    assert any("disable" in call and TIMER_NAME in call for call in calls)
    assert any("stop" in call and TIMER_NAME in call for call in calls)
    assert not any("enable" in call or "daemon-reload" in call for call in calls)
    assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize("command", ["enable", "reconcile"])
def test_management_parser_accepts_takeover(command):
    root = argparse.ArgumentParser()
    top = root.add_subparsers(dest="top")
    auto = top.add_parser("auto_update")
    register_management_cli(auto)

    args = root.parse_args(["auto_update", command, "--takeover"])

    assert args.takeover is True


@pytest.mark.parametrize("handler", [auto_update_command, management_auto_update_command])
@pytest.mark.parametrize("command", ["enable", "reconcile"])
def test_cli_takeover_reaches_lifecycle_for_enabled_and_disabled_management(
    handler, command, user_scope, monkeypatch
):
    from plugins.auto_update.systemd import ReconcileResult

    received: list[dict] = []

    def successful_reconcile(**kwargs):
        received.append(kwargs)
        return ReconcileResult(
            supported=True,
            scope=user_scope,
            changed=False,
            enabled=True,
            timer_active=True,
            legacy=(),
            warnings=(),
        )

    monkeypatch.setattr("plugins.auto_update.cli.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.cli._save_enabled_flag", lambda _enabled: None)
    monkeypatch.setattr("plugins.auto_update.cli._effective_enabled", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.cli.reconcile_scheduler_on_load", successful_reconcile
    )

    exit_code = handler(
        argparse.Namespace(auto_update_command=command, takeover=True)
    )

    assert exit_code == 0
    assert received == [{"takeover": True}]


def test_cli_disposable_refusal_is_nonzero_and_actionable(
    user_scope, cfg, monkeypatch, capsys
):
    calls: list[list[str]] = []
    runner = _patch_reconcile(
        monkeypatch, user_scope, Path("/tmp/hermes-cli-disposable"), calls
    )
    monkeypatch.setattr("plugins.auto_update.cli._effective_enabled", lambda: True)
    _patch_lifecycle(monkeypatch, cfg)
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.detect_install_scope", lambda: user_scope
    )

    exit_code = cmd_reconcile()

    assert exit_code != 0
    assert "--takeover" in capsys.readouterr().out
    assert calls == []
    assert not service_unit_path(user_scope).exists()
