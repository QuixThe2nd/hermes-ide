"""``delegate_claude_agent(background=true)`` — the uniform background branch.

Focused acceptance tests for the cli-agent half of the uniform delegation
lifecycle:

- foreground (default) blocks and returns the run's result inline, emitting
  ZERO completion-queue events;
- background returns the shared acceptance envelope immediately (with
  ``tool`` / ``result_kind="cli_agent"``) and exactly ONE terminal event
  later lands on the shared completion rail;
- an unsupported delivery channel is rejected BEFORE any side effect — no
  /goal brief file, no log directory entry, no subprocess;
- a capacity rejection starts nothing and names the foreground alternative;
- ``interrupt_all`` kills the live subprocess and produces an
  ``interrupted`` terminal event.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from tools.process_registry import format_process_notification, process_registry

# Real fake-binary subprocesses (see test_claude_agent_tool.py); the conftest
# live-system guard allows signals inside the test's own process subtree.
_REAL_SUBPROC = pytest.mark.live_system_guard_bypass

_HERMES_HOME_BACKUP = {}


def _write_fake_binary(tmp_path: Path, name: str = "claude-glm") -> Path:
    """A fake wrapper binary that either succeeds immediately or sleeps."""
    script = f"""#!{sys.executable}
import json, os, sys, time

pid_out = os.environ.get("FAKE_CLAUDE_PID_OUT")
if pid_out:
    with open(pid_out, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "30")))
    sys.exit(0)

