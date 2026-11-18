"""Serving-process identity for gateway turns and status surfaces.

Contract under test (shared with ``internal/servingidentity``):

- the snapshot classifies THIS process's restart-handoff role and FAILS
  UNKNOWN on classification errors — never a default ``serving`` claim;
- the per-turn runtime note never claims a successor serves new turns from
  a bare PID attribute: only the hub-attested readiness ack (the same
  signal that gates real event handoff) may say "already taken over";
- the status ladder never prints a green serving claim it cannot prove:
  a draining predecessor without a confirmed successor stays unknown, a
  fresh ``draining`` record in gateway_state.json never labels its pid as
  the serving gateway, and a successor claimed from PID liveness alone
  (PID reuse) does not count.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.serving_identity import (
    ROLE_DRAINING_PREDECESSOR,
    ROLE_OVERLAP_SUCCESSOR,
    ROLE_SERVING,
    ROLE_UNKNOWN,
    overlap_fields,
    serving_identity_snapshot,
    serving_runtime_note,
    serving_status_lines,
)
from gateway.control_socket import build_identify_payload

_PREDECESSOR_PID = 1438
_SUCCESSOR_PID = 709505
_SUCCESSOR_START = 987654


def _runner(**attrs) -> SimpleNamespace:
    """Runner stand-in: the snapshot only reads attributes, never methods."""
    runner = SimpleNamespace(
        adapters={}, _profile_adapters={}, _draining=False, _restart_requested=False,
        _overlap_successor_pending=False, _overlap_predecessor_pid=None,
        _overlap_restart_successor_pid=None, _overlap_restart_successor_start_time=None,
        _service_tier=None, _consume_pending_turn_sidecar_notes=lambda session_key: [],
    )
    for key, value in attrs.items():
        setattr(runner, key, value)
    return runner


def _hub_adapter(*, ready: bool) -> SimpleNamespace:
    return SimpleNamespace(supports_overlap_restart=True, _overlap_successor_ready=ready)


class _ExplodingRunner(SimpleNamespace):
    """Attribute reads that raise: classification must fail UNKNOWN, not serve."""

    @property
    def _restart_requested(self):  # noqa: N805 - property on subclass namespace
        raise RuntimeError("boom")

    def __getattr__(self, name):
        raise RuntimeError("boom")


# ── snapshot classification ────────────────────────────────────────────────


def test_snapshot_ordinary_process_is_serving():
    snapshot = serving_identity_snapshot(_runner())
    assert snapshot["role"] == ROLE_SERVING
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
                _overlap_restart_successor_pid=_SUCCESSOR_PID,
                _overlap_restart_successor_start_time=_SUCCESSOR_START))
    assert snapshot["role"] == ROLE_DRAINING_PREDECESSOR
    assert snapshot["successor_pid"] == _SUCCESSOR_PID
    assert snapshot["successor_start_time"] == _SUCCESSOR_START


def test_snapshot_identity_cache_rebuilds_when_pid_changes(monkeypatch):
    """A stale cached identity must never name the wrong process."""
    monkeypatch.setattr(os, "getpid", lambda: 424242)
    assert serving_identity_snapshot(_runner())["pid"] == 424242
    monkeypatch.setattr(os, "getpid", lambda: 424243)
    assert serving_identity_snapshot(_runner())["pid"] == 424243


def test_snapshot_drain_without_restart_stays_serving():
    """A pure shutdown/pause drain is not a restart handoff: no successor identity to claim."""
    snapshot = serving_identity_snapshot(_runner(_draining=True))
    assert snapshot["role"] == ROLE_SERVING


def test_snapshot_classification_failure_is_unknown_never_serving():
    snapshot = serving_identity_snapshot(_ExplodingRunner())
    assert snapshot["role"] == ROLE_UNKNOWN


def test_snapshot_never_raises_on_junk_runner():
    snapshot = serving_identity_snapshot(_runner(_overlap_restart_successor_pid="junk"))
    assert snapshot["role"] == ROLE_SERVING
    assert snapshot["successor_pid"] is None


# ── successor readiness: hub attestation, not PID liveness ─────────────────


def test_snapshot_successor_ready_only_with_hub_ack():
    ready = _runner(_draining=True, _restart_requested=True,
                    _overlap_restart_successor_pid=_SUCCESSOR_PID,
                    adapters={"discord": _hub_adapter(ready=True)})
    not_ready = _runner(_draining=True, _restart_requested=True,
                        _overlap_restart_successor_pid=_SUCCESSOR_PID,
                        adapters={"discord": _hub_adapter(ready=False)})
    assert serving_identity_snapshot(ready)["successor_ready"] is True
    assert serving_identity_snapshot(not_ready)["successor_ready"] is False


def test_snapshot_successor_ready_survives_adapter_scan_failure():
    class _BadAdapters(dict):
        def values(self):
            raise RuntimeError("boom")

    runner = _runner(_draining=True, _restart_requested=True,
                     _overlap_restart_successor_pid=_SUCCESSOR_PID)
    runner.adapters = _BadAdapters()
    assert serving_identity_snapshot(runner)["successor_ready"] is False


# ── per-turn runtime note ──────────────────────────────────────────────────


def test_note_names_serving_pid_and_scope():
    note = serving_runtime_note(_runner())
    assert f"pid {os.getpid()}" in note
    assert "systemctl" in note
    assert note.startswith("[System note:")


def test_note_overlap_successor_explains_draining_predecessor():
    note = serving_runtime_note(
        _runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert f"predecessor pid {_PREDECESSOR_PID}" in note
    assert "restart already succeeded" in note


def test_note_draining_predecessor_directs_successor_and_finish():
    note = serving_runtime_note(
        _runner(_draining=True, _restart_requested=True,
                _overlap_restart_successor_pid=_SUCCESSOR_PID,
                adapters={"discord": _hub_adapter(ready=True)}))
    assert f"successor (pid {_SUCCESSOR_PID})" in note
    assert "has already taken over new turns" in note
    assert "do not call restart again" in note


def test_note_draining_predecessor_without_hub_ack_never_claims_takeover():
    """Spawned PID alone is not proof: readiness unconfirmed stays unclaimed."""
    note = serving_runtime_note(
        _runner(_draining=True, _restart_requested=True,
                _overlap_restart_successor_pid=_SUCCESSOR_PID,
                adapters={"discord": _hub_adapter(ready=False)}))
    assert f"pid {_SUCCESSOR_PID}" in note
    assert "has already taken over" not in note
    assert "is starting" in note


def test_note_draining_predecessor_without_spawned_successor_stays_honest():
    note = serving_runtime_note(_runner(_draining=True, _restart_requested=True))
    assert "successor" in note or "replacement process" in note
    assert f"pid {_SUCCESSOR_PID}" not in note
    assert "has already taken over" not in note


def test_note_classification_failure_still_names_own_pid():
    note = serving_runtime_note(_ExplodingRunner())
    assert f"pid {os.getpid()}" in note
    assert "predecessor" not in note
    assert "successor" not in note


# ── turn-wiring seam (the owning consumer assignment) ─────────────────────


class _SeamAgent:
    """Attribute sink mirroring what _wire_turn_agent_callbacks touches with real values."""

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


def _wire_notes(runner, ctx=None):
    from gateway.run_turn_runner import TurnRunner

    if ctx is None:
        ctx = _seam_ctx()
    agent = _SeamAgent()
    TurnRunner(runner, ctx)._wire_turn_agent_callbacks(
        agent, {}, None, None, None, False)
    return agent

def _seam_ctx():
    ctx = MagicMock()
    ctx.user_config = {}
    source = SimpleNamespace(platform="discord", is_bot=False)
    ctx.source = source
    ctx._native_slack_task_cards = False
    ctx._hooks_ref = SimpleNamespace(loaded_hooks=False)
    ctx._status_callback_sync = None
    ctx._notice_callback_sync = None
    ctx._event_callback_sync = None
    ctx._step_callback_sync = None
    ctx.progress_callback = None
    ctx.session_key = "sk-test"
    ctx.tools_holder = [None]
    agent = _SeamAgent()
    ctx.agent_holder = [agent]
    return ctx


def test_wire_turn_agent_callbacks_attaches_runtime_note():
    agent = _wire_notes(_runner())
    notes = agent._gateway_turn_context_notes
    assert f"pid {os.getpid()}" in notes
    assert "systemctl" in notes


def test_wire_turn_agent_callbacks_appends_after_staged_notes():
    runner = _runner()
    runner._consume_pending_turn_sidecar_notes = lambda session_key: ["staged note"]
    agent = _wire_notes(runner)
    assert agent._gateway_turn_context_notes.startswith("staged note")
    assert f"pid {os.getpid()}" in agent._gateway_turn_context_notes


def test_wire_turn_agent_callbacks_note_rides_overlap_state():
    agent = _wire_notes(
        _runner(_overlap_successor_pending=True, _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert f"predecessor pid {_PREDECESSOR_PID}" in agent._gateway_turn_context_notes


# ── identify enrichment ────────────────────────────────────────────────────


def test_identify_payload_carries_overlap_fields():
    payload = build_identify_payload(
        runner=_runner(_draining=True, _restart_requested=True,
                       _overlap_restart_successor_pid=_SUCCESSOR_PID,
                       _overlap_restart_successor_start_time=_SUCCESSOR_START,
                       adapters={"discord": _hub_adapter(ready=True)}))
    overlap = payload["overlap"]
    assert overlap["role"] == ROLE_DRAINING_PREDECESSOR
    assert overlap["successor_pid"] == _SUCCESSOR_PID
    assert overlap["successor_start_time"] == _SUCCESSOR_START
    assert overlap["successor_ready"] is True


def test_identify_payload_overlap_successor_fields():
    payload = build_identify_payload(
        runner=_runner(_overlap_successor_pending=True,
                       _overlap_predecessor_pid=_PREDECESSOR_PID))
    assert payload["overlap"] == {
        "role": ROLE_OVERLAP_SUCCESSOR, "predecessor_pid": _PREDECESSOR_PID}


def test_identify_payload_without_runner_keeps_legacy_shape():
    payload = build_identify_payload()
    assert "pid" in payload
    assert "overlap" not in payload


def test_overlap_fields_omit_unknowns():
    fields = overlap_fields(serving_identity_snapshot(_runner()))
    assert fields == {"role": ROLE_SERVING}


# ── status lines: serving successor vs predecessor drain vs chained ───────


def _identify(pid, *, role=None, predecessor_pid=None, successor_pid=None,
              successor_start_time=None, successor_ready=None):
    payload = {"pid": pid, "start_time": 123456, "kind": "hermes-gateway"}
    overlap = {}
    if role is not None:
        overlap["role"] = role
    if predecessor_pid is not None:
        overlap["predecessor_pid"] = predecessor_pid
    if successor_pid is not None:
        overlap["successor_pid"] = successor_pid
    if successor_start_time is not None:
        overlap["successor_start_time"] = successor_start_time
    if successor_ready is not None:
        overlap["successor_ready"] = successor_ready
    if overlap:
        payload["overlap"] = overlap
    return payload


def _successor_identity(expected_pid, expected_start):
    def resolve(pid, start_time):
        assert pid == expected_pid
        if expected_start is None:
            return "unknown"
        return "live" if start_time == expected_start else "dead"

    return resolve


def test_status_lines_green_only_for_attested_live_successor():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID,
                           successor_start_time=_SUCCESSOR_START, successor_ready=True),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert any(
        ln.startswith("✓ Serving gateway:")
        and f"pid {_SUCCESSOR_PID}" in ln
        and "successor" in ln
        for ln in lines)
    assert any(f"pid {_PREDECESSOR_PID}" in ln and "predecessor" in ln.lower() for ln in lines)
    assert any("not an incomplete restart" in ln for ln in lines)


def test_status_lines_spawned_but_unattested_successor_is_never_green():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID,
                           successor_start_time=_SUCCESSOR_START, successor_ready=False),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any("starting" in ln and f"pid {_SUCCESSOR_PID}" in ln for ln in lines)


def test_status_lines_ready_flag_alone_is_not_green_when_fingerprint_unknown():
    """No fingerprint relayed → PID liveness is unprovable → readiness stays a maybe."""
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID, successor_ready=True),
        successor_identity=_successor_identity(_SUCCESSOR_PID, None))
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(f"pid {_SUCCESSOR_PID}" in ln for ln in lines)


def test_status_lines_pid_reuse_does_not_pass_as_successor():
    """A live PID whose start fingerprint differs is a reused PID, not our successor."""
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR,
                           successor_pid=_SUCCESSOR_PID,
                           successor_start_time=_SUCCESSOR_START, successor_ready=True),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START + 5))
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any("not running" in ln or "reused" in ln or "unknown" in ln for ln in lines)


def test_status_lines_draining_predecessor_without_successor_is_unknown():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_DRAINING_PREDECESSOR),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(f"pid {_PREDECESSOR_PID}" in ln and "draining" in ln for ln in lines)


def test_status_lines_unclassifiable_role_is_unknown():
    lines = serving_status_lines(
        identify=_identify(_PREDECESSOR_PID, role=ROLE_UNKNOWN),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(ln.startswith("⚠ Serving gateway: unknown") for ln in lines)


def test_status_lines_plain_identify_names_answering_pid():
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID, role=ROLE_SERVING),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert any(
        ln.startswith("✓ Serving gateway:") and f"pid {_SUCCESSOR_PID}" in ln for ln in lines)


def test_status_lines_legacy_identify_without_overlap_is_serving():
    """A gateway old enough not to relay overlap state is a plain serving answer."""
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert any(ln.startswith("✓ Serving gateway:") for ln in lines)


def test_status_lines_overlap_successor_answer_is_green():
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID, role=ROLE_OVERLAP_SUCCESSOR,
                           predecessor_pid=_PREDECESSOR_PID),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert any(
        ln.startswith("✓ Serving gateway:")
        and f"pid {_SUCCESSOR_PID}" in ln
        and f"pid {_PREDECESSOR_PID}" in ln
        for ln in lines)


def test_status_lines_state_file_fallback_when_socket_silent():
    lines = serving_status_lines(
        state_pid=_SUCCESSOR_PID, state_gateway_state="running", state_pid_is_live=True)
    assert any("gateway_state.json" in ln and f"pid {_SUCCESSOR_PID}" in ln for ln in lines)
    assert any(ln.startswith("✓ Serving gateway:") for ln in lines)


def test_status_lines_state_file_draining_record_is_never_serving():
    """A fresh, live `draining` record names a predecessor mid-handoff — not the server."""
    lines = serving_status_lines(
        state_pid=_PREDECESSOR_PID, state_gateway_state="draining",
        state_pid_is_live=True, state_restart_requested=True)
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(ln.startswith("⚠ Serving gateway: unknown") for ln in lines)
    assert any(f"pid {_PREDECESSOR_PID}" in ln and "predecessor" in ln.lower() for ln in lines)


def test_status_lines_unknown_when_socket_silent_and_state_stale():
    lines = serving_status_lines(
        state_pid=_PREDECESSOR_PID, state_gateway_state="running", state_is_stale=True)
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(
        ln.startswith("⚠ Serving gateway: unknown") and "stale" in ln for ln in lines)


def test_status_lines_unknown_when_state_pid_dead():
    lines = serving_status_lines(
        state_pid=_PREDECESSOR_PID, state_gateway_state="running",
        state_pid_is_live=False)
    assert not any(ln.startswith("✓ Serving gateway:") for ln in lines)
    assert any(
        ln.startswith("⚠ Serving gateway: unknown") and "not running" in ln
        for ln in lines)


def test_status_lines_unknown_when_no_sources():
    lines = serving_status_lines(identify=None)
    assert any(ln.startswith("⚠ Serving gateway: unknown") for ln in lines)


def test_status_lines_report_pending_chained_restart():
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID, role=ROLE_SERVING),
        chained={"source": "discord user 42 (restart)"},
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert any("Chained restart queued" in ln and "discord user 42" in ln for ln in lines)


def test_status_lines_no_chained_line_without_marker():
    lines = serving_status_lines(
        identify=_identify(_SUCCESSOR_PID, role=ROLE_SERVING),
        successor_identity=_successor_identity(_SUCCESSOR_PID, _SUCCESSOR_START))
    assert not any("Chained restart" in ln for ln in lines)
