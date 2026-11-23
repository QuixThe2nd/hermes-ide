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
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import serving_identity as si


_OLD_PID, _OLD_START = 709505, 850305
_NEW_PID, _NEW_START = 1531038, 19048
_OLD_BUILD = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
_NEW_BUILD = "e5f60718e5f60718e5f60718e5f60718e5f60718"


def _current_identity(pid=_NEW_PID, start=_NEW_START, sha=_NEW_BUILD):
    """Static identity fields stand-in (the shape of _static_identity_fields)."""
    return {"pid": pid, "start_time": start, "code_sha": sha}


@contextmanager
def _canned_delta(commits="abc123 fix gateway\ndef456 fix tests",
                  stat=" 2 files changed, 3 insertions(+)"):
    """Serve a canned git body so wire tests exercise the note, not git."""
    import gateway.serving_codedelta as cd

    with patch.object(cd, "_git_render", lambda old, new: (commits, stat)):
        yield


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
    identity, transition, _delta, _pending = session_turn_notes(db, "sess-1", _runner())
    assert identity.startswith("[System note: This turn is served by")
    assert transition is None
    record = json.loads(db.get_meta("served_by:sess-1"))
    assert isinstance(record["pid"], int)
    assert isinstance(record["start_time"], int)


def test_same_process_next_turn_claims_no_transition(tmp_path):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    session_turn_notes(db, "sess-1", _runner())
    _, transition, _, _ = session_turn_notes(db, "sess-1", _runner())
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
    identity, transition, _delta, _pending = session_turn_notes(db, "sess-1", _runner())
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
    _, transition, _, _ = session_turn_notes(db, "sess-1", _runner())
    assert transition is None


def test_corrupt_record_is_ignored_and_rewritten(tmp_path):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    db.set_meta("served_by:sess-1", "not-json{")
    _, transition, _, _ = session_turn_notes(db, "sess-1", _runner())
    assert transition is None
    assert json.loads(db.get_meta("served_by:sess-1"))["pid"] > 0


def test_persistence_failure_never_breaks_the_turn(tmp_path):
    from gateway.serving_transition import session_turn_notes

    class _ExplodingStore:
        def _db_for_session_id(self, session_id):
            raise RuntimeError("db unavailable")

    identity, transition, _delta, _pending = session_turn_notes(
        _ExplodingStore(), "sess-1", _runner())
    assert identity.startswith("[System note:")
    assert transition is None


def test_non_string_session_id_skips_persistence():
    from gateway.serving_transition import session_turn_notes

    identity, transition, _delta, _pending = session_turn_notes(None, None, _runner())
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


def test_wire_suppressed_note_absent_from_both_turn_note_channels(tmp_path):
    from gateway.serving_transition import confirm_served_note

    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    first = _wire(_runner(session_store=db), ctx)
    assert "This turn is served by" in first._gateway_turn_context_notes
    assert "This turn is served by" in first._gateway_continuation_note
    # The note's sidecar stamp landed durably (modelled here by the ack the
    # stamp performs): the proof advances, and only then does the redundant
    # unchanged repeat drop off both the turn-note sidecar and the
    # transparent-continuation channel.
    confirm_served_note(db, "sess-w", first._gateway_serving_note_pending)
    second = _wire(_runner(session_store=db), ctx)
    assert "This turn is served by" not in (second._gateway_turn_context_notes or "")
    assert "This turn is served by" not in (second._gateway_continuation_note or "")


# ── the note proof is ack-gated at the sidecar stamp ───────────────────────


