"""Two-phase tick behavior and the stock updater argv boundary.

One tick = prepare (always; never idle-gated) + activate (always dispatched,
own fresh process). Idleness is deliberately NOT consulted here — the
activation subcommand re-checks it immediately before any restart.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from hermes_cli.update_lock import UpdateHolder
from hermes_state import SessionDB
from plugins.auto_update.runner import (
    ACTIVATE_ARGV,
    UPDATE_APPLY_ARGV,
    UPDATE_CHECK_ARGV,
    UPDATE_PREPARE_ARGV,
    _check_output_indicates_update_available,
    build_stock_updater_argv,
    run_scheduled_update,
)

UPDATE_AVAILABLE_STDOUT = "⚕ Update available: 1 commit behind origin/main.\n"
UP_TO_DATE_STDOUT = "✓ Already up to date.\n"

BASE_CFG = {
    "enabled": True,
    "idle_minutes": 8,
    "notify_on_success": "",
    "notify_on_failure": "",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.plugin_explicitly_disabled", lambda: False
    )


def _mode(argv) -> str:
    """Classify a dispatched argv by the public CLI surface it targets."""
    tokens = [str(tok) for tok in argv]
    if "--check" in tokens or "check" in tokens:
        return "check"
    if "--defer-restart" in tokens or "prepare" in tokens:
        return "prepare"
    if ACTIVATE_ARGV[-1] in tokens:
        return "activate"
    return "apply"


class SimpleResult:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok(stdout=""):
    return SimpleResult(0, stdout, "")


def _scripted(monkeypatch, responses: dict, *, activation_responses=None):
    """run_cmd/run_activation fake driven by a {mode: result} mapping."""
    calls: list[str] = []
    activation_calls: list[str] = []

    def _run(mapping, record, argv):
        mode = _mode(argv)
        record.append(mode)
        result = mapping.get(mode)
        if isinstance(result, BaseException):
            raise result
        if result is None:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    monkeypatch.setattr(
        "plugins.auto_update.runner.build_stock_updater_argv",
        lambda mode: ["hermes", mode],
    )
    responses = dict(responses)
    if activation_responses is not None:
        responses["activate"] = activation_responses

    def run_cmd(argv):
        return _run(responses, calls, argv)

    def run_activation(argv):
        return _run({"activate": responses.get("activate")}, activation_calls, argv)

    return run_cmd, run_activation, calls, activation_calls


# ---------------------------------------------------------------------------
# Public argv boundary
# ---------------------------------------------------------------------------


def test_build_stock_updater_argv_uses_public_subcommand(monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.resolve_hermes_bin", lambda: "/usr/local/bin/hermes"
    )
    assert build_stock_updater_argv("check") == [
        "/usr/local/bin/hermes",
        *UPDATE_CHECK_ARGV,
    ]
    assert build_stock_updater_argv("apply") == [
        "/usr/local/bin/hermes",
        *UPDATE_APPLY_ARGV,
    ]
    assert build_stock_updater_argv("prepare") == [
        "/usr/local/bin/hermes",
        *UPDATE_PREPARE_ARGV,
    ]
    assert build_stock_updater_argv("activate") == [
        "/usr/local/bin/hermes",
        *ACTIVATE_ARGV,
    ]


def test_build_stock_updater_argv_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.resolve_hermes_bin", lambda: "/usr/local/bin/hermes"
    )
    with pytest.raises(ValueError):
        build_stock_updater_argv("nonsense")


def test_behind_substring_without_marker_is_not_available():
    text = "  This checkout is 5 commit(s) BEHIND origin/main"
    assert _check_output_indicates_update_available(text) is False


def test_update_available_marker_is_detected():
    text = "⚕ Update available: 2 commits behind origin/main."
    assert _check_output_indicates_update_available(text) is True


# ---------------------------------------------------------------------------
# Gates: disabled / live update / lock contention
# ---------------------------------------------------------------------------


def test_runner_enabled_string_false_is_quiet_noop(home, enabled, monkeypatch):
    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "enabled": "false"},
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
        run_activation=lambda argv: pytest.fail("must not invoke activation"),
    )
    assert outcome.reason == "disabled"


def test_runner_live_update_defers(home, enabled, monkeypatch):
    monkeypatch.setattr(
        "plugins.auto_update.runner.read_live_update",
        lambda: UpdateHolder(pid=999, age_seconds=1.0),
    )
    outcome = run_scheduled_update(
        read_live_update_fn=lambda: UpdateHolder(pid=999, age_seconds=1.0),
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
        run_activation=lambda argv: pytest.fail("must not invoke activation"),
    )
    assert outcome.reason == "update_in_progress"


def test_runner_lock_contention_defers(home, enabled, monkeypatch):
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    from contextlib import contextmanager

    @contextmanager
    def locked_false():
        yield False

    monkeypatch.setattr(
        "plugins.auto_update.runner.nonblocking_run_lock", locked_false
    )
    outcome = run_scheduled_update(
        cfg=BASE_CFG,
        run_cmd=lambda argv: pytest.fail("must not invoke updater"),
        run_activation=lambda argv: pytest.fail("must not invoke activation"),
    )
    assert outcome.reason == "lock_contention"


# ---------------------------------------------------------------------------
# Phase A: prepare always runs — busy is no longer a gate
# ---------------------------------------------------------------------------


def test_runner_prepares_and_dispatches_activation_on_update(home, enabled, monkeypatch):
    """One tick with an update available: check → deferred prepare → activate."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {"check": _ok(UPDATE_AVAILABLE_STDOUT), "prepare": _ok("deferred")},
    )

    outcome = run_scheduled_update(
        cfg=BASE_CFG, run_cmd=run_cmd, run_activation=run_activation
    )

    assert outcome.code == 0
    # The prepare argv is the deferred one — a bare apply would restart.
    assert calls == ["check", "prepare"]
    assert activation_calls == ["activate"]


