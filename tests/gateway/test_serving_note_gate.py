"""Serving-note gate: suppress the identity note a session already saw.

The per-turn serving-identity runtime note (``serving_runtime_note``) today
rides EVERY turn of EVERY session — even when it is byte-identical to the
note the same conversation saw on the previous turn (same process, same
role, same wording). That is pure context noise: an ordinary long-lived
session re-pays the note on every turn for information that never changed.

This file is the test-first contract for the suppression gate (branch
``fix/serving-note-on-change``). On the pre-change baseline (dcef78fc390)
every test here FAILS at its suppression assertion, because no gate exists:
the unchanged note is re-rendered per turn. The contract it pins on
``gateway.serving_transition.session_turn_notes``:

- SUPPRESS — a turn whose identity note is identical to the one this
  session already saw returns ``None`` for the identity note (and claims
  no transition). Suppression is per session: another session's record
  never silences a first turn.
- EMIT when proof is missing — first-ever turn, corrupt record, a record
  written before the gate existed, or no reachable store: an unprovable
  "already saw it" never suppresses.
- EMIT on real change — a process handoff (different pid or start
  fingerprint) still yields BOTH the identity note and the transition
  note; a role change on the SAME process (drain begins; the hub
  readiness ack arrives mid-drain) still yields the new wording. The
  gate keys on what the note SAYS, not just on pid identity.
- The wired turn channels (turn context notes + continuation sidecar)
  stage the note only when it is emitted.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import serving_identity as si

_OLD_PID, _OLD_START = 709505, 850305
_NEW_PID, _NEW_START = 1531038, 19048
_SUCCESSOR_PID = 4242
_BUILD = "abcd1234ef567890"


def _identity(pid=_NEW_PID, start=_NEW_START):
    """Static identity fields stand-in (the shape of _static_identity_fields)."""
    return {"pid": pid, "start_time": start, "code_sha": _BUILD}


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


def _db(tmp_path):
    from hermes_state import SessionDB

    return SessionDB(db_path=tmp_path / "state.db")


def _pin_identity(monkeypatch, pid=_NEW_PID, start=_NEW_START):
    monkeypatch.setattr(si, "_static_identity_fields", lambda: _identity(pid, start))


# ── suppression: the note a session already saw ────────────────────────────


def test_unchanged_note_suppressed_on_next_same_process_turn(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    first, transition = session_turn_notes(db, "sess-gate", _runner())
    # First turn: nothing proves the conversation saw any note yet → emit.
    assert first.startswith("[System note: This turn is served by")
    assert transition is None
    # Ordinary next turn, same process, same role: the note would be
    # byte-identical, so the gate must suppress it.
    second, transition2 = session_turn_notes(db, "sess-gate", _runner())
    assert second is None, "an unchanged serving note must not ride a second turn"
    assert transition2 is None
    # No reachable store means no proof of "already saw it" — never suppress.
    assert session_turn_notes(None, None, _runner())[0].startswith("[System note:")


def test_suppression_is_per_session(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    assert session_turn_notes(db, "sess-a", _runner())[0].startswith("[System note:")
    # Session B has no record of its own: its FIRST turn must still learn
    # which process serves it — session A's proof never silences B.
    first_b, _ = session_turn_notes(db, "sess-b", _runner())
    assert first_b.startswith("[System note: This turn is served by")
    # Each session suppresses only from its own second unchanged turn.
    assert session_turn_notes(db, "sess-a", _runner())[0] is None
    assert session_turn_notes(db, "sess-b", _runner())[0] is None


@pytest.mark.parametrize("flavor", [
    "absent-record",
    "corrupt-record",
    "legacy-record-without-note-proof",
])
def test_unprovable_previous_record_emits_then_suppresses(flavor, tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    if flavor == "corrupt-record":
        db.set_meta("served_by:sess-gate", "not-json{")
    elif flavor == "legacy-record-without-note-proof":
        # Written by a pre-gate build: it names THIS process, but carries no
        # proof the conversation ever saw the note.
        db.set_meta("served_by:sess-gate",
                    json.dumps({"pid": _NEW_PID, "start_time": _NEW_START}))
    identity, transition = session_turn_notes(db, "sess-gate", _runner())
    assert identity.startswith("[System note: This turn is served by"), (
        "an unprovable 'already saw it' must never suppress the note")
    assert transition is None
    # The emitting turn made the proof durable; the unchanged repeat suppresses.
    repeat, transition2 = session_turn_notes(db, "sess-gate", _runner())
    assert repeat is None
    assert transition2 is None


# ── real changes still ride ────────────────────────────────────────────────


def test_process_handoff_still_emits_both_notes_then_settles(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    db = _db(tmp_path)
    _pin_identity(monkeypatch, pid=_OLD_PID, start=_OLD_START)
    session_turn_notes(db, "sess-gate", _runner())  # the predecessor served earlier turns
    _pin_identity(monkeypatch, pid=_NEW_PID, start=_NEW_START)
    identity, transition = session_turn_notes(db, "sess-gate", _runner())
    # A REAL handoff is the one transition that must never be silenced.
    assert identity is not None and f"pid {_NEW_PID}" in identity
    assert transition is not None
    assert f"pid {_OLD_PID}" in transition
    assert f"pid {_NEW_PID}" in transition
    # ...but once the new process's note has been seen, repeats suppress.
    repeat, transition2 = session_turn_notes(db, "sess-gate", _runner())
    assert repeat is None
    assert transition2 is None


def test_drain_role_change_on_same_process_emits_new_wording_then_suppresses(
        tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    session_turn_notes(db, "sess-gate", _runner())  # ordinary serving turn
    drain = _runner(_draining=True, _restart_requested=True,
                    _overlap_restart_successor_pid=_SUCCESSOR_PID)
    identity, transition = session_turn_notes(db, "sess-gate", drain)
    # Same pid, same start — but the note now says something NEW: the
    # process is draining for a restart. The gate keys on the note, not
    # just the process identity, so this must ride.
    assert "draining for a queued restart" in identity
    assert f"pid {_SUCCESSOR_PID}" in identity
    assert "is starting" in identity  # no hub ack yet → never claims takeover
    assert transition is None  # same process, no handoff
    repeat, _ = session_turn_notes(db, "sess-gate", drain)
    assert repeat is None, "the same drain wording must not ride twice"


def test_drain_readiness_ack_changes_wording_and_rides_again(tmp_path, monkeypatch):
    from gateway.serving_transition import session_turn_notes

    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    unattested = _runner(_draining=True, _restart_requested=True,
                         _overlap_restart_successor_pid=_SUCCESSOR_PID)
    identity, _ = session_turn_notes(db, "sess-gate", unattested)
    assert "is starting" in identity
    assert "has already taken over" not in identity
    # The hub attests the successor: the note's claim materially changes.
    attested = _runner(_draining=True, _restart_requested=True,
                       _overlap_restart_successor_pid=_SUCCESSOR_PID,
                       adapters={"discord": SimpleNamespace(
                           supports_overlap_restart=True, _overlap_successor_ready=True)})
    identity2, _ = session_turn_notes(db, "sess-gate", attested)
    assert "has already taken over new turns" in identity2
    repeat, _ = session_turn_notes(db, "sess-gate", attested)
    assert repeat is None


# ── turn wiring: a suppressed note reaches neither channel ─────────────────


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


def test_wire_suppressed_note_absent_from_both_turn_note_channels(tmp_path, monkeypatch):
    _pin_identity(monkeypatch)
    db = _db(tmp_path)
    ctx = _seam_ctx("sess-wire")
    runner = _runner(session_store=db)
    first = _wire(runner, ctx)
    assert "This turn is served by" in first._gateway_turn_context_notes
    assert "This turn is served by" in first._gateway_continuation_note
    # The redundant unchanged repeat must not reach the model on either the
    # turn-note sidecar or the transparent-continuation channel.
    second = _wire(runner, ctx)
    assert "This turn is served by" not in (second._gateway_turn_context_notes or "")
    assert "This turn is served by" not in (second._gateway_continuation_note or "")