sys.stdout.write(json.dumps({{
    "type": "result", "subtype": "success", "is_error": False,
    "result": "claude report: done", "session_id": "cc-sess-1",
    "num_turns": 2, "duration_ms": 5, "total_cost_usd": 0.01,
    "modelUsage": {{"glm-4.6": 1}},
}}) + "\\n")
sys.stdout.flush()
"""
    binary = tmp_path / name
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    return _write_fake_binary(tmp_path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Point ``get_hermes_home()`` at a scratch dir so no real run logs land."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _HERMES_HOME_BACKUP["path"] = str(home)

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


def _patch_binary(monkeypatch, binary: Path) -> None:
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary", lambda model=None: str(binary)
    )


def _patch_delivery(monkeypatch, supported: bool):
    """Pin the capability predicate; the uniform gate must consult it."""
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported",
        lambda: supported,
    )
    if not supported:
        monkeypatch.setattr(
            "tools.async_delegation._current_origin_session_id",
            lambda: "",
        )


def _drain_one(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


# ---------------------------------------------------------------------------
# Foreground default: inline result, zero queue events
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_foreground_returns_inline_result_and_emits_no_event(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)
    monkeypatch.setattr(
        "tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01
    )

    out = claude_agent_tool.delegate_claude_agent(
        task="finish the work", workdir=str(repo)
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["final_report"] == "claude report: done"
    assert parsed["session_id"] == "cc-sess-1"
    # Exactly one delivery channel: a foreground run never mints a handle
    # and never touches the completion rail.
    assert "delegation_id" not in parsed
    assert parsed.get("mode") != "background"
    assert process_registry.completion_queue.empty()


def test_schema_advertises_background_default_false():
    from tools.registry import registry

    entry = registry.get_entry("delegate_claude_agent")
    prop = entry.schema["parameters"]["properties"]["background"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert "Blocking by default" in prop["description"]


def test_handler_forwards_background_argument(monkeypatch, repo):
    """The registry handler forwards `background` unchanged — no default
    flipping, no inference from platform/session."""
    import tools.claude_agent_tool as mod

    seen = {}

    def _capture(*a, **kw):
        seen.update(kw)
        return "{}"

    monkeypatch.setattr(mod, "delegate_claude_agent", _capture)

    mod._handle_delegate_claude_agent(
        {"task": "t", "workdir": str(repo), "background": True}, task_id="tk"
    )
    assert seen["background"] is True

    seen.clear()
    mod._handle_delegate_claude_agent({"task": "t", "workdir": str(repo)}, task_id="tk")
    assert seen["background"] is False

    # The legacy truthy spellings normalize the same way the schema promises.
    seen.clear()
    mod._handle_delegate_claude_agent(
        {"task": "t", "workdir": str(repo), "background": "true"}, task_id="tk"
    )
    assert seen["background"] is True
    seen.clear()
    mod._handle_delegate_claude_agent(
        {"task": "t", "workdir": str(repo), "background": ""}, task_id="tk"
    )
    assert seen["background"] is False


# ---------------------------------------------------------------------------
# Background: shared envelope + exactly one terminal event
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_background_returns_envelope_then_one_terminal_event(
    monkeypatch, repo, fake_binary, tmp_path
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)
    monkeypatch.setattr(
        "tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01
    )

    out = claude_agent_tool.delegate_claude_agent(
        task="finish the work", workdir=str(repo), background=True
    )
    envelope = json.loads(out)

    # The shared acceptance envelope — no terminal result inline.
    assert envelope["status"] == "dispatched"
    assert envelope["mode"] == "background"
    assert envelope["tool"] == "delegate_claude_agent"
    assert envelope["result_kind"] == "cli_agent"
    assert envelope["delegation_id"].startswith("deleg_")
    assert envelope["count"] == 1
    assert "final_report" not in envelope
    assert "success" not in envelope

    evt = _drain_one()
    assert evt is not None, "background run never produced a terminal event"
    assert evt["type"] == "async_delegation"
    assert evt["delegation_id"] == envelope["delegation_id"]
    assert evt["status"] == "completed"
    assert evt["summary"] == "claude report: done"
    assert evt["tool"] == "delegate_claude_agent"
    assert evt["result_kind"] == "cli_agent"
    assert evt["background"] is True
    assert evt["log_path"]
    assert evt["child_session_id"] == "cc-sess-1"
    # Exactly one terminal event.
    assert process_registry.completion_queue.empty()

    rendered = format_process_notification(evt)
    assert "ASYNC CLAUDE CODE RUN COMPLETE" in rendered
    assert "claude report: done" in rendered


@_REAL_SUBPROC
def test_background_failure_is_error_status_not_success(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)
    monkeypatch.setattr(
        "tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01
    )
    # A binary that exits non-zero with no result event.
    fake_binary.write_text(
        f"#!{sys.executable}\nimport sys; sys.stdout.write('boom\\n'); sys.exit(3)\n",
        encoding="utf-8",
    )
    fake_binary.chmod(0o755)

    envelope = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="failing work", workdir=str(repo), background=True
        )
    )
    assert envelope["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["status"] == "error"
    assert evt["error"]
    assert not evt.get("summary")


# ---------------------------------------------------------------------------
# Fail clearly: unsupported channel and capacity, both before any work
# ---------------------------------------------------------------------------

def test_unsupported_channel_rejects_before_any_side_effect(
    monkeypatch, repo, fake_binary
):
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, False)

    def _no_spawn(*a, **kw):
        raise AssertionError("a subprocess must never spawn for a rejected call")

    monkeypatch.setattr(
        "tools.claude_agent_tool._run_and_stream", _no_spawn
    )

    out = claude_agent_tool.delegate_claude_agent(
        task="must not start", workdir=str(repo), background=True
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "NO WORK WAS STARTED" in parsed["error"]
    assert "background=false" in parsed["error"]
    assert process_registry.completion_queue.empty()

    from tools import async_delegation as ad

    assert ad.active_count() == 0
    # No run log was created either — the log directory stays empty.
    runs_dir = _HERMES_HOME_BACKUP["path"] and (
        Path(_HERMES_HOME_BACKUP["path"]) / "claude-runs"
    )
    if runs_dir.is_dir():
        assert list(runs_dir.iterdir()) == []


def test_capacity_rejection_starts_no_subprocess(monkeypatch, repo, fake_binary):
    from tools import async_delegation as ad
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)

    def _reject(**kw):
        return {"status": "rejected", "error": "pool at capacity (test)"}

    monkeypatch.setattr(
        "tools.async_delegation.dispatch_background_delegation", _reject
    )

    def _no_spawn(*a, **kw):
        raise AssertionError("a subprocess must never spawn for a rejected call")

    monkeypatch.setattr("tools.claude_agent_tool._run_and_stream", _no_spawn)

    out = claude_agent_tool.delegate_claude_agent(
        task="must not start", workdir=str(repo), background=True
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "NO WORK WAS STARTED" in parsed["error"]
    assert "pool at capacity" in parsed["error"]
    assert "background=false" in parsed["error"]
    assert ad.active_count() == 0
    assert process_registry.completion_queue.empty()


def test_real_capacity_rejection_uses_the_shared_cap(monkeypatch, repo, fake_binary):
    """The real dispatch path rejects (not queues) past max_concurrent_children."""
    from tools import async_delegation as ad
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)
    monkeypatch.setattr(
        "tools.delegate_tool._get_max_async_children", lambda: 0
    )

    out = claude_agent_tool.delegate_claude_agent(
        task="must not start", workdir=str(repo), background=True
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "capacity" in parsed["error"].lower()
    assert ad.active_count() == 0
    assert process_registry.completion_queue.empty()


# ---------------------------------------------------------------------------
# Interrupt -> process kill
# ---------------------------------------------------------------------------

@_REAL_SUBPROC
def test_interrupt_all_kills_background_subprocess(
    monkeypatch, repo, fake_binary, tmp_path
):
    from tools import async_delegation as ad
    from tools import claude_agent_tool

    _patch_binary(monkeypatch, fake_binary)
    _patch_delivery(monkeypatch, True)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    pid_out = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_PID_OUT", str(pid_out))

    envelope = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="long work", workdir=str(repo), background=True
        )
    )
    assert envelope["status"] == "dispatched"
    delegation_id = envelope["delegation_id"]

    # Wait for the child to actually exist before signalling.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not pid_out.exists():
        time.sleep(0.02)
    assert pid_out.exists(), "fake binary never started"
    child_pid = int(pid_out.read_text().strip())
    assert ad.active_count() == 1

    ad.interrupt_all(reason="stop requested")

    evt = _drain_one(timeout=15.0)
    assert evt is not None, "interrupted run never terminalized"
    assert evt["delegation_id"] == delegation_id
    assert evt["status"] == "interrupted"

    # The subprocess is gone.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"child pid {child_pid} survived the interrupt")

    rendered = format_process_notification(evt)
    assert "interrupted" in rendered