def test_runner_no_update_tick_still_dispatches_activation(home, enabled, monkeypatch):
    """Phase B runs even when this tick prepared nothing."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch, {"check": _ok(UP_TO_DATE_STDOUT)}
    )

    outcome = run_scheduled_update(
        cfg=BASE_CFG, run_cmd=run_cmd, run_activation=run_activation
    )

    assert outcome.code == 0
    assert calls == ["check"]
    assert activation_calls == ["activate"]


def test_runner_check_failure_never_dispatches_activation(home, enabled, monkeypatch):
    """A quiet check failure is a failed tick, never an activation trigger.

    Not knowing whether an update exists is not "no update": the tick must
    not paper over a dead check with exit 0 and then restart the fleet onto
    whatever happens to be pending. An older prepared update reconciles on
    a later tick whose check actually succeeded (the ``no_update`` path).
    """
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch, {"check": SimpleResult(1, "", "boom")}
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "check failed"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code != 0
    assert outcome.reason == "check_failed"
    assert calls == ["check"]
    assert activation_calls == []
    assert emitted == ["check failed"]


def test_runner_nonzero_check_with_update_marker_still_fails(home, enabled, monkeypatch):
    """A nonzero check rc fails the tick even when the output names an update.

    The stock ``update --check`` reports availability with exit 0, so a
    nonzero rc can never legitimately mean "update available" — whatever
    wrote that marker also failed. Preparing on the strength of output text
    alone would stage an update the check never actually confirmed, so the
    rc wins: failure notification, same nonzero code, no prepare, no
    activation.
    """
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {"check": SimpleResult(3, UPDATE_AVAILABLE_STDOUT, "partial remote failure")},
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "check failed"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code == 3
    assert outcome.reason == "check_failed"
    assert calls == ["check"]
    assert activation_calls == []
    assert emitted == ["check failed"]


def test_runner_prepares_while_a_delegation_is_live(home, enabled, monkeypatch):
    """Busy (live delegated agent) no longer stops the check — only activation."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    db_path = home / "state.db"
    SessionDB(db_path=db_path)
    now = time.time()
    db = SessionDB(db_path=db_path)
    db._conn.execute(
        """
        INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("delegation-dispatched", "sess-1", "dispatched", now, now),
    )
    db._conn.commit()
    row_before = db._conn.execute(
        "SELECT delegation_id, state FROM async_delegations"
    ).fetchall()
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch, {"check": _ok(UPDATE_AVAILABLE_STDOUT), "prepare": _ok("")}
    )

    outcome = run_scheduled_update(cfg=BASE_CFG, run_cmd=run_cmd, run_activation=run_activation)

    assert outcome.code == 0
    assert calls == ["check", "prepare"]
    assert activation_calls == ["activate"]
    # The tick itself never consults (or mutates) session state.
    db_after = SessionDB(db_path=db_path)
    rows_after = db_after._conn.execute(
        "SELECT delegation_id, state FROM async_delegations"
    ).fetchall()
    assert rows_after == row_before


# ---------------------------------------------------------------------------
# Preparation failures / timeouts never activate
# ---------------------------------------------------------------------------


def test_runner_prepare_failure_skips_activation(home, enabled, monkeypatch):
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {
            "check": _ok(UPDATE_AVAILABLE_STDOUT),
            "prepare": SimpleResult(1, "", "broken"),
        },
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "prepare failed"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code != 0
    assert outcome.reason == "prepare_failed"
    assert calls == ["check", "prepare"]
    assert activation_calls == []
    assert emitted == ["prepare failed"]


def test_runner_prepare_timeout_skips_activation(home, enabled, monkeypatch):
    """A prepare that never finished is not a completed update — no restart."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {
            "check": _ok(UPDATE_AVAILABLE_STDOUT),
            "prepare": subprocess.TimeoutExpired(["hermes"], 3600),
        },
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "timeout"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code == 1
    assert outcome.reason == "prepare_timeout"
    assert calls == ["check", "prepare"]
    assert activation_calls == []
    assert emitted == ["timeout"]


