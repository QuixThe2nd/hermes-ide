"""delegate_agent(action=...) must control conversation-owned live CLI coding runs.

Pin (branch fix/cli-delegation-control, Nov 2026): a live Claude Code run
(``delegate_claude_agent`` / result_kind ``cli_agent``) or Cursor cloud run
(``delegate_cursor_agent`` / result_kind ``cloud_agent``) is registered only in
``tools.async_delegation._records`` under a ``deleg_*`` id with an
``interrupt_fn``. ``_handle_control_action`` consulted only
``_active_subagents``, so ``list`` omitted the run and ``steer``/``stop`` with
its ``deleg_*`` id answered "No live subagent" without ever firing
``interrupt_fn`` — forcing a note file and a second run.

Contract (triage-decided): list and stop must see conversation-owned live
``deleg_*`` coding runs, and stop must actually interrupt. steer cannot type
into Claude (stdin closed) or Cursor (cloud run), so steer must answer an
honest not_steerable / stop-and-redelegate response — never "No live subagent".

The pins below FAIL on unfixed HEAD; that failure is the point. The native
subagent regression at the bottom must keep passing.
"""

import json
import time
import weakref
from typing import Any, Dict, cast

import tools.async_delegation as ad
from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _handle_control_action,
    _register_subagent,
    _unregister_subagent,
)
from tools.session_context import reset_current_session_key, set_current_session_key


class _StubParent:
    """Parent-agent stand-in: durable session id + optional SessionDB lineage."""

    def __init__(self, session_id: str = "", session_db=None):
        self.session_id = session_id
        self._session_db = session_db


class _StubLineageDB:
    """resolve_resume_session_id: pre-compression session id -> lineage tip."""

    def __init__(self, lineage: dict):
        self._lineage = lineage

    def resolve_resume_session_id(self, session_id):
        return self._lineage.get(session_id, session_id)


def _interrupt_spy():
    """Interrupt closure that records invocations; returns (fn, calls)."""
    calls: list = []

    def _fn() -> None:
        calls.append(1)

    return _fn, calls


def _seed_cli_record(
    delegation_id: str,
    *,
    tool: str,
    result_kind: str,
    parent_session_id: str,
    session_key: str = "",
    status: str = "running",
    goal: str = "Fix the flaky integration test",
):
    """Insert a live coding-run record shaped exactly as
    ``dispatch_background_delegation`` writes it (claude_agent_tool.py /
    cursor_agent_tool.py): no runner, no durable-ledger write, unmonitored
    (``progress_fn=None``) so the stale-monitor thread ignores the seed."""
    record = {
        "delegation_id": delegation_id,
        "goal": goal,
        "goals": [goal],
        "context": None,
        "toolsets": None,
        "role": "leaf",
        "model": None,
        "session_key": session_key,
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "parent_session_id": parent_session_id,
        "tool": tool,
        "result_kind": result_kind,
        "status": status,
        "dispatched_at": time.time(),
        "completed_at": None,
        "interrupt_fn": None,
        "progress_fn": None,
        "startup_evidence": {},
        "slot_key": delegation_id,
        "delivery_mode": ad.DELIVERY_MODE_EVENT,
    }
    with ad._records_lock:
        ad._records[delegation_id] = record
    return record


def _drop_records(*delegation_ids: str) -> None:
    with ad._records_lock:
        for did in delegation_ids:
            ad._records.pop(did, None)


# ---------------------------------------------------------------------------
# Pin 1: steer on an owned live Claude coding run -> honest not_steerable
# ---------------------------------------------------------------------------


def test_steer_owned_live_claude_run_is_not_steerable():
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_5c686284",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="sess-cli-steer-1",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="sess-cli-steer-1")
    try:
        out = _handle_control_action(
            "steer", "deleg_5c686284", "Prefer the streaming parser", parent
        )
        assert "No live subagent" not in out, out
        payload = json.loads(out)  # must be a JSON control reply, not tool_error text
        assert "error" not in payload, payload
        assert payload["action"] == "steer"
        assert payload["subagent_id"] == "deleg_5c686284"
        assert payload["status"] == "not_steerable"
        note = str(payload.get("note", ""))
        low = note.lower()
        # The run cannot accept mid-run text (stdin closed / cloud run).
        assert (
            "mid-run" in low
            or "cannot accept" in low
            or "cannot be steered" in low
            or "not steerable" in low
        ), note
        # How to stop it: with the same id.
        assert "stop" in low, note
        assert (
            "same id" in low
            or "deleg_5c686284" in note
            or "action='stop'" in low
            or 'action="stop"' in low
        ), note
        # And redelegate for the course correction.
        assert "redelegate" in low or "re-delegate" in low, note
        # Steering must never fire the process-group kill.
        assert calls == []
    finally:
        _drop_records("deleg_5c686284")