def test_stage_leaves_note_proof_unwritten_until_stamp_ack(tmp_path):
    """Regression: the stage step must NOT persist the note as seen.

    A turn staged and then crashed before the api_content sidecar stamp used
    to suppress the note forever — the conversation never learned which
    process serves it. The durable proof now lands only with the stamp ack,
    so an unacked turn re-emits the note on the next turn.
    """
    from gateway.serving_transition import confirm_served_note, session_turn_notes

    db = _db(tmp_path)
    identity, transition, _delta, pending = session_turn_notes(db, "sess-1", _runner())
    assert identity is not None and transition is None
    assert pending == identity
    record = json.loads(db.get_meta("served_by:sess-1"))
    # pid/start land at stage (the durable predecessor must be learnable)…
    assert isinstance(record["pid"], int)
    # …but the note proof does not: the note has not been delivered yet.
    assert record.get("note") != identity
    # A crashed turn (no stamp, no ack) re-emits the note next turn.
    again, _, _, pending_again = session_turn_notes(db, "sess-1", _runner())
    assert again == identity
    # The stamp ack lands: the proof advances and the repeat suppresses.
    assert confirm_served_note(db, "sess-1", pending_again) is True
    assert json.loads(db.get_meta("served_by:sess-1"))["note"] == identity
    settled, _, _, _ = session_turn_notes(db, "sess-1", _runner())
    assert settled is None


def test_wire_unacked_turn_re_emits_note(tmp_path):
    """Two staged turns with no stamp ack between them both carry the note."""
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    first = _wire(_runner(session_store=db), ctx)
    assert "This turn is served by" in first._gateway_turn_context_notes
    second = _wire(_runner(session_store=db), ctx)
    assert "This turn is served by" in second._gateway_turn_context_notes
    assert "This turn is served by" in second._gateway_continuation_note


def test_confirm_served_note_boundaries(tmp_path, monkeypatch):
    """The ack is write-on-change over the note AND the code_sha proof,
    keyed per session, and never raises."""
    from gateway.serving_transition import confirm_served_note

    db = _db(tmp_path)
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    # No record to advance: no write, no error.
    assert confirm_served_note(db, "sess-1", "note-x") is False
    assert db.get_meta("served_by:sess-1") is None
    # Corrupt record: unprovable, left for the stage step to re-establish.
    db.set_meta("served_by:sess-1", "not-json{")
    assert confirm_served_note(db, "sess-1", "note-x") is False
    # A parseable record advances its note proof AND its build sha — the
    # delta channel settles for this process's build at stamp time.
    db.set_meta("served_by:sess-1", json.dumps(
        {"pid": 1, "start_time": 2, "code_sha": _OLD_BUILD}))
    assert confirm_served_note(db, "sess-1", "note-x") is True
    assert json.loads(db.get_meta("served_by:sess-1")) == {
        "pid": 1, "start_time": 2, "note": "note-x", "code_sha": _NEW_BUILD}
    assert confirm_served_note(db, "sess-1", "note-x") is False
    # Note already current but sha stale: the delta channel still settles.
    db.set_meta("served_by:sess-2", json.dumps(
        {"pid": 1, "start_time": 2, "note": "note-x", "code_sha": _OLD_BUILD}))
    assert confirm_served_note(db, "sess-2", "note-x") is True
    assert json.loads(db.get_meta("served_by:sess-2"))["code_sha"] == _NEW_BUILD
    # Session A's proof never silences session B.
    assert json.loads(db.get_meta("served_by:sess-1"))["note"] == "note-x"
    assert db.get_meta("served_by:sess-b") is None
    # Invalid inputs are silent no-ops.
    assert confirm_served_note(db, None, "note-x") is False
    assert confirm_served_note(db, "sess-1", None) is False
    assert confirm_served_note(None, "sess-1", "note-x") is False


