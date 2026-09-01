"""``hermes auto_update activate`` — the idle-gated activation half of a tick.

Contract: nothing pending → silent no-op; pending without a *durably published*
prepared generation behind it → exit 1, nothing restarted; pending + prepared +
busy → exit 0, marker kept; pending + prepared + idle → the stock updater lock
is taken, idleness is re-checked under it, and the strict activation re-reads
every piece of durable state before it restarts anything — clearing the
obligation only when the live fleet demonstrably serves the prepared code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from plugins.auto_update import cli as auto_cli
from plugins.auto_update.idle import IdleBlocker, IdleSnapshot

SETTINGS = {"enabled": True, "idle_minutes": 8}

#: A full 40-hex git object name — the only shape the strict parse accepts.
PREPARED_SHA = "a" * 40
MOVED_SHA = "b" * 40

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _publish_prepared(monkeypatch, *, runtimes, sha: str = PREPARED_SHA):
    """Drive the REAL publication machinery for one prepared generation.

    Exactly what a completed ``hermes update --defer-restart`` leaves behind:
    the pull-time obligation, then the receipt finalized with the generation
    bound to it, then the marker atomically swapped in and read back.
    """
    from hermes_cli import update_receipt as ur
    from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan, record_plan_in_receipt

    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: sha)
    assert update_cmd._write_fleet_restart_pending_marker(expected_sha=sha)
    ur.begin_update_receipt()
    record_plan_in_receipt(UpdatePlan(runtimes=[RuntimeRecord(**r) for r in runtimes]))
    ok, reason = update_cmd._publish_prepared_generation()
    assert ok, reason
    return update_cmd._parse_prepared_generation()


def _marker() -> Path:
    return get_hermes_home() / "fleet_restart_pending"


def _prepared_record() -> Path:
    return get_hermes_home() / "fleet_restart_prepared"


#: Install id stamped into every hand-built ledger entry below, matching the
#: monkeypatched ``install_id`` so the real reader keeps them.
LEDGER_INSTALL = "test-install"


def _ledger_entry(pid: int, *, host: str, port: int) -> dict:
    """One spawn-ledger entry, shaped exactly like ``register_self`` writes."""
    return {
        "pid": pid,
        "create_time": 1700000000.0,
        "purpose": "serve",
        "install": LEDGER_INSTALL,
        "spawner_pid": None,
        "spawner_create": None,
        "registered_at": 1700000000.0,
        "argv": "python -m hermes_cli.main serve",
        "host": host,
        "port": port,
        "profile": "default",
    }


def _write_spawn_ledger(path: Path, entries: list[dict]) -> None:
    """A real ledger file: the JSON list the ledger's only writer emits."""
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


@pytest.fixture
def wiring(monkeypatch, home):
    """A valid prepared generation on disk + spies on the fleet boundary."""
    calls = {"pending": 0, "restart": 0}
    state = {"pending": True, "fleet": [], "ledger": []}
    generation = _publish_prepared(monkeypatch, runtimes=[])

    def _pending() -> bool:
        calls["pending"] += 1
        return state["pending"]

    def _inspect() -> dict:
        return {"fleet": list(state["fleet"]), "ledger": list(state["ledger"])}

    def _restart() -> bool:
        calls["restart"] += 1
        return True

    monkeypatch.setattr(update_cmd, "_pending_fleet_restart_needed", _pending)
    monkeypatch.setattr(update_cmd, "_inspect_live_fleet_strict", _inspect)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", _restart)
    # One verification pass per attempt: the budget exists for a real freshly
    # restarted runtime to stamp itself, which these stubs never do.
    monkeypatch.setattr(update_cmd, "_ACTIVATION_VERIFY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(
        "plugins.auto_update.cli.load_auto_update_config", lambda: dict(SETTINGS)
    )
    return SimpleNamespace(calls=calls, state=state, generation=generation)


def _set_idle(monkeypatch, snapshot):
    monkeypatch.setattr("plugins.auto_update.idle.evaluate_idle", lambda **kw: snapshot)


def test_no_pending_is_silent_noop(home, monkeypatch, wiring, capsys):
    wiring.state["pending"] = False
    _set_idle(monkeypatch, IdleSnapshot(idle=False, blockers=()))

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0
    # Idleness is only consulted when something is actually pending.
    assert wiring.calls["pending"] == 1
    assert capsys.readouterr().out == ""