# ---------------------------------------------------------------------------
# Pin 2: same contract for an owned live Cursor cloud run
# ---------------------------------------------------------------------------


def test_steer_owned_live_cursor_run_is_not_steerable():
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_9d13aa02",
        tool="delegate_cursor_agent",
        result_kind="cloud_agent",
        parent_session_id="sess-cli-steer-2",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="sess-cli-steer-2")
    try:
        out = _handle_control_action(
            "steer", "deleg_9d13aa02", "Use the staging project", parent
        )
        assert "No live subagent" not in out, out
        payload = json.loads(out)
        assert "error" not in payload, payload
        assert payload["action"] == "steer"
        assert payload["subagent_id"] == "deleg_9d13aa02"
        assert payload["status"] == "not_steerable"
        note = str(payload.get("note", "")).lower()
        assert (
            "mid-run" in note
            or "cannot accept" in note
            or "cannot be steered" in note
            or "not steerable" in note
        )
        assert "stop" in note
        assert "redelegate" in note or "re-delegate" in note
        assert calls == []
    finally:
        _drop_records("deleg_9d13aa02")


# ---------------------------------------------------------------------------
# Pin 3: list shows exactly the owned live coding runs
# ---------------------------------------------------------------------------


def test_list_shows_owned_live_coding_runs_and_excludes_the_rest():
    parent = _StubParent(session_id="sess-cli-list-1")
    ids = (
        "deleg_3aa10001",  # owned live claude run -> listed
        "deleg_3aa10002",  # owned live cursor run -> listed
        "deleg_3aa10003",  # foreign parent session -> hidden
        "deleg_3aa10004",  # owned but completed -> hidden
        "deleg_3aa10005",  # owned running subagent_batch -> not a coding run
    )
    _seed_cli_record(
        "deleg_3aa10001",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="sess-cli-list-1",
        goal="Claude: refactor auth module",
    )
    _seed_cli_record(
        "deleg_3aa10002",
        tool="delegate_cursor_agent",
        result_kind="cloud_agent",
        parent_session_id="sess-cli-list-1",
        goal="Cursor: migrate the endpoint",
    )
    _seed_cli_record(
        "deleg_3aa10003",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="sess-someone-else",
        goal="Someone else's claude run",
    )
    _seed_cli_record(
        "deleg_3aa10004",
        tool="delegate_cursor_agent",
        result_kind="cloud_agent",
        parent_session_id="sess-cli-list-1",
        status="completed",
        goal="Already finished",
    )
    _seed_cli_record(
        "deleg_3aa10005",
        tool="delegate_agent",
        result_kind="subagent_batch",
        parent_session_id="sess-cli-list-1",
        goal="Native batch unit",
    )
    try:
        out = json.loads(_handle_control_action("list", None, None, parent))
        listed = {e.get("subagent_id"): e for e in out.get("subagents", [])}
        assert set(listed) == {"deleg_3aa10001", "deleg_3aa10002"}, listed

        claude = listed["deleg_3aa10001"]
        assert claude["tool"] == "delegate_claude_agent"
        assert claude["accepting_steer"] is False
        assert claude["status"] == "running"
        assert claude["goal"]

        cursor = listed["deleg_3aa10002"]
        assert cursor["tool"] == "delegate_cursor_agent"
        assert cursor["accepting_steer"] is False
        assert cursor["goal"] and cursor["status"]

        # With an owned coding run live, the empty-registry note must be gone.
        assert "No live subagents right now" not in json.dumps(out)
    finally:
        _drop_records(*ids)


# ---------------------------------------------------------------------------
# Pin 4: stop interrupts the owned coding run; foreign / spineless fail closed
# ---------------------------------------------------------------------------


def test_stop_owned_live_coding_run_interrupts_exactly_once():
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_4bb10001",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="sess-cli-stop-1",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="sess-cli-stop-1")
    try:
        out = _handle_control_action("stop", "deleg_4bb10001", None, parent)
        assert "No live subagent" not in out, out
        payload = json.loads(out)
        assert "error" not in payload, payload
        assert payload["action"] == "stop"
        assert payload["subagent_id"] == "deleg_4bb10001"
        assert payload["status"] == "interrupt_requested"
        assert len(calls) == 1
    finally:
        _drop_records("deleg_4bb10001")


def test_stop_foreign_coding_run_is_refused_without_interrupt():
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_4bb10002",
        tool="delegate_cursor_agent",
        result_kind="cloud_agent",
        parent_session_id="sess-someone-else-2",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="sess-cli-stop-2")
    try:
        out = _handle_control_action("stop", "deleg_4bb10002", None, parent)
        assert "No live subagent" in out
        assert calls == []
    finally:
        _drop_records("deleg_4bb10002")