def test_persist_sidecar_stamp_confirms_pending_note(tmp_path):
    """The production seam: the api_content sidecar stamp acks the note.

    ``_persist_turn_user_api_content`` stamps BOTH the ordinary turn-note
    sidecar and the transparent-continuation note; the stamp landing is the
    acknowledgement that advances the durable note proof.
    """
    from agent.turn_context import _persist_turn_user_api_content

    db = _db(tmp_path)
    db.create_session("sess-1", source="cli")
    db.append_message("sess-1", "user", content="hi")
    row_id = db.get_messages("sess-1")[0]["id"]
    note = "[System note: This turn is served by Hermes gateway pid 1.]"
    db.set_meta("served_by:sess-1", json.dumps({"pid": 1, "start_time": 2}))
    agent = SimpleNamespace(
        session_id="sess-1", _session_db=db, _last_compaction_in_place=False,
        _gateway_serving_note_pending=note)
    row = {"role": "user", "content": "hi", "_row_id": row_id}
    _persist_turn_user_api_content(
        agent, row, "hi", f"hi\n\n{note}", preflight_compressed=False)
    assert db.get_messages("sess-1")[0]["api_content"] == f"hi\n\n{note}"
    assert json.loads(db.get_meta("served_by:sess-1"))["note"] == note
    # One-shot: the pending note is consumed by its ack.
    assert agent._gateway_serving_note_pending is None


def test_persist_sidecar_without_stamp_leaves_proof_unwritten(tmp_path):
    """A stamp that never lands (no addressable row) must NOT ack the note —
    the next turn re-emits it."""
    from agent.turn_context import _persist_turn_user_api_content

    db = _db(tmp_path)
    db.set_meta("served_by:sess-1", json.dumps({"pid": 1, "start_time": 2}))
    note = "[System note: This turn is served by Hermes gateway pid 1.]"
    agent = SimpleNamespace(
        session_id="sess-1", _session_db=db, _last_compaction_in_place=False,
        _gateway_serving_note_pending=note)
    row = {"role": "user", "content": "hi"}  # no _row_id: nothing to stamp
    _persist_turn_user_api_content(
        agent, row, "hi", f"hi\n\n{note}", preflight_compressed=False)
    assert json.loads(db.get_meta("served_by:sess-1")).get("note") is None
    assert agent._gateway_serving_note_pending == note


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


# ── the code delta rides the process handoff ───────────────────────────────
#
# The served-by record also remembers WHICH BUILD served the previous turns.
# Stage proves the new process but keeps the previous build's sha; only the
# api_content stamp ack settles the sha — so a turn that crashes before the
# stamp re-emits the delta on the next turn. When the two builds differ, a
# code-delta note (commit subjects + diff stat + a terminal pointer) rides
# BOTH the ordinary turn-note channel and the transparent-continuation
# channel, tagged with its own injection kind.

def _handoff_record(**extra):
    return {"pid": _OLD_PID, "start_time": _OLD_START,
            "code_sha": _OLD_BUILD, **extra}