def test_busy_keeps_marker_and_skips_restart(home, monkeypatch, wiring, capsys):
    _set_idle(
        monkeypatch,
        IdleSnapshot(idle=False, blockers=(IdleBlocker("streaming", "x"),)),
    )

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "busy" in capsys.readouterr().out


def test_busy_under_the_lock_keeps_the_marker(home, monkeypatch, wiring, capsys):
    """Hermes going busy between the gate and the lock still restarts nothing."""
    snapshots = iter(
        [
            IdleSnapshot(idle=True, blockers=()),
            IdleSnapshot(idle=False, blockers=(IdleBlocker("streaming", "x"),)),
        ]
    )
    monkeypatch.setattr(
        "plugins.auto_update.idle.evaluate_idle", lambda **kw: next(snapshots)
    )

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "busy" in capsys.readouterr().out


def test_idle_activates_and_clears(home, monkeypatch, wiring):
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0, "an empty plan proves nothing to restart"
    assert not _marker().exists()


def test_lock_contention_is_retryable_and_preserves_the_obligation(
    home, monkeypatch, wiring, capsys
):
    """A live stock updater owns the lock → exit 2, nothing restarted/cleared."""
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    # A REAL contention shape: a live pid holding the shared update marker.
    lock_marker = get_hermes_home() / ".hermes-update-in-progress"
    lock_marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")

    assert auto_cli.cmd_activate() == 2
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    out = capsys.readouterr().out
    assert "already running" in out


