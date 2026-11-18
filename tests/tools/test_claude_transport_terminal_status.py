"""Transport-death terminal status and run-log notice contracts for Claude delegations.

A Claude Code run whose result event carries ``terminal_reason=api_error``,
or whose result/error/log text names a transport death (ECONNRESET /
connection reset / connection closed), must be reported with terminal status
``api_error`` instead of a generic error. Interrupted and timeout verdicts
are never reclassified, and a success never gains an api_error token. The
parent-facing notice carries the run's exact jsonl path only when one was
recorded; an owner-exit recovery notice says so explicitly when none was.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

import pytest

import tools.async_delegation as ad
from tools import claude_agent_tool as cat
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    yield
    ad._reset_for_tests()


# ---------------------------------------------------------------------------
# Spawn-layer stubs
# ---------------------------------------------------------------------------

def _patch_stream(monkeypatch, *, watchdog=None, log_path="/tmp/log.jsonl",
                  log_text="", rc=0):
    """Stub the spawn layer with canned watchdog/log/exit facts.

    ``on_spawn`` fires with the log path exactly where the real runner does
    (right after spawn), so spawn-time persistence paths are exercised.
    """

    def _fake(cmd, *, workdir, timeout_seconds, log_dir, run_timestamp,
              on_spawn=None, on_proc=None):
        if on_spawn is not None:
            on_spawn(Path(log_path))
        return watchdog, log_path, log_text, 1.0, rc

    monkeypatch.setattr(cat, "_run_and_stream", _fake)


def _sync_payload(tmp_path, monkeypatch, **stream_kwargs):
    _patch_stream(monkeypatch, **stream_kwargs)
    return json.loads(cat._run_claude_agent_sync(
        ["claude-glm"], workdir=str(tmp_path), clamped_timeout=0,
        log_dir=tmp_path, run_timestamp="20261116-000000",
        goal_brief_path=None,
    ))


def _dispatch_background(monkeypatch, tmp_path, *, log_path, log_text, rc):
    _patch_stream(monkeypatch, log_path=log_path, log_text=log_text, rc=rc)
    return json.loads(cat._dispatch_claude_background(
        ["claude-glm"], workdir=str(tmp_path), clamped_timeout=0,
        log_dir=tmp_path, run_timestamp="20261116-000000",
        goal_brief_path=None, goal="ship it", model_name="glm-5.2",
    ))


def _drain_event(delegation_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            evt = process_registry.completion_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if isinstance(evt, dict) and evt.get("delegation_id") == delegation_id:
            return evt
    return None


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def test_parse_claude_agent_log_carries_terminal_reason():
    log_text = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "connection closed", "session_id": "s1", "num_turns": 2,
        "duration_ms": 1000, "total_cost_usd": 0.01,
        "terminal_reason": "api_error",
    })
    parsed = cat.parse_claude_agent_log(log_text)
    assert parsed.get("terminal_reason") == "api_error"
    # The new field is additive; existing parsed fields survive it.
    assert parsed["subtype"] == "error_during_execution"
    assert parsed["is_error"] is True
    assert parsed["session_id"] == "s1"


# ---------------------------------------------------------------------------
# classify_claude_terminal_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("existing", ["interrupted", "timeout"])
def test_classifier_never_reclassifies_interrupted_or_timeout(existing):
    assert cat.classify_claude_terminal_status(
        terminal_reason="api_error",
        texts=["read tcp: ECONNRESET"],
        existing_status=existing,
    ) == existing


def test_classifier_names_structured_api_error_reason():
    assert cat.classify_claude_terminal_status(
        terminal_reason="  API_ERROR ", texts=[], existing_status="error",
    ) == "api_error"


@pytest.mark.parametrize("text", [
    "read tcp 1.2.3.4:443->5.6.7.8:9: read: connection reset by peer",
    "ECONNRESET",
    "the stream ended: Connection Closed",
])
def test_classifier_matches_transport_death_prose(text):
    assert cat.classify_claude_terminal_status(
        terminal_reason=None, texts=[text], existing_status="error",
    ) == "api_error"


def test_classifier_leaves_generic_failure_and_success_alone():
    assert cat.classify_claude_terminal_status(
        terminal_reason=None, texts=["exit 2 after a bad flag"],
        existing_status="error",
    ) == "error"
    assert cat.classify_claude_terminal_status(
        terminal_reason=None, texts=["all tests pass"],
        existing_status="completed",
    ) == "completed"


# ---------------------------------------------------------------------------
# _run_claude_agent_sync
# ---------------------------------------------------------------------------

def test_nonzero_exit_with_econnreset_log_is_api_error(tmp_path, monkeypatch):
    log_text = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "read tcp: ECONNRESET",
    })
    payload = _sync_payload(tmp_path, monkeypatch,
                            log_path="/tmp/sept-econn.jsonl",
                            log_text=log_text, rc=1)
    assert payload["success"] is False
    assert payload.get("terminal_status") == "api_error"
    assert payload["error"].startswith("Claude Code exited with code 1")
    assert payload["log_path"] == "/tmp/sept-econn.jsonl"


def test_terminal_reason_api_error_with_closed_connection(tmp_path, monkeypatch):
    log_text = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "connection closed", "terminal_reason": "api_error",
    })
    payload = _sync_payload(tmp_path, monkeypatch,
                            log_path="/tmp/sept-closed.jsonl",
                            log_text=log_text, rc=1)
    assert payload.get("terminal_status") == "api_error"


def test_success_result_stays_success_without_api_error_token(tmp_path, monkeypatch):
    log_text = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "done",
    })
    payload = _sync_payload(tmp_path, monkeypatch, log_text=log_text, rc=0)
    assert payload["success"] is True
    assert payload.get("terminal_status") != "api_error"


@pytest.mark.parametrize("token", ["timeout", "interrupted"])
def test_watchdog_verdicts_survive_closed_connection_text(
        tmp_path, monkeypatch, token):
    log_text = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "connection closed",
    })
    payload = _sync_payload(tmp_path, monkeypatch, watchdog=token,
                            log_text=log_text, rc=1)
    assert payload["error"] == token
    assert payload.get("terminal_status") != "api_error"


# ---------------------------------------------------------------------------
# Background status collapse
# ---------------------------------------------------------------------------

def test_background_status_names_transport_death():
    payload = {
        "success": False,
        "error": "Claude Code exited with code 1: read tcp: ECONNRESET",
        "terminal_status": "api_error",
    }
    assert cat._claude_background_status(payload) == "api_error"


def test_background_status_success_stays_completed():
    payload = {"success": True, "error": None, "terminal_status": "api_error"}
    assert cat._claude_background_status(payload) == "completed"


@pytest.mark.parametrize("token", ["interrupted", "timeout"])
def test_background_status_interrupted_and_timeout_win(token):
    payload = {"success": False, "error": token, "terminal_status": "api_error"}
    assert cat._claude_background_status(payload) == token


def test_background_status_interrupt_bit_forces_interrupted(monkeypatch):
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)
    payload = {
        "success": False,
        "error": "Claude Code exited with code 1",
        "terminal_status": "api_error",
    }
    assert cat._claude_background_status(payload) == "interrupted"


def test_background_status_generic_error_stays_error():
    assert cat._claude_background_status(
        {"success": False, "error": "boom"}) == "error"


# ---------------------------------------------------------------------------
# Completion event + parent-facing notice
# ---------------------------------------------------------------------------

def _evt(**over):
    evt = {
        "type": "async_delegation", "delegation_id": "deleg_t1",
        "session_key": "", "goal": "ship the fix",
        "tool": "delegate_claude_agent", "result_kind": "cli_agent",
        "model": "glm-5.2", "status": "api_error", "summary": None,
        "error": "Claude Code exited with code 1: read tcp: ECONNRESET",
        "api_calls": 0, "duration_seconds": 12.0,
        "dispatched_at": 1000.0, "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_notice_names_api_error_and_carries_exact_log_path():
    text = format_process_notification(_evt(log_path="/tmp/sept-closed.jsonl"))
    assert text is not None
    assert "api_error" in text
    assert "Log: /tmp/sept-closed.jsonl" in text


def test_notice_without_log_path_mentions_no_jsonl():
    text = format_process_notification(_evt())
    assert text is not None
    assert ".jsonl" not in text


def test_background_transport_death_event_carries_token_and_log(
        monkeypatch, tmp_path):
    envelope = _dispatch_background(
        monkeypatch, tmp_path, log_path="/tmp/sept-econn.jsonl",
        log_text=json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "read tcp: ECONNRESET",
        }),
        rc=1,
    )
    assert envelope.get("status") == "dispatched"
    evt = _drain_event(envelope["delegation_id"])
    assert evt is not None, "completion event never arrived"
    assert evt["status"] == "api_error"
    assert evt.get("terminal_status") == "api_error"
    assert evt.get("log_path") == "/tmp/sept-econn.jsonl"


def test_background_spawn_persists_log_path_on_running_row(
        monkeypatch, tmp_path):
    envelope = _dispatch_background(
        monkeypatch, tmp_path, log_path="/tmp/spawn-persist.jsonl",
        log_text=json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "Done.",
        }),
        rc=0,
    )
    assert envelope.get("status") == "dispatched"
    evt = _drain_event(envelope["delegation_id"])
    assert evt is not None, "completion event never arrived"

    with ad._transaction() as conn:
        row = conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?",
            (envelope["delegation_id"],),
        ).fetchone()
    assert row is not None
    assert json.loads(row[0]).get("log_path") == "/tmp/spawn-persist.jsonl"


# ---------------------------------------------------------------------------
# Owner-exit recovery notice
# ---------------------------------------------------------------------------

def _insert_running_row(conn, delegation_id, task):
    conn.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, origin_ui_session_id,
            parent_session_id, state, dispatched_at, updated_at,
            delivery_state, delivery_attempts, owner_pid, owner_started_at,
            task_json, origin_session_id)
           VALUES (?, '', '', NULL, 'running', 100.0, 100.0, 'pending', 0,
                   999999, NULL, ?, '')""",
        (delegation_id, json.dumps(task)),
    )