def test_first_turn_records_code_sha_and_rides_delta_free(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    identity, transition, delta, _pending = session_turn_notes(db, "sess-1", _runner())
    assert identity.startswith("[System note: This turn is served by")
    assert transition is None and delta is None
    record = json.loads(db.get_meta("served_by:sess-1"))
    assert record["code_sha"] == _NEW_BUILD


def test_handoff_build_change_keeps_previous_sha_until_ack(tmp_path, monkeypatch):
    """Regression for the ack-gating contract: stage must NOT settle the
    code_sha — only the stamp ack may — or a crashed turn would silently
    lose its delta."""
    from gateway.serving_transition import confirm_served_note, session_turn_notes

    db = _db(tmp_path)
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    db.set_meta("served_by:sess-1", json.dumps(_handoff_record()))
    with _canned_delta():
        identity, transition, delta, pending = session_turn_notes(
            db, "sess-1", _runner())
    assert identity is not None and transition is not None
    assert delta is not None
    assert "could not be computed" not in delta
    assert _OLD_BUILD[:8] in delta and _NEW_BUILD[:8] in delta
    assert "Commits between the two builds" in delta
    assert f"git diff {_OLD_BUILD}..{_NEW_BUILD}" in delta
    # Stage claims THIS process but keeps the PREVIOUS build.
    record = json.loads(db.get_meta("served_by:sess-1"))
    assert record["pid"] == _NEW_PID
    assert record["code_sha"] == _OLD_BUILD
    # The stamp ack settles the delta channel for this build…
    assert confirm_served_note(db, "sess-1", pending) is True
    assert json.loads(db.get_meta("served_by:sess-1"))["code_sha"] == _NEW_BUILD
    # …and the next turn rides nothing.
    with _canned_delta():
        _, _, settled_delta, _ = session_turn_notes(db, "sess-1", _runner())
    assert settled_delta is None


def test_wire_handoff_build_change_carries_delta_on_both_channels(
        tmp_path, monkeypatch):
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    _wire(_runner(session_store=db), ctx)
    db.set_meta("served_by:sess-w", json.dumps(_handoff_record()))
    with _canned_delta():
        agent = _wire(_runner(session_store=db), ctx)
    # The ordinary turn-note channel: delta bytes tagged with their kind.
    assert "The Hermes gateway build changed" in agent._gateway_turn_context_notes
    assert agent._gateway_turn_note_kinds[-1] == "code_delta"
    # The transparent-continuation channel carries it too.
    assert "The Hermes gateway build changed" in agent._gateway_continuation_note
    # Ordering: identity → transition → delta.
    continuation = agent._gateway_continuation_note
    assert continuation.index("This turn is served by") < \
        continuation.index("moved to a new") < \
        continuation.index("The Hermes gateway build changed")


def test_wire_unacked_handoff_re_emits_delta(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    _wire(_runner(session_store=db), ctx)
    db.set_meta("served_by:sess-w", json.dumps(_handoff_record()))
    with _canned_delta():
        first = _wire(_runner(session_store=db), ctx)
        second = _wire(_runner(session_store=db), ctx)
    assert "The Hermes gateway build changed" in first._gateway_turn_context_notes
    assert "The Hermes gateway build changed" in second._gateway_turn_context_notes
    assert second._gateway_turn_note_kinds[-1] == "code_delta"


def test_wire_suppressed_delta_after_the_ack(tmp_path, monkeypatch):
    from gateway.serving_transition import confirm_served_note

    db = _db(tmp_path)
    ctx = _seam_ctx("sess-w")
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    _wire(_runner(session_store=db), ctx)
    db.set_meta("served_by:sess-w", json.dumps(_handoff_record()))
    with _canned_delta():
        agent = _wire(_runner(session_store=db), ctx)
    confirm_served_note(db, "sess-w", agent._gateway_serving_note_pending)
    with _canned_delta():
        settled = _wire(_runner(session_store=db), ctx)
    assert "The Hermes gateway build changed" not in (
        settled._gateway_turn_context_notes or "")
    assert "The Hermes gateway build changed" not in (
        settled._gateway_continuation_note or "")
    assert "code_delta" not in (settled._gateway_turn_note_kinds or [])


def test_persist_sidecar_stamp_settles_the_delta_channel(tmp_path, monkeypatch):
    """The production seam: the api_content sidecar stamp acks the delta too."""
    from agent.turn_context import _persist_turn_user_api_content

    db = _db(tmp_path)
    db.create_session("sess-1", source="cli")
    db.append_message("sess-1", "user", content="hi")
    row_id = db.get_messages("sess-1")[0]["id"]
    note = "[System note: This turn is served by Hermes gateway pid 1.]"
    db.set_meta("served_by:sess-1", json.dumps(
        {"pid": 1, "start_time": 2, "code_sha": _OLD_BUILD}))
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _current_identity())
    agent = SimpleNamespace(
        session_id="sess-1", _session_db=db, _last_compaction_in_place=False,
        _gateway_serving_note_pending=note)
    row = {"role": "user", "content": "hi", "_row_id": row_id}
    _persist_turn_user_api_content(
        agent, row, "hi", f"hi\n\n{note}", preflight_compressed=False)
    assert db.get_messages("sess-1")[0]["api_content"] == f"hi\n\n{note}"
    record = json.loads(db.get_meta("served_by:sess-1"))
    assert record["note"] == note
    assert record["code_sha"] == _NEW_BUILD
