"""Shared-contract parity for the serving-process identity surfaces.

``internal/servingidentity`` (Go, canonical contract statement) and
``gateway/serving_identity.py`` (Python module, what the runtime renders
with) must produce byte-identical runtime notes and status lines. The twin
is pinned against the RUNNING Go implementation, not merely the shared
table: when a Go toolchain is available the real ``cmd/servingidentity``
binary renders every shared-table record (testdata/serving_cases.json) and
the Python output must match it byte for byte.

The table is generated from the Python module's real output; the Go tests
run the same table as a second cross-check, so a wording change on either
side fails BOTH suites unless the table — and therefore both twins — moves.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import gateway.serving_identity as si

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "servingidentity" / "testdata" / "serving_cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _go_binary(tmp_path_factory) -> str | None:
    """Build cmd/servingidentity once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("servingidentity", numbered=True) / "servingidentity"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/servingidentity"],
        cwd=_REPO / "internal" / "servingidentity",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/servingidentity failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture(scope="session")
def go_servingidentity(tmp_path_factory) -> str | None:
    return _go_binary(tmp_path_factory)


def _table_runner(snap: dict):
    """Runner stand-in that yields exactly this table snapshot."""
    runner = SimpleNamespace(
        adapters={}, _profile_adapters={},
        _draining=bool(snap.get("draining", False)),
        _restart_requested=bool(snap.get("restart_requested", False)),
        _overlap_successor_pending=bool(snap.get("overlap_successor_pending", False)),
        _overlap_predecessor_pid=snap.get("predecessor_pid") or None,
        _overlap_restart_successor_pid=snap.get("successor_pid") or None,
        _overlap_restart_successor_start_time=snap.get("successor_start_time") or None,
    )
    if snap.get("successor_pid"):
        runner.adapters = {"discord": SimpleNamespace(
            supports_overlap_restart=True,
            _overlap_successor_ready=bool(snap.get("successor_ready", False)))}
    return runner


# Ops whose table expectation is one value (string or dict) rather than a
# line list.
_VALUE_OPS = ("note", "transition", "record", "owner_guard",
              "owner_refusal", "chain_rewrite")

# Of those, the ops whose Go binary prints a bare value (not JSON): the
# binary prints chain_rewrite as JSON.
_BARE_OUTPUT_OPS = ("note", "transition", "record", "owner_guard", "owner_refusal")