def test_the_lock_is_held_across_inspection_and_restart(home, monkeypatch, wiring):
    """No unlocked gap: the fleet inspection runs while we hold the lock."""
    _publish_prepared(
        monkeypatch, runtimes=[{"kind": "gateway", "profile": "default", "pid": 11}]
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    seen = {}

    def _inspect():
        from hermes_cli.update_lock import read_live_update

        seen["holder"] = read_live_update()
        return {"fleet": [], "ledger": []}

    monkeypatch.setattr(update_cmd, "_inspect_live_fleet_strict", _inspect)

    assert auto_cli.cmd_activate() == 1
    assert seen["holder"] is not None, "activation inspected the fleet unlocked"
    assert seen["holder"].pid == os.getpid()


# ---------------------------------------------------------------------------
# Readiness: a preparation that did not finish is never activated
# ---------------------------------------------------------------------------


def test_failed_prepare_is_not_activated_on_a_later_tick(
    home, monkeypatch, wiring, capsys
):
    """Two ticks: late prepare failure, then an up-to-date tick stays put.

    Tick 1 wrote ``fleet_restart_pending`` the moment HEAD advanced, then a
    preparation stage failed — so the obligation exists with no published
    generation and ``--check`` now reports up to date. Tick 2 must
    not restart the fleet onto that half-prepared checkout, however idle
    Hermes is.
    """
    # A failed preparation publishes nothing: the generic marker is the raw
    # pull-time shape, and no prepared record exists at all.
    _prepared_record().unlink()
    _marker().write_text(f"started=1\npid=2\nexpected_sha={PREPARED_SHA}\n", encoding="utf-8")
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file(), "the obligation stays for a real `hermes update`"
    out = capsys.readouterr().out
    assert "never fully prepared" in out
    assert "hermes update" in out


def test_pending_without_a_marker_never_activates(home, monkeypatch, wiring):
    """A skewed receipt alone proves nothing about the preparation."""
    _marker().unlink()
    _prepared_record().unlink()
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0


def test_readiness_gate_runs_before_the_idle_check(home, monkeypatch, wiring):
    """An unproven obligation is refused even while Hermes is busy."""
    _prepared_record().unlink()
    _marker().write_text("started=1\npid=2\n", encoding="utf-8")
    # evaluate_idle is imported lazily inside cmd_activate: reaching it at all
    # would mean the gate let an unprepared obligation through.
    monkeypatch.setattr(
        "plugins.auto_update.idle.evaluate_idle",
        lambda **kw: pytest.fail("idleness must not be consulted yet"),
    )

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0


def test_incomplete_restart_is_nonzero_and_keeps_the_obligation(
    home, monkeypatch, wiring, capsys
):
    """The restart command itself failed → nonzero, marker kept."""
    _publish_prepared(
        monkeypatch, runtimes=[{"kind": "gateway", "profile": "default", "pid": 11}]
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: False)

    assert auto_cli.cmd_activate() == 1
    assert _marker().is_file()
    assert "incomplete" in capsys.readouterr().out


def test_repeated_activation_after_a_partial_restart_is_idempotent(
    home, monkeypatch, wiring
):
    """A restart that never converges keeps the obligation, tick after tick."""
    _publish_prepared(
        monkeypatch, runtimes=[{"kind": "gateway", "profile": "default", "pid": 11}]
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    # One planned gateway that never comes back on the prepared generation.
    wiring.state["fleet"] = [
        {"profile": "default", "pid": 12, "code_sha": MOVED_SHA, "state": "stale"}
    ]

    for _ in range(3):
        assert auto_cli.cmd_activate() == 1
        assert _marker().is_file()
    assert wiring.calls["restart"] == 3


def test_real_busy_fleet_leaves_marker_untouched(home, monkeypatch, wiring, capsys):
    """Live delegated agent + a real marker: exit 0, nothing restarted."""
    # Real evaluate_idle against a real state.db with a dispatched delegation.
    db = SessionDB(db_path=home / "state.db")
    now = time.time()
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-1", "sess-1", "dispatched", now, now),
    )
    db._conn.commit()

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "busy" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Strict verification: what counts as "the fleet serves the prepared code"
# ---------------------------------------------------------------------------


def test_prepared_update_reconciles_current_fleet_without_restarting(
    home, monkeypatch, capsys
):
    """Manual `/restart` after a prepare: clear the marker, restart nothing."""
    _publish_prepared(
        monkeypatch,
        runtimes=[{"kind": "gateway", "profile": "default", "pid": 11}],
    )
    restarts = {"n": 0}

    def _inspect():
        return {
            "fleet": [
                {
                    "profile": "default",
                    "pid": 12,
                    "code_sha": PREPARED_SHA,
                    "state": "current",
                }
            ],
            "ledger": [],
        }

    monkeypatch.setattr(update_cmd, "_inspect_live_fleet_strict", _inspect)
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )
    monkeypatch.setattr(
        "plugins.auto_update.cli.load_auto_update_config", lambda: dict(SETTINGS)
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    assert auto_cli.cmd_activate() == 0

    assert restarts["n"] == 0, "the fleet already serves the prepared code"
    assert not _marker().exists()
    assert "clearing the pending restart" in capsys.readouterr().out


def test_missing_planned_gateway_is_not_success(home, monkeypatch, wiring, capsys):
    """A planned profile the live fleet no longer shows cannot be cleared."""
    _publish_prepared(
        monkeypatch, runtimes=[{"kind": "gateway", "profile": "default", "pid": 11}]
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    code = auto_cli.cmd_activate()

    assert code == 1
    assert _marker().is_file()
    assert wiring.calls["restart"] == 1
    out = capsys.readouterr().out
    assert "no live gateway for profile 'default'" in out


def test_head_moved_since_prepare_refuses_to_restart(home, monkeypatch, wiring, capsys):
    """A checkout that moved on is not the generation the receipt proves."""
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: MOVED_SHA)

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert _prepared_record().is_file(), "a refusal clears nothing"
    out = capsys.readouterr().out
    assert "no longer matches the prepared update" in out


def test_missing_bound_receipt_refuses_to_restart(home, monkeypatch, wiring, capsys):
    """The generation's receipt is the proof; without it, nothing restarts."""
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))
    (get_hermes_home() / "logs" / "update_receipts" / wiring.generation.receipt).unlink()

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "never fully prepared" in capsys.readouterr().out


