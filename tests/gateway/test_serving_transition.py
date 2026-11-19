"""Durable per-session serving identity and the process-transition note.

A restart (overlap or crash) moves a conversation to a new gateway process
mid-life. The persisted conversation/task context carries over; the
process-local state (terminal sessions, background processes, kernels,
sockets, caches, PIDs) does not. The agent must be told the difference on
the first turn served by the new process — and never on ordinary turns.

Regression for the live incident where a transparent continuation resumed
in an overlap successor with no note at all: the agent kept referring to
the predecessor's terminals and PIDs as if they were current.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import serving_identity as si


_OLD_PID, _OLD_START = 709505, 850305
_NEW_PID, _NEW_START = 1531038, 19048


def _current_identity(pid=_NEW_PID, start=_NEW_START):
    """Static identity fields stand-in (the shape of _static_identity_fields)."""
    return {"pid": pid, "start_time": start, "code_sha": "abcd1234ef567890"}


def _runner(**attrs) -> SimpleNamespace:
    runner = SimpleNamespace(
        adapters={}, _profile_adapters={}, _draining=False, _restart_requested=False,
        _overlap_successor_pending=False, _overlap_predecessor_pid=None,
        _overlap_restart_successor_pid=None, _overlap_restart_successor_start_time=None,
        _service_tier=None, _consume_pending_turn_sidecar_notes=lambda session_key: [],
    )
    for key, value in attrs.items():
        setattr(runner, key, value)
    return runner


# ── comparison contract ────────────────────────────────────────────────────


def test_transition_state_distinguishes_same_process():
    assert si.transition_state(
        {"pid": _NEW_PID, "start_time": _NEW_START},
        _current_identity(),
    ) == "same"


def test_transition_state_different_pid_is_changed():
    assert si.transition_state(
        {"pid": _OLD_PID, "start_time": _OLD_START},
        _current_identity(),
    ) == "changed"


def test_transition_state_pid_reuse_is_changed():
    """Same pid with a different start fingerprint: a new process on a reused pid."""
    assert si.transition_state(
        {"pid": _NEW_PID, "start_time": _OLD_START},
        _current_identity(),
    ) == "changed"


def test_transition_state_unknown_without_previous_record():
    assert si.transition_state(None, _current_identity()) == "unknown"


def test_transition_state_unknown_when_fingerprints_missing():
    assert si.transition_state(
        {"pid": _NEW_PID}, _current_identity()) == "unknown"
    assert si.transition_state(
        {"pid": _NEW_PID, "start_time": _NEW_START},
        {"pid": _NEW_PID, "start_time": None},
    ) == "unknown"


# ── note wording ───────────────────────────────────────────────────────────


def test_transition_note_names_old_and_new_process_and_revalidation():
    note = si.serving_transition_note(
        {"pid": _OLD_PID, "start_time": _OLD_START}, _current_identity())
    assert note.startswith("[System note:")
    assert f"pid {_NEW_PID}, start {_NEW_START}" in note
    assert f"pid {_OLD_PID}, start {_OLD_START}" in note
    # Not a new task: carried-over context stays valid.
    assert "not a new task" in note
    assert "persisted" in note
    # Process-local state must be revalidated, never assumed current.
    assert "revalidate" in note
    assert any(term in note for term in ("terminal", "background", "cache"))


def test_transition_note_silent_when_no_transition_provable():
    assert si.serving_transition_note(None, _current_identity()) is None
    assert si.serving_transition_note(
        {"pid": _NEW_PID, "start_time": _NEW_START}, _current_identity()) is None
    assert si.serving_transition_note(
        {"pid": _NEW_PID}, _current_identity()) is None


# ── durable persistence + producer composition ─────────────────────────────


def _db(tmp_path):
    from hermes_state import SessionDB

    return SessionDB(db_path=tmp_path / "state.db")


def test_first_turn_records_identity_and_claims_no_transition(tmp_path):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    identity, transition = session_turn_notes(db, "sess-1", _runner())
    assert identity.startswith("[System note: This turn is served by")
    assert transition is None
    record = json.loads(db.get_meta("served_by:sess-1"))
    assert isinstance(record["pid"], int)
    assert isinstance(record["start_time"], int)


def test_same_process_next_turn_claims_no_transition(tmp_path):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    session_turn_notes(db, "sess-1", _runner())
    _, transition = session_turn_notes(db, "sess-1", _runner())
    assert transition is None


def test_new_process_names_transition_and_updates_record(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    monkeypatch.setattr(si, "_static_identity_fields",
                        lambda: dict(_current_identity(), pid=_NEW_PID))
    session_turn_notes(db, "sess-1", _runner())
    # The conversation was previously served by a different process.
    db.set_meta("served_by:sess-1",
                json.dumps({"pid": _OLD_PID, "start_time": _OLD_START}))
    identity, transition = session_turn_notes(db, "sess-1", _runner())
    assert transition is not None
    assert f"pid {_OLD_PID}" in transition
    assert f"pid {_NEW_PID}" in transition
    assert json.loads(db.get_meta("served_by:sess-1"))["pid"] == _NEW_PID

def test_uncomparable_previous_record_claims_nothing(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    # Same pid as this process, but no start fingerprint: equality is
    # unprovable (the pid may have been recycled), so claim nothing.
    db.set_meta("served_by:sess-1", json.dumps({"pid": _NEW_PID}))
    _, transition = session_turn_notes(db, "sess-1", _runner())
    assert transition is None


def test_corrupt_record_is_ignored_and_rewritten(tmp_path):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    db.set_meta("served_by:sess-1", "not-json{")
    _, transition = session_turn_notes(db, "sess-1", _runner())
    assert transition is None
    assert json.loads(db.get_meta("served_by:sess-1"))["pid"] > 0


def test_persistence_failure_never_breaks_the_turn(tmp_path):
    from gateway.serving_transition import session_turn_notes

    class _ExplodingStore:
        def _db_for_session_id(self, session_id):
            raise RuntimeError("db unavailable")

    identity, transition = session_turn_notes(_ExplodingStore(), "sess-1", _runner())
    assert identity.startswith("[System note:")
    assert transition is None


def test_non_string_session_id_skips_persistence():
    from gateway.serving_transition import session_turn_notes

    identity, transition = session_turn_notes(None, None, _runner())
    assert identity.startswith("[System note:")
    assert transition is None


# ── turn wiring: both delivery channels ────────────────────────────────────


class _SeamAgent:
    def __init__(self):
        self._notification_config = None
        self._notification_platform = None
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None
        self.step_callback = None
        self.stream_delta_callback = None
        self.interim_assistant_callback = None
        self.status_callback = None
        self.notice_callback = None
        self.notice_clear_callback = None
        self.event_callback = None
        self.reasoning_config = None
        self.service_tier = None
        self.request_overrides = {}
        self._gateway_turn_request_overrides = {}


def _seam_ctx(session_id):
    ctx = MagicMock()
    ctx.user_config = {}
    ctx.source = SimpleNamespace(platform="discord", is_bot=False)
    ctx._native_slack_task_cards = False
    ctx._hooks_ref = SimpleNamespace(loaded_hooks=False)
    ctx._status_callback_sync = None
    ctx._notice_callback_sync = None
    ctx._event_callback_sync = None
    ctx._step_callback_sync = None
    ctx.progress_callback = None
    ctx.session_key = "sk-test"
    ctx.session_id = session_id
    ctx.tools_holder = [None]
    ctx.agent_holder = [_SeamAgent()]
    return ctx


def _wire(runner, ctx):
    from gateway.run_turn_runner import TurnRunner

    agent = _SeamAgent()
    TurnRunner(runner, ctx)._wire_turn_agent_callbacks(
        agent, {}, None, None, None, False)
    return agent


def test_wire_first_turn_has_identity_note_without_transition(tmp_path):
    db = _db(tmp_path)
    agent = _wire(_runner(session_store=db), _seam_ctx("sess-w"))
    assert "This turn is served by" in agent._gateway_turn_context_notes
    assert "moved to a new" not in agent._gateway_turn_context_notes
    assert "moved to a new" not in agent._gateway_continuation_note


def test_wire_transition_after_handoff_on_both_channels(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    _wire(_runner(session_store=db), ctx)
    # The predecessor served the previous turns of this same session.
    db.set_meta("served_by:sess-w",
                json.dumps({"pid": _OLD_PID, "start_time": _OLD_START}))
    agent = _wire(_runner(session_store=db), ctx)
    # The ordinary turn-note channel (current user message sidecar).
    assert "moved to a new Hermes gateway process" in agent._gateway_turn_context_notes
    assert f"pid {_OLD_PID}" in agent._gateway_turn_context_notes
    # The transparent-continuation channel (no new user row) carries it too.
    assert "moved to a new Hermes gateway process" in agent._gateway_continuation_note
    assert f"pid {_OLD_PID}" in agent._gateway_continuation_note


def test_wire_continuation_note_composes_identity_then_transition(tmp_path):
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    _wire(_runner(session_store=db), ctx)
    db.set_meta("served_by:sess-w",
                json.dumps({"pid": _OLD_PID, "start_time": _OLD_START}))
    agent = _wire(_runner(session_store=db), ctx)
    continuation = agent._gateway_continuation_note
    assert "This turn is served by" in continuation
    assert "moved to a new Hermes gateway process" in continuation
    assert continuation.index("This turn is served by") < continuation.index("moved to a new")


# ── the continuation sidecar carries the composed note byte-for-byte ──────


def test_continuation_sidecar_stamps_transition_note(tmp_path):
    from agent.turn_context import _stamp_continuation_note_sidecar

    note = (
        "[System note: This turn is served by Hermes gateway pid 1. Generic body.]"
        "\n\n[System note: This conversation moved to a new Hermes gateway process: "
        f"pid {_NEW_PID} replaced pid {_OLD_PID}. Revalidate process-local state.]"
    )
    agent = SimpleNamespace(_gateway_continuation_note=note)
    messages = [{"role": "user", "content": "deploy the service"}]
    _stamp_continuation_note_sidecar(agent, messages, 0)
    assert messages[0]["api_content"] == f"deploy the service\n\n{note}"
    assert getattr(agent, "_gateway_continuation_note", "") == ""