def _python_render(case: dict):
    if case["op"] == "note":
        with patch.object(si, "_static_identity_fields",
                          lambda: case["input"]["identity"]):
            return si.serving_runtime_note(_table_runner(case["input"]))
    if case["op"] == "transition":
        # The persisted record vs the current process: silence normalizes
        # to "" for the wire table (None in the module, "" in the Go twin).
        with patch.object(si, "_static_identity_fields",
                          lambda: case["input"]["identity"]):
            return si.serving_transition_note(
                case["input"]["previous"], case["input"]["identity"]) or ""
    if case["op"] == "record":
        from gateway.serving_transition import _record_write_needed, _valid_session_id
        request = case["input"]
        if not _valid_session_id(request["session_id"]):
            return "skip"
        return ("write" if _record_write_needed(
            request["stored"], request["record"]["pid"],
            request["record"]["start_time"]) else "skip")
    if case["op"] == "owner_guard":
        from plugins.gateway_restart.tool import socket_owner_action
        request = case["input"]
        return socket_owner_action(request["serving_pid"], request["identify"])
    if case["op"] == "owner_refusal":
        from plugins.gateway_restart.tool import socket_owner_refusal
        request = case["input"]
        return socket_owner_refusal(request["serving_pid"], request["identify"])
    if case["op"] == "chain_rewrite":
        from plugins.gateway_restart.tool import _socket_chain_rewrites_serving_process
        request = case["input"]
        return _socket_chain_rewrites_serving_process(
            dict(request["ack"]), request["serving_pid"], request["identify"])
    if case["op"] == "turn_notes":
        request = case["input"]
        identity = request["identity"]
        fields = {"pid": identity.get("pid"), "start_time": identity.get("start_time"),
                  "code_sha": identity.get("code_sha")}
        with patch.object(si, "_static_identity_fields", lambda: fields):
            runtime = si.serving_runtime_note(None)
            transition = si.serving_transition_note(
                json.loads(request["stored"]) if request["stored"] else None,
                identity) or ""
        return [note for note in (runtime, transition) if note]
    request = case["input"]
    identify = request.get("identify")
    state = request.get("state") or {}
    return si.serving_status_lines(
        identify=identify,
        state_pid=state.get("pid"),
        state_gateway_state=state.get("gateway_state"),
        state_pid_is_live=bool(state.get("pid_is_live", False)),
        state_is_stale=bool(state.get("is_stale", False)),
        state_restart_requested=state.get("restart_requested"),
        chained=request.get("chained") or (
            {"source": request["chained_source"]} if request.get("chained_source") else None),
        successor_identity=(
            (lambda pid, start_time: request["successor_identity"])
            if request.get("successor_identity") else None),
        gateway_pids=request.get("gateway_pids"),
    )


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_module_matches_the_shared_table(case):
    if case["op"] in _VALUE_OPS:
        assert _python_render(case) == case["expect"]
    else:
        assert _python_render(case) == case["expect_lines"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_module_matches_the_running_go_binary(case, go_servingidentity):
    """The real Go binary renders the same record: Python module == Go output.

    Skipped only where no Go toolchain exists (CI builds it; the Go tests run
    the same table as a second cross-check).
    """
    if go_servingidentity is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_servingidentity], input=json.dumps({"op": case["op"], **case["input"]}),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = _python_render(case)
    if case["op"] in _BARE_OUTPUT_OPS:
        assert rendered == run.stdout.rstrip("\n")
    else:
        assert rendered == json.loads(run.stdout)


def test_shared_table_covers_the_honesty_contract():
    """Every proof-gating branch of the ladder appears in the table, so both
    twins are pinned on exactly the claims that were once false."""
    cases = _shared_cases()
    names = {case["name"] for case in cases}
    for required in (
        # notes: readiness-acked vs spawned vs no successor
        "note_draining_ready_successor",
        "note_draining_spawned_unattested",
        "note_draining_no_successor",
        # status: green only with ack+fingerprint; never green otherwise
        "status_ready_live_successor_green",
        "status_spawned_unattested_starting",
        "status_ready_but_fingerprint_unknown",
        "status_pid_reuse_dead",
        "status_draining_no_successor_unknown",
        "status_role_unknown",
        "status_state_file_draining_never_green",
        # CONTRACT CHANGE: a legacy identify answer (no overlap block) is
        # never green — during overlap that answerer is the draining
        # predecessor; ambiguity clauses name every live candidate
        "status_legacy_identify_no_overlap",
        "status_legacy_identify_multi_process_ambiguous",
        "status_legacy_identify_draining_state_unknown",
        "status_state_file_multi_process_ambiguous",
        "status_no_sources_multi_process_ambiguous",
        "status_serving_green_beside_second_pid",
        # transitions: changed names both processes; everything else silent
        "transition_changed_names_both_processes",
        "transition_same_process_silent",
        "transition_first_turn_silent",
        "transition_unknown_fingerprints_silent",
        # durable record discipline + restart-tool owner gate
        "record_same_process_skip",
        "record_process_change_write",
        "record_invalid_session_skip",
        "owner_guard_legacy_predecessor_refuse",
        "owner_guard_unready_successor_refuse",
        "owner_guard_attested_chain",
        "owner_guard_no_stamp_proceed",
        "owner_refusal_legacy_answerer",
        "turn_notes_first_turn_after_handoff",
        "turn_notes_same_process",
    ):
        assert required in names, f"shared table lost the {required} case"