def test_strict_activation_rejects_a_receipt_that_disagrees(
    home, monkeypatch, wiring, capsys
):
    """A receipt binding a different generation/SHA proves nothing.

    The early gate refuses this before idleness is even consulted; this runs
    the strict activation body itself, which must reach the same refusal
    under the lock from its own receipt validation.
    """
    receipt_path = (
        get_hermes_home() / "logs" / "update_receipts" / wiring.generation.receipt
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["prepared_generation"]["expected_sha"] = MOVED_SHA
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert update_cmd._activate_pending_fleet_restart_strict() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "missing or unreadable" in capsys.readouterr().out


def test_strict_activation_rejects_a_missing_plan(home, monkeypatch, wiring, capsys):
    """A receipt with no plan is no evidence — not an empty plan."""
    receipt_path = (
        get_hermes_home() / "logs" / "update_receipts" / wiring.generation.receipt
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("plan", None)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert update_cmd._activate_pending_fleet_restart_strict() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "runtime plan cannot be verified" in capsys.readouterr().out


def test_failed_inspection_is_not_success(home, monkeypatch, wiring, capsys):
    """A fleet probe that raises leaves everything exactly as it was."""
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    def _boom():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(update_cmd, "_inspect_live_fleet_strict", _boom)

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 0
    assert _marker().is_file()
    assert "Could not inspect the running fleet" in capsys.readouterr().out


def test_managed_serve_backend_must_come_back(
    home, monkeypatch, wiring, capsys
):
    """A systemd-supervised serve backend is updater-managed: prove it moved."""
    _publish_prepared(
        monkeypatch,
        runtimes=[
            {
                "kind": "serve",
                "profile": "default",
                "pid": 4242,
                "supervisor": "systemd",
            }
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    # The restart ran but the planned pid is still the live one.
    wiring.state["ledger"] = [{"purpose": "serve", "profile": "default", "pid": 4242}]
    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 1
    assert _marker().is_file()
    assert "still runs the pre-update process" in capsys.readouterr().out

    # And a backend that never came back at all is equally not success.
    wiring.state["ledger"] = []
    assert auto_cli.cmd_activate() == 1
    assert _marker().is_file()
    out = capsys.readouterr().out
    assert "serve backend for profile 'default' is not running" in out


def test_unmanaged_serve_backend_is_not_required(home, monkeypatch, wiring):
    """A manually-launched serve is not the updater's to bounce."""
    _publish_prepared(
        monkeypatch,
        runtimes=[
            {
                "kind": "serve",
                "profile": "default",
                "pid": 4242,
                "supervisor": "manual-serve",
            },
            {
                "kind": "dashboard",
                "profile": "default",
                "pid": 4243,
                "supervisor": "desktop",
            },
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 0
    assert not _marker().exists()
    assert not _prepared_record().exists()


# ---------------------------------------------------------------------------
# Injective verification: N planned runtimes need N distinct live rows
# ---------------------------------------------------------------------------


def test_two_planned_serve_backends_require_two_distinct_live_rows(
    home, monkeypatch, wiring, capsys
):
    """Cardinality: one live replacement can never satisfy two planned rows."""
    _publish_prepared(
        monkeypatch,
        runtimes=[
            {"kind": "serve", "profile": "default", "pid": 5001, "supervisor": "systemd"},
            {"kind": "serve", "profile": "default", "pid": 5002, "supervisor": "systemd"},
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    # One compatible live row for two planned rows: fail closed, keep both
    # state files, and say exactly why.
    wiring.state["ledger"] = [{"purpose": "serve", "profile": "default", "pid": 6001}]
    assert auto_cli.cmd_activate() == 1
    assert _marker().is_file()
    assert _prepared_record().is_file()
    assert "no distinct live instance" in capsys.readouterr().out

    # Two distinct live replacements satisfy the plan injectively — the
    # fleet already serves the prepared code, so this clears with NO restart.
    wiring.state["ledger"] = [
        {"purpose": "serve", "profile": "default", "pid": 6001},
        {"purpose": "serve", "profile": "default", "pid": 6002},
    ]
    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 1, "only the failed attempt restarted"
    assert not _marker().exists()
    assert not _prepared_record().exists()


def test_two_planned_dashboards_require_two_distinct_live_rows(
    home, monkeypatch, wiring, capsys
):
    """The same cardinality rule holds for dashboard plan rows."""
    _publish_prepared(
        monkeypatch,
        runtimes=[
            {"kind": "dashboard", "profile": "default", "pid": 5101, "supervisor": "systemd"},
            {"kind": "dashboard", "profile": "default", "pid": 5102, "supervisor": "systemd"},
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    wiring.state["ledger"] = [
        {"purpose": "dashboard", "profile": "default", "pid": 6101}
    ]
    assert auto_cli.cmd_activate() == 1
    assert _prepared_record().is_file()
    assert "no distinct live instance" in capsys.readouterr().out

    wiring.state["ledger"] = [
        {"purpose": "dashboard", "profile": "default", "pid": 6101},
        {"purpose": "dashboard", "profile": "default", "pid": 6102},
    ]
    assert auto_cli.cmd_activate() == 0
    assert not _prepared_record().exists()


def test_stable_instance_identity_is_preferred_when_present(home, monkeypatch, wiring):
    """Plan rows carrying host/port match only the live row with that identity."""
    _publish_prepared(
        monkeypatch,
        runtimes=[
            {
                "kind": "serve",
                "profile": "default",
                "pid": 5201,
                "supervisor": "systemd",
                "detail": {"host": "127.0.0.1", "port": 8642},
            },
            {
                "kind": "serve",
                "profile": "default",
                "pid": 5202,
                "supervisor": "systemd",
                "detail": {"host": "127.0.0.1", "port": 8643},
            },
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    # A live backend on a DIFFERENT port is a different instance, not the
    # second plan row's replacement.
    wiring.state["ledger"] = [
        {"purpose": "serve", "profile": "default", "pid": 6201,
         "host": "127.0.0.1", "port": 8642},
        {"purpose": "serve", "profile": "default", "pid": 6202,
         "host": "127.0.0.1", "port": 9999},
    ]
    assert auto_cli.cmd_activate() == 1
    assert _prepared_record().is_file()

    wiring.state["ledger"] = [
        {"purpose": "serve", "profile": "default", "pid": 6201,
         "host": "127.0.0.1", "port": 8642},
        {"purpose": "serve", "profile": "default", "pid": 6202,
         "host": "127.0.0.1", "port": 8643},
    ]
    assert auto_cli.cmd_activate() == 0
    assert not _prepared_record().exists()


def test_one_live_pid_cannot_satisfy_two_planned_runtimes(
    home, monkeypatch, wiring, capsys, tmp_path
):
    """Duplicate ledger rows are one process, never two replacements.

    The ledger's single writer prunes a pid's rows before appending its new
    one, so two VALID rows for the same pid — here with different
    host/port identities, the shape a raced or tampered write leaves —
    cannot prove which instance that process is. Row-count matching let
    that ONE process satisfy both planned serve rows and clear the
    obligation; verification must instead report the duplicate identity
    and keep it.
    """
    from hermes_cli import process_identity

    _publish_prepared(
        monkeypatch,
        runtimes=[
            {"kind": "serve", "profile": "default", "pid": 5301, "supervisor": "systemd"},
            {"kind": "serve", "profile": "default", "pid": 5302, "supervisor": "systemd"},
        ],
    )
    _set_idle(monkeypatch, IdleSnapshot(idle=True, blockers=()))

    # The REAL ledger reader over a real ledger file whose two valid serve
    # entries claim the same pid with different identities.
    ledger_path = tmp_path / "spawn_ledger.json"
    _write_spawn_ledger(
        ledger_path,
        [
            _ledger_entry(6301, host="127.0.0.1", port=8701),
            _ledger_entry(6301, host="127.0.0.1", port=8702),
        ],
    )
    monkeypatch.setattr(process_identity, "_ledger_path", lambda: ledger_path)
    monkeypatch.setattr(process_identity, "install_id", lambda *a, **k: LEDGER_INSTALL)
    monkeypatch.setattr(
        process_identity, "_pid_alive_matches", lambda pid, create_time: True
    )

    def _inspect() -> dict:
        return {"fleet": [], "ledger": process_identity.ledger_entries()}

    monkeypatch.setattr(update_cmd, "_inspect_live_fleet_strict", _inspect)

    assert auto_cli.cmd_activate() == 1
    assert wiring.calls["restart"] == 1, "one repair restart, then refusal"
    assert _marker().is_file()
    assert _prepared_record().is_file()
    assert "conflicting rows for live pid 6301" in capsys.readouterr().out

    # Two genuinely distinct live pids ARE two replacements — the normal
    # post-restart shape clears the obligation with no second restart.
    _write_spawn_ledger(
        ledger_path,
        [
            _ledger_entry(6301, host="127.0.0.1", port=8701),
            _ledger_entry(6302, host="127.0.0.1", port=8702),
        ],
    )
    assert auto_cli.cmd_activate() == 0
    assert wiring.calls["restart"] == 1
    assert not _marker().exists()
    assert not _prepared_record().exists()


def test_duplicate_ledger_rows_are_one_process_not_two():
    """Unit-level: a pid recorded twice is one slot, never two live rows."""
    verify = update_cmd._verify_fleet_on_expected_generation
    sha = "a" * 40
    planned = [{"kind": "serve", "profile": "default", "pid": 40 + i} for i in range(2)]
    duplicate = {
        "purpose": "serve",
        "profile": "default",
        "pid": 50,
        "host": "127.0.0.1",
        "port": 8601,
    }
    # An identical replay collapses to one usable identity — still ONE
    # process, so two planned rows cannot both be satisfied by it.
    problems = verify(
        {"fleet": [], "ledger": [duplicate, dict(duplicate)]},
        gateway_profiles=set(),
        managed_serve=planned,
        expected_sha=sha,
    )
    assert problems == [
        "serve backend for profile 'default' has no distinct live instance"
        " — one live runtime cannot satisfy two planned ones"
    ]

    # Conflicting identities for one pid are an explicit duplicate-identity
    # problem, whatever row-count matching would have concluded.
    problems = verify(
        {"fleet": [], "ledger": [duplicate, {**duplicate, "port": 8602}]},
        gateway_profiles=set(),
        managed_serve=planned,
        expected_sha=sha,
    )
    assert any("conflicting rows for live pid 50" in p for p in problems)


def test_runtime_matching_is_maximum_not_first_come():
    """A greedy first-come match must not strand an identity-bound row."""
    planned = [
        {"kind": "serve", "profile": "default", "pid": 1},  # no identity
        {"kind": "serve", "profile": "default", "pid": 2,
         "detail": {"host": "127.0.0.1", "port": 8642}},
    ]
    live = [
        {"kind": "serve", "profile": "default", "pid": 101,
         "host": "127.0.0.1", "port": 8642},
        {"kind": "serve", "profile": "default", "pid": 102,
         "host": "127.0.0.1", "port": 9999},
    ]
    matching = update_cmd._match_runtimes_injectively(planned, live)
    # The identity-free row must yield the identity-bound live row to the
    # row that needs it — augmenting paths, not insertion order.
    assert matching == {0: 1, 1: 0}


def test_verify_fleet_matches_gateways_and_managed_rows_injectively():
    """Unit-level sweep: gateway, serve and dashboard rows all fail closed."""
    verify = update_cmd._verify_fleet_on_expected_generation
    sha = "a" * 40
    # Two planned gateway profiles, one live row — the missing profile fails.
    problems = verify(
        {"fleet": [
            {"profile": "default", "pid": 1, "code_sha": sha, "state": "current"}
        ], "ledger": []},
        gateway_profiles={"default", "work"},
        managed_serve=[],
        expected_sha=sha,
    )
    assert problems == ["no live gateway for profile 'work'"]
    # One compatible live row, two planned rows of each managed kind.
    planned = [
        {"kind": kind, "profile": "default", "pid": 10 + i}
        for kind in ("serve", "dashboard")
        for i in range(2)
    ]
    problems = verify(
        {"fleet": [], "ledger": [
            {"purpose": "serve", "profile": "default", "pid": 20},
            {"purpose": "dashboard", "profile": "default", "pid": 30},
        ]},
        gateway_profiles=set(),
        managed_serve=planned,
        expected_sha=sha,
    )
    assert len(problems) == 2
    assert all("no distinct live instance" in p for p in problems)
    # Two distinct live rows per kind: clean.
    problems = verify(
        {"fleet": [], "ledger": [
            {"purpose": "serve", "profile": "default", "pid": 20},
            {"purpose": "serve", "profile": "default", "pid": 21},
            {"purpose": "dashboard", "profile": "default", "pid": 30},
            {"purpose": "dashboard", "profile": "default", "pid": 31},
        ]},
        gateway_profiles=set(),
        managed_serve=planned,
        expected_sha=sha,
    )
    assert problems == []


# ---------------------------------------------------------------------------
# Durable cross-tick proof: activation in a genuinely fresh process
# ---------------------------------------------------------------------------


def _run_fresh_activate(home: Path) -> subprocess.CompletedProcess:
    """Run the public activation CLI in a brand-new interpreter.

    This is the real Phase-B boundary: a fresh process, importing this
    checkout's modules, reading only what is durable on disk.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "auto_update", "activate"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def fresh_process_home(tmp_path, monkeypatch):
    """A sandbox whose marker HEAD is the REAL checkout HEAD.

    The fresh process resolves HEAD with real git against the real
    PROJECT_ROOT, so the prepared generation is bound to this repo's actual
    HEAD — exactly the cross-process binding the schema exists to prove.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    SessionDB(db_path=home / "state.db")  # present + quiet ⇒ idle
    return home, real_head


def test_prepared_generation_survives_the_process_boundary(fresh_process_home, monkeypatch):
    """Tick 1 publishes; a fresh interpreter on a later tick activates it."""
    home, real_head = fresh_process_home
    _publish_prepared(monkeypatch, runtimes=[], sha=real_head)
    marker = home / "fleet_restart_pending"
    record = home / "fleet_restart_prepared"
    assert update_cmd._parse_prepared_generation() is not None

    result = _run_fresh_activate(home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), "the fresh process consumed the obligation"
    assert not record.exists(), "and the authoritative prepared record"


def test_torn_marker_refused_across_the_process_boundary(fresh_process_home, monkeypatch):
    """A truncated prepared record is unprepared in a fresh process too."""
    home, real_head = fresh_process_home
    _publish_prepared(monkeypatch, runtimes=[], sha=real_head)
    record = home / "fleet_restart_prepared"
    body = record.read_text(encoding="utf-8")
    record.write_text(body[: len(body) // 2], encoding="utf-8")

    result = _run_fresh_activate(home)

    assert result.returncode == 1, result.stdout + result.stderr
    assert record.is_file()
    assert "never fully prepared" in result.stdout


def test_wrong_sha_refused_across_the_process_boundary(fresh_process_home, monkeypatch):
    """A generation bound to a SHA this checkout is not on never restarts."""
    home, _real_head = fresh_process_home
    _publish_prepared(monkeypatch, runtimes=[], sha=PREPARED_SHA)
    marker = home / "fleet_restart_pending"
    record = home / "fleet_restart_prepared"

    result = _run_fresh_activate(home)

    assert result.returncode == 1, result.stdout + result.stderr
    assert marker.is_file()
    assert record.is_file(), "a refusal restarts nothing and clears nothing"
    assert "no longer matches the prepared update" in result.stdout


def test_timed_out_child_cannot_overwrite_the_prior_generation(
    fresh_process_home, monkeypatch
):
    """A re-prepare killed on timeout after its pull-time write preserves
    the older valid prepared record byte-identical — real subprocess reap.

    The child performs exactly the write a timed-out re-prepare reaches
    (the generic pull-time marker) and is then killed by the parent's
    ``subprocess.run`` timeout, which reaps it before returning. The
    authoritative record in ``fleet_restart_prepared`` must be untouched:
    only a completed preparation may replace it.
    """
    home, real_head = fresh_process_home
    _publish_prepared(monkeypatch, runtimes=[], sha=real_head)
    record = home / "fleet_restart_prepared"
    before = record.read_bytes()

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = (
        "import time\n"
        "from hermes_cli import update_cmd\n"
        "update_cmd._write_fleet_restart_pending_marker(expected_sha='f' * 40)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert record.read_bytes() == before
    # The generic obligation DID move to the new target — the two files are
    # independent, and strict activation still reads only the record.
    marker_body = (home / "fleet_restart_pending").read_text(encoding="utf-8")
    assert f"expected_sha={'f' * 40}" in marker_body
    assert update_cmd._parse_prepared_generation() is not None


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def test_activate_dispatch_wired_to_subcommand(monkeypatch):
    seen = {}

    def _fake():
        seen["ran"] = True
        return 7

    monkeypatch.setattr(auto_cli, "cmd_activate", _fake)
    assert (
        auto_cli.auto_update_command(SimpleNamespace(auto_update_command="activate"))
        == 7
    )
    assert seen == {"ran": True}


def test_activate_registered_on_parser():
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    auto_cli.register_cli(subparsers.add_parser("auto_update"))
    assert parser.parse_args(["auto_update", "activate"]).auto_update_command == "activate"
