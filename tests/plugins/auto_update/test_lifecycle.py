"""Lifecycle hook and oneshot entrypoint invariants."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import sys
import time

import pytest

from hermes_state import SessionDB
from plugins.auto_update.legacy import LegacyMigrationResult
from plugins.auto_update.lifecycle import is_oneshot_run_invocation, reconcile_scheduler_on_load
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import (
    ReconcileResult,
    TIMER_NAME,
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
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


def test_register_only_registers_cli_command(monkeypatch):
    reconcile_calls: list[int] = []

    monkeypatch.setattr(
        "plugins.auto_update.systemd.reconcile_units",
        lambda *a, **k: reconcile_calls.append(1),
    )

    from plugins.auto_update import register

    class Ctx:
        commands: list[dict] = []

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

    ctx = Ctx()
    register(ctx)
    assert len(ctx.commands) == 1
    assert ctx.commands[0]["name"] == "auto_update"
    assert reconcile_calls == []


def test_register_registers_on_gateway_start_when_ctx_supports_hooks(monkeypatch):
    from plugins.auto_update import register

    class Ctx:
        hooks: list[tuple[str, object]] = []

        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    ctx = Ctx()
    register(ctx)
    assert len(ctx.hooks) == 1
    assert ctx.hooks[0][0] == "on_gateway_start"

def _register_gateway_start_hook(monkeypatch):
    from plugins.auto_update import register

    hooks: list[tuple[str, object]] = []

    class Ctx:
        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            hooks.append((hook_name, callback))

    register(Ctx())
    assert len(hooks) == 1
    assert hooks[0][0] == "on_gateway_start"
    return hooks[0][1]


def test_gateway_start_hook_reconciles_enabled_state(monkeypatch, user_scope, home):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if "is-enabled" in args:
            return 1, "disabled\n", ""
        if "is-active" in args:
            return 1, "inactive\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.get_hermes_home",
        lambda: Path("/var/lib/hermes-lifecycle-test"),
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

    callback = _register_gateway_start_hook(monkeypatch)
    callback(
        scope=user_scope,
        run_systemctl=fake_systemctl,
        telemetry_schema_version=1,
    )
    assert any("enable" in c and TIMER_NAME in c for c in calls)


def test_gateway_start_hook_disables_timer_when_explicitly_disabled(
    monkeypatch, user_scope
):
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
            "enabled": False,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )

    callback = _register_gateway_start_hook(monkeypatch)
    callback(
        scope=user_scope,
        run_systemctl=fake_systemctl,
        telemetry_schema_version=1,
    )
    assert any("disable" in c and TIMER_NAME in c for c in calls)
    assert not any("enable" in c for c in calls)


def test_gateway_start_hook_skips_oneshot_argv(monkeypatch, user_scope):
    calls: list[int] = []

    monkeypatch.setattr(sys, "argv", ["hermes", "auto_update", "run"])
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.reconcile_units",
        lambda *a, **k: calls.append(1),
    )

    callback = _register_gateway_start_hook(monkeypatch)
    callback(
        scope=user_scope,
        run_systemctl=lambda args: (0, "", ""),
        telemetry_schema_version=1,
    )
    assert calls == []


def test_oneshot_argv_detection():
    assert is_oneshot_run_invocation(["hermes", "auto_update", "run"]) is True
    assert is_oneshot_run_invocation(["python", "-m", "hermes_cli.main", "auto_update", "run"]) is True
    assert is_oneshot_run_invocation(["hermes", "auto_update", "reconcile"]) is False


def test_reconcile_scheduler_on_load_skips_oneshot(monkeypatch, user_scope):
    calls: list[int] = []

    monkeypatch.setattr(sys, "argv", ["hermes", "auto_update", "run"])
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.reconcile_units",
        lambda *a, **k: calls.append(1),
    )

    assert reconcile_scheduler_on_load(scope=user_scope) is None
    assert calls == []


def test_explicit_disable_stops_timer_via_lifecycle(monkeypatch, user_scope):
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
            "enabled": False,
            "schedule": "*-*-* 04,05,06,07:00:00",
            "randomized_delay_sec": 1800,
            "accuracy_sec": "1s",
        },
    )

    result = reconcile_scheduler_on_load(
        scope=user_scope,
        run_systemctl=fake_systemctl,
    )
    assert result is not None
    assert result.enabled is False
    assert any("disable" in c and TIMER_NAME in c for c in calls)


def test_disable_survives_subsequent_reconcile(monkeypatch, user_scope):
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.platform_supported", lambda: True
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
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.plugin_explicitly_disabled",
        lambda: True,
    )

    first = reconcile_scheduler_on_load(
        scope=user_scope,
        run_systemctl=fake_systemctl,
    )
    second = reconcile_scheduler_on_load(
        scope=user_scope,
        run_systemctl=fake_systemctl,
    )
    assert first is not None and first.enabled is False
    assert second is not None and second.enabled is False
    disable_calls = [c for c in calls if "disable" in c and TIMER_NAME in c]
    assert len(disable_calls) >= 2
    assert not any("enable" in c for c in calls)


def test_gateway_lifecycle_cannot_overwrite_foreign_production_units(
    tmp_path, monkeypatch
):
    """A disposable gateway home must not claim the machine-wide scheduler."""
    unit_dir = tmp_path / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    scope = InstallScope(
        system=True,
        unit_dir=unit_dir,
        systemctl_prefix=("systemctl",),
    )
    production_home = tmp_path / "production-home"
    disposable_home = tmp_path / "disposable-home"
    production_home.mkdir()
    disposable_home.mkdir()

    service_path = service_unit_path(scope)
    timer_path = timer_unit_path(scope)
    service_path.write_text(
        render_service_unit(
            hermes_home=str(production_home),
            exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
            scope=scope,
        ),
        encoding="utf-8",
    )
    timer_path.write_text(
        render_timer_unit(
            schedule="*-*-* 04:00:00",
            randomized_delay_sec=0,
            accuracy_sec="1s",
        ),
        encoding="utf-8",
    )
    production_service_bytes = service_path.read_bytes()
    production_timer_bytes = timer_path.read_bytes()
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        if "is-enabled" in args:
            return 0, "enabled\n", ""
        if "is-active" in args:
            return 0, "active\n", ""
        return 0, "", ""

    monkeypatch.setattr("plugins.auto_update.lifecycle.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: False)
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {
            "enabled": True,
            "schedule": "*-*-* 00,06,12,18:00:00",
            "randomized_delay_sec": 0,
            "accuracy_sec": "1s",
        },
    )
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.systemd.get_hermes_home", lambda: disposable_home)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.migrate_legacy_units",
        lambda *_args, **_kwargs: LegacyMigrationResult((), (), (), ()),
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )

    result = reconcile_scheduler_on_load(scope=scope, run_systemctl=fake_systemctl)

    assert service_path.read_bytes() == production_service_bytes
    assert timer_path.read_bytes() == production_timer_bytes
    assert result is not None
    assert any("ownership" in warning for warning in result.warnings)
    mutations = [
        call
        for call in calls
        if {"daemon-reload", "disable", "enable", "restart", "start", "stop"}
        & set(call)
    ]
    assert mutations == []

def _patch_enabled_reconcile(monkeypatch, home, scope):
    monkeypatch.setattr("plugins.auto_update.lifecycle.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: False)
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {
            "enabled": True,
            "schedule": "*-*-* 00,06,12,18:00:00",
            "randomized_delay_sec": 0,
            "accuracy_sec": "1s",
        },
    )
    monkeypatch.setattr("plugins.auto_update.cli.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.cli._effective_enabled", lambda: True)
    monkeypatch.setattr("plugins.auto_update.systemd.platform_supported", lambda: True)
    monkeypatch.setattr("plugins.auto_update.systemd.detect_install_scope", lambda: scope)
    monkeypatch.setattr("plugins.auto_update.systemd.get_hermes_home", lambda: home)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.unit_exec_start_argv",
        lambda: ["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.migrate_legacy_units",
        lambda *_args, **_kwargs: LegacyMigrationResult((), (), (), ()),
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.duplicate_scheduler_present",
        lambda *_args, **_kwargs: False,
    )


def _write_installed_units(scope, home: str, schedule: str) -> tuple[bytes, bytes]:
    service_path = service_unit_path(scope)
    timer_path = timer_unit_path(scope)
    service_path.write_text(
        render_service_unit(
            hermes_home=home,
            exec_start=["/usr/bin/python3", "-m", "hermes_cli.main", "auto_update", "run"],
            scope=scope,
        ),
        encoding="utf-8",
    )
    timer_path.write_text(
        render_timer_unit(
            schedule=schedule,
            randomized_delay_sec=0,
            accuracy_sec="1s",
        ),
        encoding="utf-8",
    )
    return service_path.read_bytes(), timer_path.read_bytes()


def _active_systemctl(calls: list[list[str]]):
    def fake(args):
        calls.append(list(args))
        if "is-enabled" in args:
            return 0, "enabled\n", ""
        if "is-active" in args:
            return 0, "active\n", ""
        return 0, "", ""

    return fake


def test_same_owner_reconcile_backfills_ownership(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "state"
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    home = "/var/lib/hermes production%blue"
    original_service, _ = _write_installed_units(scope, home, "*-*-* 04:00:00")
    calls: list[list[str]] = []
    _patch_enabled_reconcile(monkeypatch, Path(home), scope)

    result = reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=_active_systemctl(calls),
        state_dir=state_dir,
    )

    assert result is not None and result.changed is True
    assert service_unit_path(scope).read_bytes() == original_service
    assert "OnCalendar=*-*-* 00,06,12,18:00:00" in timer_unit_path(scope).read_text(
        encoding="utf-8"
    )
    ownership = json.loads(
        (state_dir / "hermes-auto-updater.owner.json").read_text(encoding="utf-8")
    )
    assert ownership["hermes_home"] == home
    assert any("daemon-reload" in call for call in calls)


def test_disposable_home_cannot_claim_unowned_scheduler(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    disposable_home = Path("/tmp/hermes-auto-update-disposable-owner")
    calls: list[list[str]] = []
    _patch_enabled_reconcile(monkeypatch, disposable_home, scope)

    result = reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=_active_systemctl(calls),
    )

    assert result is not None
    assert any("disposable" in warning for warning in result.warnings)
    assert not service_unit_path(scope).exists()
    assert not timer_unit_path(scope).exists()
    assert not (unit_dir / "hermes-auto-updater.owner.json").exists()
    assert calls == []


def test_conflicting_record_and_unit_ownership_refuses_mutation(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    current_home = "/var/lib/hermes-current"
    original_service, original_timer = _write_installed_units(
        scope, "/var/lib/hermes-unit-owner", "*-*-* 04:00:00"
    )
    (unit_dir / "hermes-auto-updater.owner.json").write_text(
        json.dumps({"version": 1, "hermes_home": current_home}),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    _patch_enabled_reconcile(monkeypatch, Path(current_home), scope)

    result = reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=_active_systemctl(calls),
    )

    assert result is not None
    assert any("disagrees" in warning for warning in result.warnings)
    assert service_unit_path(scope).read_bytes() == original_service
    assert timer_unit_path(scope).read_bytes() == original_timer
    assert calls == []


def test_disabled_foreign_profile_does_not_stop_production_timer(
    tmp_path, monkeypatch
):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    original_service, original_timer = _write_installed_units(
        scope, "/var/lib/hermes-production", "*-*-* 04:00:00"
    )
    calls: list[list[str]] = []
    _patch_enabled_reconcile(
        monkeypatch, Path("/tmp/hermes-disabled-foreign"), scope
    )
    monkeypatch.setattr("plugins.auto_update.lifecycle.plugin_explicitly_disabled", lambda: True)
    monkeypatch.setattr(
        "plugins.auto_update.lifecycle.load_auto_update_config",
        lambda: {
            "enabled": False,
            "schedule": "*-*-* 00,06,12,18:00:00",
            "randomized_delay_sec": 0,
            "accuracy_sec": "1s",
        },
    )

    result = reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=_active_systemctl(calls),
    )

    assert result is not None
    assert any("ownership" in warning for warning in result.warnings)
    assert service_unit_path(scope).read_bytes() == original_service
    assert timer_unit_path(scope).read_bytes() == original_timer
    assert calls == []


def test_temp_root_equal_to_os_home_is_disposable(tmp_path, monkeypatch):
    from plugins.auto_update.ownership import is_disposable_home

    monkeypatch.setattr(
        "plugins.auto_update.ownership.tempfile.gettempdir", lambda: str(tmp_path)
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert is_disposable_home(tmp_path / "profile")
    assert not is_disposable_home("/var/lib/hermes-persistent")


def test_failed_takeover_state_write_preserves_units_and_record(
    tmp_path, monkeypatch
):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    old_home = "/var/lib/hermes-production"
    original_service, original_timer = _write_installed_units(
        scope, old_home, "*-*-* 04:00:00"
    )
    state_path = unit_dir / "hermes-auto-updater.owner.json"
    original_state = json.dumps({"version": 1, "hermes_home": old_home})
    state_path.write_text(original_state, encoding="utf-8")
    calls: list[list[str]] = []
    _patch_enabled_reconcile(
        monkeypatch, Path("/var/lib/hermes-replacement"), scope
    )

    def fail_replace(_source, _destination):
        raise OSError("simulated ownership write failure")

    monkeypatch.setattr("plugins.auto_update.ownership.os.replace", fail_replace)
    result = reconcile_scheduler_on_load(
        scope=scope,
        run_systemctl=_active_systemctl(calls),
        takeover=True,
    )

    assert result is not None
    assert any("failed to persist" in warning for warning in result.warnings)
    assert state_path.read_text(encoding="utf-8") == original_state
    assert service_unit_path(scope).read_bytes() == original_service
    assert timer_unit_path(scope).read_bytes() == original_timer
    assert list(unit_dir.glob(".hermes-auto-updater.owner.json.*.tmp")) == []
    assert calls == []


def test_cli_reconcile_takeover_replaces_foreign_owner(tmp_path, monkeypatch, capsys):
    from plugins.auto_update.cli import auto_update_command, register_management_cli

    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = InstallScope(system=True, unit_dir=unit_dir, systemctl_prefix=("systemctl",))
    old_home = "/var/lib/hermes-production"
    new_home = "/tmp/hermes-manual-takeover"
    _write_installed_units(scope, old_home, "*-*-* 04:00:00")
    (unit_dir / "hermes-auto-updater.owner.json").write_text(
        json.dumps({"version": 1, "hermes_home": old_home}),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    _patch_enabled_reconcile(monkeypatch, Path(new_home), scope)
    monkeypatch.setattr(
        "plugins.auto_update.systemd.default_systemctl_runner",
        _active_systemctl(calls),
    )

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    register_management_cli(commands.add_parser("auto_update"))
    args = parser.parse_args(["auto_update", "reconcile", "--takeover"])

    assert auto_update_command(args) == 0
    capsys.readouterr()
    assert f"Environment=HERMES_HOME={new_home}" in service_unit_path(scope).read_text(
        encoding="utf-8"
    )
    ownership = json.loads(
        (unit_dir / "hermes-auto-updater.owner.json").read_text(encoding="utf-8")
    )
    assert ownership["hermes_home"] == new_home
    assert any("enable" in call and TIMER_NAME in call for call in calls)

def test_oneshot_entrypoint_busy_tick_prepares_but_touches_nothing(home, monkeypatch):
    """A busy tick still prepares (Phase A is not idle-gated) — but the tick
    process itself mutates nothing: no systemd reconcile, no migration, no
    writes outside the updater subprocesses' own scope, and the activation is
    a dispatch (Phase B), never an in-process restart."""
    db = SessionDB(db_path=home / "state.db")
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO session_turn_leases
            (conversation_id, holder, acquired_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        ("conv-live", "holder-1", now, now + 60.0),
    )
    db._conn.commit()
    leases_before = db._conn.execute("SELECT * FROM session_turn_leases").fetchall()
    db.close()

    sentinel = home / "auto-update" / "sentinel.bin"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"UNCHANGED")
    side_effects = {"reconcile": 0, "migrate": 0}
    dispatched: list[str] = []

    def fake_run_subprocess(argv):
        joined = " ".join(str(tok) for tok in argv)
        dispatched.append(joined)
        if "--check" in joined:
            stdout = "⚕ Update available: 3 new commits\n"
        elif "--defer-restart" in joined:
            stdout = "✓ Code updated!\n✓ Update prepared — fleet restart deferred.\n"
        elif "activate" in joined:
            stdout = "→ Hermes is busy (session_turn_lease) — prepared update stays pending.\n"
        else:
            raise AssertionError(f"unexpected updater dispatch: {joined!r}")
        return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")

    monkeypatch.setattr(sys, "argv", ["hermes", "auto_update", "run"])
    monkeypatch.setattr(
        "plugins.auto_update.systemd.reconcile_units",
        lambda *a, **k: side_effects.__setitem__(
            "reconcile", side_effects["reconcile"] + 1
        )
        or ReconcileResult(
            supported=True,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(),
        ),
    )
    monkeypatch.setattr(
        "plugins.auto_update.systemd.migrate_legacy_units",
        lambda *a, **k: side_effects.__setitem__(
            "migrate", side_effects["migrate"] + 1
        )
        or LegacyMigrationResult((), (), (), ()),
    )
    monkeypatch.setattr(
        "plugins.auto_update.runner.run_subprocess", fake_run_subprocess
    )
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)

    from plugins.auto_update import register

    class Ctx:
        def register_cli_command(self, **kwargs):
            return None

        def register_hook(self, hook_name, callback):
            return None

    register(Ctx())
    assert reconcile_scheduler_on_load() is None

    from plugins.auto_update.cli import cmd_run

    assert cmd_run() == 0
    # Busy is no reason to skip preparation — and activation is still
    # attempted (in the dispatched subprocess, which found Hermes busy).
    assert len(dispatched) == 3
    assert "update --check" in dispatched[0]
    assert "update --yes --defer-restart" in dispatched[1]
    assert "auto_update activate" in dispatched[2]
    # The tick itself stayed read-only toward the host and the profile state.
    assert side_effects == {"reconcile": 0, "migrate": 0}
    assert sentinel.read_bytes() == b"UNCHANGED"
    db = SessionDB(db_path=home / "state.db")
    leases_after = db._conn.execute("SELECT * FROM session_turn_leases").fetchall()
    db.close()
    assert leases_after == leases_before