def _recovered_events(*delegation_ids):
    events = {}
    with ad._transaction() as conn:
        for did in delegation_ids:
            row = conn.execute(
                "SELECT event_json FROM async_delegations WHERE delegation_id=?",
                (did,),
            ).fetchone()
            events[did] = json.loads(row[0]) if row and row[0] else None
    return events


def test_owner_exit_notice_names_saved_log_or_says_none_recorded(monkeypatch):
    base_task = {
        "goal": "fix the flaky build",
        "tool": "delegate_claude_agent",
        "result_kind": "cli_agent",
    }
    with ad._transaction() as conn:
        _insert_running_row(conn, "deleg-saved",
                            {**base_task, "log_path": "/tmp/spawn-saved.jsonl"})
        _insert_running_row(conn, "deleg-unsaved", dict(base_task))

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    assert ad.recover_abandoned_delegations() == 2

    saved, unsaved = (
        _recovered_events("deleg-saved", "deleg-unsaved")[key]
        for key in ("deleg-saved", "deleg-unsaved")
    )

    # Saved at spawn: the exact path rides the event and the notice.
    assert saved["status"] == "unknown"
    assert saved.get("log_path") == "/tmp/spawn-saved.jsonl"
    saved_notice = format_process_notification(saved)
    assert saved_notice is not None
    assert "Log: /tmp/spawn-saved.jsonl" in saved_notice

    # Never saved: unknown stays unknown, the notice says so, no invented path.
    assert unsaved["status"] == "unknown"
    assert not unsaved.get("log_path")
    unsaved_notice = format_process_notification(unsaved)
    assert unsaved_notice is not None
    assert "no log was recorded" in unsaved_notice
    assert ".jsonl" not in unsaved_notice


