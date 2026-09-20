"""``delegate_claude_agent`` writes its spawn receipt before completion.

The dispatch-card correlation receipt (``tools/claude_run_receipts``)
must exist the moment the child process exists — for the synchronous
(blocking) branch and the background branch alike, while the run is
still going — so a Mission Control card can link to the live viewer
before any tool result row exists. These tests run a real fake-wrapper
subprocess whose LAST act, just before emitting the result event, is to
list the receipts tree: if the receipt is already there, creation
provably preceded completion.

Also pinned: the hidden ``session_id``/``tool_call_id`` pair arrives
through the registry handler exactly as the gateway passes it, and a
run without the pair (or whose receipt write fails) completes with its
normal result shape — the receipt is strictly best-effort.

Run:  scripts/run_tests.sh tests/tools/test_claude_agent_spawn_receipt.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from tools.process_registry import process_registry

# Real fake-binary subprocesses (same live-system guard carve-out the
# other cli-agent suites use); the child stays inside this test's own
# process subtree.
_REAL_SUBPROC = pytest.mark.live_system_guard_bypass

SID = "sess-spawn-1"
CALL = "call-spawn-1"


def _write_fake_binary(tmp_path: Path, home: Path, marker: Path) -> Path:
    """A fake wrapper that records the receipts tree it can see at
    COMPLETION time — in its last act, right before emitting the result
    event — so the marker proves the receipt already existed while the
    run was still going (and did not appear only at teardown)."""
    script = f"""#!{sys.executable}
import json, os, sys, time

time.sleep(0.15)  # the run is genuinely underway by now
receipts = os.path.join({str(home)!r}, "claude-runs", "receipts")
try:
    names = sorted(os.listdir(receipts))
except OSError as exc:
    names = ["ERR", repr(exc)]
with open({str(marker)!r}, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(names))

sys.stdout.write(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False,
    "result": "claude report: done", "session_id": "cc-child-1",
    "num_turns": 1, "duration_ms": 4, "total_cost_usd": 0.01,
    "modelUsage": {{"glm-4.6": 1}},
}}) + "\\n")
sys.stdout.flush()
"""
    binary = tmp_path / "claude-glm"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Scratch HERMES_HOME: run logs AND receipts land here, never in
    the real home."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from tools import async_delegation as ad

    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield home
    deadline = time.monotonic() + 5.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


def _patch_binary(monkeypatch, binary: Path) -> None:
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary",
        lambda model=None: str(binary))


def _patch_delivery(monkeypatch):
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True)


def _dispatch(monkeypatch, repo, home, marker, background=False):
    """One delegated run through the registry handler — the exact call
    path the gateway uses, hidden correlation pair included."""
    import tools.claude_agent_tool as tool

    # Pin the viewer host so watch_url() never falls into the tailscale /
    # default-route auto-detect probes (a subprocess with a 5s timeout):
    # the fake child lists the receipts tree 0.15s in, so the parent's
    # spawn-time receipt write must not wait on host detection.
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"delegation": {"claude_viewer": {"public_host": "127.0.0.1"}}})
    _patch_binary(monkeypatch, _write_fake_binary(
        marker.parent, home, marker))
    if background:
        _patch_delivery(monkeypatch)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS",
                        0.01)
    return tool._handle_delegate_claude_agent(
        {"task": "finish the work", "workdir": str(repo),
         "background": background},
        session_id=SID, tool_call_id=CALL)


def _receipt(home: Path):
    from tools import claude_run_receipts as r

    return r.receipt_path(SID, CALL, home)


def _drain_one(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# Sync path: receipt exists while the child is still running
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_sync_run_writes_receipt_before_completion(
    monkeypatch, repo, home, tmp_path
):
    marker = tmp_path / "sync-marker.json"
    out = _dispatch(monkeypatch, repo, home, marker, background=False)
    parsed = json.loads(out)
    assert parsed["success"] is True

    # The child SAW the receipt mid-run: it listed the receipts tree in
    # its last act, right before emitting the result the parent waited
    # for — so creation provably preceded completion.
    seen = json.loads(marker.read_text(encoding="utf-8"))
    assert seen == [_receipt(home).name]

    # ...and the receipt itself is exact: ids, stem == the run log's,
    # resolver URL, 0600.
    from tools import claude_run_receipts as r

    path = _receipt(home)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == SID
    assert data["tool_call_id"] == CALL
    assert Path(parsed["log_path"]).stem == data["run_stem"]
    assert data["viewer_url"].endswith("#" + data["run_stem"])
    # the task brief never rides along
    assert "finish the work" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Background path: same receipt, same timing guarantee
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_background_run_writes_receipt_before_completion(
    monkeypatch, repo, home, tmp_path
):
    marker = tmp_path / "bg-marker.json"
    envelope = json.loads(
        _dispatch(monkeypatch, repo, home, marker, background=True))
    assert envelope["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None and evt["status"] == "completed"

    seen = json.loads(marker.read_text(encoding="utf-8"))
    assert seen == [_receipt(home).name]
    data = json.loads(_receipt(home).read_text(encoding="utf-8"))
    assert data["session_id"] == SID
    assert data["tool_call_id"] == CALL
    assert Path(evt["log_path"]).stem == data["run_stem"]


# ---------------------------------------------------------------------------
# Absent ids / failed write: the delegation itself is unaffected
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_run_without_correlation_ids_completes_and_writes_nothing(
    monkeypatch, repo, home, tmp_path
):
    import tools.claude_agent_tool as tool

    marker = tmp_path / "noid-marker.json"
    _patch_binary(monkeypatch, _write_fake_binary(tmp_path, home, marker))
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS",
                        0.01)
    out = tool._handle_delegate_claude_agent(
        {"task": "finish the work", "workdir": str(repo)})
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["final_report"] == "claude report: done"
    # no ids -> no receipt, and the receipts tree never even appears
    assert not _receipt(home).exists()
    receipts = home / "claude-runs" / "receipts"
    assert not receipts.exists() or not list(receipts.iterdir())


@_REAL_SUBPROC
def test_receipt_write_failure_leaves_execution_intact(
    monkeypatch, repo, home, tmp_path
):
    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(
        "tools.claude_agent_tool.write_spawn_receipt", _boom)
    marker = tmp_path / "boom-marker.json"
    out = _dispatch(monkeypatch, repo, home, marker, background=False)
    parsed = json.loads(out)
    # the coding task completed normally despite the dead receipt writer
    assert parsed["success"] is True
    assert parsed["final_report"] == "claude report: done"
    assert not _receipt(home).exists()