def test_stop_fails_closed_without_any_session_spine():
    """No parent session id and no current session key: a live record must not
    be claimable — empty-string selectors never create ownership."""
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_4bb10003",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="",
        session_key="telegram:999:guard",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="")
    try:
        out = _handle_control_action("stop", "deleg_4bb10003", None, parent)
        assert "No live subagent" in out
        assert calls == []
    finally:
        _drop_records("deleg_4bb10003")


# ---------------------------------------------------------------------------
# Pin 5: ownership tiers — compression lineage and current session key
# ---------------------------------------------------------------------------


def test_stop_ownership_resolves_compression_lineage():
    """Coding run dispatched under a pre-compression session id; the parent now
    carries the rotated id. Both resolve to the same lineage tip -> still
    owned and interruptible."""
    lineage = {
        "sess-pre-rotation": "sess-tip-5c",
        "sess-rotated-now": "sess-tip-5c",
    }
    parent = _StubParent(session_id="sess-rotated-now", session_db=_StubLineageDB(lineage))
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_5cc10001",
        tool="delegate_claude_agent",
        result_kind="cli_agent",
        parent_session_id="sess-pre-rotation",
    )
    rec["interrupt_fn"] = fn
    try:
        out = _handle_control_action("stop", "deleg_5cc10001", None, parent)
        assert "No live subagent" not in out, out
        payload = json.loads(out)
        assert payload["status"] == "interrupt_requested"
        assert len(calls) == 1
    finally:
        _drop_records("deleg_5cc10001")


def test_steer_ownership_via_current_session_key():
    """parent_session_id empty (the routing session key is the only spine): the
    run is owned when its session_key is the conversation's current key."""
    fn, calls = _interrupt_spy()
    rec = _seed_cli_record(
        "deleg_5dd10002",
        tool="delegate_cursor_agent",
        result_kind="cloud_agent",
        parent_session_id="",
        session_key="telegram:4242:curator",
    )
    rec["interrupt_fn"] = fn
    parent = _StubParent(session_id="")
    token = set_current_session_key("telegram:4242:curator")
    try:
        out = _handle_control_action(
            "steer", "deleg_5dd10002", "Tighten the diff scope", parent
        )
        assert "No live subagent" not in out, out
        payload = json.loads(out)
        assert "error" not in payload, payload
        assert payload["subagent_id"] == "deleg_5dd10002"
        assert payload["status"] == "not_steerable"
        assert calls == []
    finally:
        reset_current_session_key(token)
        _drop_records("deleg_5dd10002")


# ---------------------------------------------------------------------------
# Pin 6: the delegate_agent schema must teach the deleg_* control contract
# ---------------------------------------------------------------------------


def test_delegate_schema_documents_cli_coding_run_controls():
    schema = cast(Dict[str, Any], DELEGATE_TASK_SCHEMA)
    params = cast(Dict[str, Any], schema["parameters"])
    props = cast(Dict[str, Any], params["properties"])
    action_prop = cast(Dict[str, Any], props["action"])
    desc = str(action_prop.get("description", "")).lower()
    # deleg_* ids from Claude/Cursor coding runs show up in list...
    assert "deleg_" in desc, desc
    assert "list" in desc
    # ...can be stopped...
    assert "stop" in desc
    # ...cannot be steered — stop and redelegate instead.
    assert (
        "cannot be steered" in desc
        or "not steerable" in desc
        or "cannot accept" in desc
    ), desc
    assert "redelegate" in desc or "re-delegate" in desc, desc


# ---------------------------------------------------------------------------
# Regression (must PASS on HEAD): native subagents stay listed and steerable
# ---------------------------------------------------------------------------


class _StubNativeChild:
    """Weakref-able stand-in for a live native child AIAgent."""

    def __init__(self, parent):
        self.steered: list = []
        self._delegate_parent_ref = weakref.ref(parent)
        self._live_transcript_path = "/tmp/live/native-0.log"

    def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True


def test_native_subagent_still_listed_and_steerable():
    parent = _StubParent(session_id="sess-native-reg")
    child = _StubNativeChild(parent)
    _register_subagent(
        {
            "subagent_id": "sid-native-regression",
            "parent_id": None,
            "depth": 0,
            "goal": "native goal",
            "model": "test-model",
            "started_at": 1000.0,
            "status": "running",
            "tool_count": 0,
            "agent": child,
        }
    )
    try:
        listed = json.loads(_handle_control_action("list", None, None, parent))
        entries = {e.get("subagent_id"): e for e in listed.get("subagents", [])}
        assert "sid-native-regression" in entries
        assert entries["sid-native-regression"]["accepting_steer"] is True

        out = json.loads(
            _handle_control_action("steer", "sid-native-regression", "tighten scope", parent)
        )
        assert out["status"] == "queued"
        assert child.steered == ["tighten scope"]
    finally:
        _unregister_subagent("sid-native-regression")