# ---------------------------------------------------------------------------
# Lane workdir: git-free directories
# ---------------------------------------------------------------------------

def test_omp_lane_accepts_git_free_workdir_and_cursor_rejects(
        monkeypatch, tmp_path):
    from tools import delegate_lane_tool as lane_tool
    from tools.cursor_agent_tool import (
        UnsupportedOriginError,
        resolve_workdir_origin,
    )
    from tools.delegate_lane_specs import resolve_lane_spec

    directory = tmp_path / "plain"
    directory.mkdir()
    assert not (directory / ".git").exists()

    monkeypatch.setattr(
        lane_tool, "resolve_runnable_spec",
        lambda name, lanes=None, omp_config=None: resolve_lane_spec(
            "ui_dev", {"ui_dev": {"harness": "omp",
                                  "models": ["provider/m"]}}),
    )
    calls = []

    def _record(spec, task, workdir, model, timeout_seconds, context):
        calls.append(workdir)
        return {"success": True, "final_report": "ok"}

    monkeypatch.setitem(lane_tool._RUNNERS, "omp", _record)

    result = json.loads(lane_tool.delegate_lane(
        "ui_dev", "do the thing", str(directory)))
    assert calls == [str(directory)], (
        "a git-free existing absolute directory must reach the OMP runner"
    )
    assert result["success"] is True

    # The same directory is still unusable as a Cursor Cloud repo URL.
    with pytest.raises(UnsupportedOriginError) as excinfo:
        resolve_workdir_origin(str(directory))
    assert "no usable git origin" in str(excinfo.value)