def test_runner_check_timeout_is_nonzero_and_suppresses_activation(
    home, enabled, monkeypatch
):
    """Not knowing whether an update exists is not "no update"."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {"check": subprocess.TimeoutExpired(["hermes"], 3600)},
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "timeout"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code == 1
    assert outcome.reason == "check_timeout"
    assert calls == ["check"]
    assert activation_calls == []
    assert emitted == ["timeout"]


def test_runner_prepare_timeout_does_not_disturb_an_older_prepared_generation(
    home, enabled, monkeypatch
):
    """A timed-out re-prepare must not erase or replace a valid generation."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    # The two-file shape a completed preparation leaves: the generic
    # obligation and the authoritative prepared record.
    marker = home / "fleet_restart_pending"
    record = home / "fleet_restart_prepared"
    marker_body = "started=1\npid=2\nexpected_sha=" + "a" * 40 + "\n"
    record_body = (
        "schema=1\ngeneration=cafe1234cafe1234cafe1234cafe1234\n"
        f"expected_sha={'a' * 40}\nreceipt=update_x.json\n"
        "prepared=yes\nrestart=pending\nprepared_at=1\npid=2\n"
    )
    marker.write_text(marker_body, encoding="utf-8")
    record.write_text(record_body, encoding="utf-8")

    run_cmd, _run_activation, _calls, _activation_calls = _scripted(
        monkeypatch,
        {
            "check": _ok(UPDATE_AVAILABLE_STDOUT),
            "prepare": subprocess.TimeoutExpired(["hermes"], 3600),
        },
    )
    outcome = run_scheduled_update(cfg=BASE_CFG, run_cmd=run_cmd)

    assert outcome.code == 1
    # The runner never touches updater state itself, and the reaped child
    # cannot have published readiness after the parent reported the timeout:
    # both files are exactly what the next activation will strictly parse.
    assert marker.read_text(encoding="utf-8") == marker_body
    assert record.read_text(encoding="utf-8") == record_body


def test_runner_success_and_failure_notifications(home, enabled, monkeypatch):
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, _ = _scripted(
        monkeypatch,
        {"check": _ok(UPDATE_AVAILABLE_STDOUT), "prepare": _ok("")},
    )

    ok = run_scheduled_update(
        cfg={
            **BASE_CFG,
            "notify_on_success": "prepared ok",
            "notify_on_failure": "prepared failed",
        },
        run_cmd=run_cmd,
        run_activation=run_activation,
    )
    assert ok.code == 0
    assert emitted == ["prepared ok"]

    run_cmd2, run_activation2, _, _ = _scripted(
        monkeypatch,
        {
            "check": _ok(UPDATE_AVAILABLE_STDOUT),
            "prepare": SimpleResult(1, "", "fail"),
        },
    )
    bad = run_scheduled_update(
        cfg={
            **BASE_CFG,
            "notify_on_success": "prepared ok",
            "notify_on_failure": "prepared failed",
        },
        run_cmd=run_cmd2,
        run_activation=run_activation2,
    )
    assert bad.reason == "prepare_failed"
    assert emitted[-1] == "prepared failed"


# ---------------------------------------------------------------------------
# Phase B outcomes
# ---------------------------------------------------------------------------


def test_runner_activation_failure_notifies_nonzero(home, enabled, monkeypatch):
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        "plugins.auto_update.runner.emit_notification",
        lambda msg, **kw: emitted.append(msg),
    )
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {"check": _ok(UP_TO_DATE_STDOUT), "activate": SimpleResult(1, "", "boom")},
    )

    outcome = run_scheduled_update(
        cfg={**BASE_CFG, "notify_on_failure": "activation failed"},
        run_cmd=run_cmd,
        run_activation=run_activation,
    )

    assert outcome.code == 1
    assert outcome.reason == "activation_failed"
    assert activation_calls == ["activate"]
    assert emitted == ["activation failed"]


def test_runner_activation_timeout_is_nonzero(home, enabled, monkeypatch):
    """A killed activation is an unknown outcome, never a success."""
    monkeypatch.setattr("plugins.auto_update.runner.read_live_update", lambda: None)
    run_cmd, run_activation, calls, activation_calls = _scripted(
        monkeypatch,
        {
            "check": _ok(UP_TO_DATE_STDOUT),
            "activate": subprocess.TimeoutExpired(["hermes"], 3600),
        },
    )

    outcome = run_scheduled_update(
        cfg=BASE_CFG, run_cmd=run_cmd, run_activation=run_activation
    )

    assert outcome.code == 1
    assert outcome.reason == "activation_timeout"
    assert activation_calls == ["activate"]


# ---------------------------------------------------------------------------
# Boundary invariants
# ---------------------------------------------------------------------------


def test_runner_stock_updater_boundary():
    import plugins.auto_update.runner as runner

    assert UPDATE_CHECK_ARGV == ("update", "--check")
    assert UPDATE_APPLY_ARGV == ("update", "--yes")
    assert UPDATE_PREPARE_ARGV == ("update", "--yes", "--defer-restart")
    assert ACTIVATE_ARGV == ("auto_update", "activate")
    assert callable(runner.build_stock_updater_argv)
    assert not hasattr(runner, "_cmd_update_impl")
