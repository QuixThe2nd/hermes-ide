"""Serving-process identity for gateway turns and status surfaces.

Regression for the recurring post-overlap-restart confusion: after a chained
overlap restart, every externally observable identity (systemd MainPID,
gateway_state.json, the pid file, and — during the drain window — even the
control socket) names the DRAINING predecessor, while new turns actually run
in the successor. Agents repeatedly concluded the restart was incomplete and
kept waiting on the dying process.

Contracts under test (owner: gateway/serving_identity.py):
- the snapshot classifies this process's role from runner state only;
- the per-turn runtime note names the actual serving pid and, when a
  transition is knowable, explains that draining ancestors are not the
  current process;
- the turn-wiring seam attaches the note on the existing api_content
  sidecar channel (never the system prompt);
- ``identify`` carries the overlap role so an out-of-process CLI can tell a
  draining predecessor from the serving successor;
- status lines distinguish serving successor / predecessor drain / pending
  chained restart, and say "unknown" — never a false serving claim — when no
  identity source is provable.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.control_socket import build_identify_payload
from gateway.serving_identity import (
    ROLE_DRAINING_PREDECESSOR,
    ROLE_OVERLAP_SUCCESSOR,
    ROLE_SERVING,
    serving_identity_snapshot,
    serving_runtime_note,
    serving_status_lines,
)

_PREDECESSOR_PID = 1438
_SUCCESSOR_PID = 709505


def _runner(**attrs) -> SimpleNamespace:
    """Runner stand-in: the snapshot only reads attributes, never methods."""
    runner = SimpleNamespace(
        _consume_pending_turn_sidecar_notes=lambda session_key: [],
        _service_tier=None,
    )
    for name, value in attrs.items():
        setattr(runner, name, value)
    return runner


# ── snapshot classification ────────────────────────────────────────────────


def test_snapshot_ordinary_process_is_serving():
    snapshot = serving_identity_snapshot(_runner())
    assert snapshot["role"] == ROLE_SERVING
    assert snapshot["pid"] == os.getpid()
    assert snapshot["predecessor_pid"] is None
    assert snapshot["successor_pid"] is None


def test_snapshot_overlap_successor_names_predecessor():
    snapshot = serving_identity_snapshot(
        _runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert snapshot["role"] == ROLE_OVERLAP_SUCCESSOR
    assert snapshot["predecessor_pid"] == _PREDECESSOR_PID


def test_snapshot_draining_predecessor_names_successor():
    snapshot = serving_identity_snapshot(
        _runner(_draining=True, _restart_requested=True,
                _overlap_restart_successor_pid=_SUCCESSOR_PID))
    assert snapshot["role"] == ROLE_DRAINING_PREDECESSOR
    assert snapshot["successor_pid"] == _SUCCESSOR_PID


def test_snapshot_drain_without_restart_stays_serving():
    """A pure shutdown/pause drain is not a restart handoff: no successor identity to claim."""
    snapshot = serving_identity_snapshot(_runner(_draining=True))
    assert snapshot["role"] == ROLE_SERVING


def test_snapshot_never_raises_on_junk_runner():
    assert serving_identity_snapshot(_runner(_overlap_restart_successor_pid="junk"))["role"] == ROLE_SERVING


# ── per-turn runtime note ──────────────────────────────────────────────────


def test_note_names_serving_pid_and_scope():
    note = serving_runtime_note(_runner())
    assert f"pid {os.getpid()}" in note
    assert "systemctl" in note
    assert "pid file" in note
    assert note.startswith("[System note:")


def test_note_overlap_successor_explains_draining_predecessor():
    note = serving_runtime_note(
        _runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert f"pid {os.getpid()}" in note
    assert f"predecessor pid {_PREDECESSOR_PID}" in note
    assert "draining" in note
    assert "restart already succeeded" in note


def test_note_draining_predecessor_directs_successor_and_finish():
    note = serving_runtime_note(
        _runner(_draining=True, _restart_requested=True,
                _overlap_restart_successor_pid=_SUCCESSOR_PID))
    assert f"pid {os.getpid()}" in note
    assert f"successor pid {_SUCCESSOR_PID}" in note
    assert "finish this turn" in note.lower()
    assert "do not call restart again" in note


def test_note_draining_predecessor_without_spawned_successor_stays_honest():
    note = serving_runtime_note(_runner(_draining=True, _restart_requested=True))
    assert "successor" in note
    assert f"pid {_SUCCESSOR_PID}" not in note


# ── turn-wiring seam (the owning consumer assignment) ─────────────────────


class _SeamAgent:
    """Attribute sink mirroring what _wire_turn_agent_callbacks touches with real values."""

    def __init__(self):
        self.request_overrides = {}
        self._gateway_turn_request_overrides = {}


def _wire_notes(runner, ctx=None):
    from gateway.run_turn_runner import TurnRunner

    turn = TurnRunner(runner, ctx or _seam_ctx())
    turn._make_bg_review_callbacks = lambda: (lambda message: None, lambda: None)
    turn._attach_session_title_callback = lambda agent, ctx: None
    agent = _SeamAgent()
    turn._wire_turn_agent_callbacks(agent, {}, None, None, None, False)
    return agent


def _seam_ctx():
    ctx = MagicMock()
    ctx.user_config = {}
    ctx.mute_notification_reply = False
    ctx._thinking_enabled = False
    ctx._native_slack_task_cards = False
    ctx._status_adapter = None
    ctx._hooks_ref.loaded_hooks = []
    ctx.session_key = "telegram:1:1"
    ctx.agent_holder = [None]
    ctx.tools_holder = [None]
    ctx.process_task_id = "task"
    ctx.process_baseline = frozenset()
    return ctx


def test_wire_turn_agent_callbacks_attaches_runtime_note():
    agent = _wire_notes(_runner())
    notes = agent._gateway_turn_context_notes
    assert f"pid {os.getpid()}" in notes
    assert "systemctl" in notes


def test_wire_turn_agent_callbacks_appends_after_staged_notes():
    runner = _runner()
    runner._consume_pending_turn_sidecar_notes = lambda session_key: ["[System note: staged note]"]
    agent = _wire_notes(runner)
    assert agent._gateway_turn_context_notes.startswith("[System note: staged note]")
    assert f"pid {os.getpid()}" in agent._gateway_turn_context_notes


def test_wire_turn_agent_callbacks_note_rides_overlap_state():
    agent = _wire_notes(
        _runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert f"predecessor pid {_PREDECESSOR_PID}" in agent._gateway_turn_context_notes


# ── identify enrichment ────────────────────────────────────────────────────


def test_identify_payload_carries_overlap_fields():
    payload = build_identify_payload(
        runner=_runner(_draining=True, _restart_requested=True,
                       _overlap_restart_successor_pid=_SUCCESSOR_PID))
    overlap = payload["overlap"]
    assert overlap["role"] == ROLE_DRAINING_PREDECESSOR
    assert overlap["successor_pid"] == _SUCCESSOR_PID


def test_identify_payload_overlap_successor_fields():
    payload = build_identify_payload(
        runner=_runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert payload["overlap"] == {
        "role": ROLE_OVERLAP_SUCCESSOR, "predecessor_pid": _PREDECESSOR_PID}


def test_identify_payload_without_runner_keeps_legacy_shape():
    payload = build_identify_payload()
    assert "pid" in payload
    assert "overlap" not in payload


# ── status lines: serving successor vs predecessor drain vs chained ───────


def _identify(pid, *, role=None, predecessor_pid=None, successor_pid=None):
    payload = {"pid": pid, "start_time": 123456, "kind": "hermes-gateway"}
    overlap = {}
    if role:
        overlap["role"] = role
    if predecessor_pid:
        overlap["predecessor_pid"] = predecessor_pid
    if successor_pid:
        overlap["successor_pid"] = successor_pid
    if overlap:
        payload["overlap"] = overlap
    return payload


def test_status_lines_distinguish_serving_successor_from_draining_predecessor():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID),
        pid_live=lambda pid: True)
    joined = "\n".join(lines)
    assert f"pid {_SUCCESSOR_PID}" in joined
    assert "successor" in joined
    assert f"pid {_PREDECESSOR_PID}" in joined
    assert "draining" in joined.lower()
    assert "not an incomplete restart" in joined
    # The serving claim must name the successor, never the predecessor alone.
    assert any(ln.startswith("✓ Serving gateway:") and f"pid {_SUCCESSOR_PID}" in ln for ln in lines)


def test_status_lines_warn_when_reported_successor_died():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID),
        pid_live=lambda pid: pid == _PREDECESSOR_PID)
    joined = "\n".join(lines)
    assert f"pid {_SUCCESSOR_PID}" in joined
    assert "unknown" in joined or "not running" in joined


def test_status_lines_plain_identify_names_answering_pid():
    lines = serving_status_lines(identify=_identify(_SUCCESSOR_PID), pid_live=lambda pid: True)
    assert any(
        ln.startswith("✓ Serving gateway:") and f"pid {_SUCCESSOR_PID}" in ln for ln in lines)


def test_status_lines_state_file_fallback_when_socket_silent():
    lines = serving_status_lines(
        identify=None, state_pid=_PREDECESSOR_PID, state_gateway_state="draining",
        state_pid_is_live=True, state_is_stale=False)
    assert any("gateway_state.json" in ln and f"pid {_PREDECESSOR_PID}" in ln for ln in lines)


def test_status_lines_unknown_when_socket_silent_and_state_stale():
    lines = serving_status_lines(
        identify=None, state_pid=_PREDECESSOR_PID, state_gateway_state="draining",
        state_pid_is_live=True, state_is_stale=True)
    assert any(ln.startswith("⚠ Serving gateway: unknown") for ln in lines)
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)


def test_status_lines_unknown_when_state_pid_dead():
    lines = serving_status_lines(
        identify=None, state_pid=_PREDECESSOR_PID, state_gateway_state="running",
        state_pid_is_live=False, state_is_stale=False)
    assert any(ln.startswith("⚠ Serving gateway: unknown") and f"pid {_PREDECESSOR_PID}" in ln
               for ln in lines)


def test_status_lines_unknown_when_no_sources():
    lines = serving_status_lines(identify=None)
    assert any(ln.startswith("⚠ Serving gateway: unknown") for ln in lines)


def test_status_lines_report_pending_chained_restart():
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID),
        chained={"source": "telegram user 42 (restart)"},
        pid_live=lambda pid: True)
    assert any("Chained restart queued" in ln and "telegram user 42" in ln for ln in lines)


def test_status_lines_no_chained_line_without_marker():
    lines = serving_status_lines(identify=_identify(_SUCCESSOR_PID), pid_live=lambda pid: True)
    assert not any("Chained restart" in ln for ln in lines)
