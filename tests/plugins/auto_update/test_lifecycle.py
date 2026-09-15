"""Lifecycle hook and oneshot entrypoint invariants."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from hermes_state import SessionDB
from plugins.auto_update.legacy import LegacyMigrationResult
from plugins.auto_update.lifecycle import is_oneshot_run_invocation, reconcile_scheduler_on_load
from plugins.auto_update.platform import InstallScope
from plugins.auto_update.systemd import ReconcileResult, TIMER_NAME


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
