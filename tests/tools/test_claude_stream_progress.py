"""Stream-progress summary + rejected-tool-loop abort for delegate_claude_agent.

Pins papercut pc_7a7fa57d72fc: a goal-mode run could sit until its
wall-clock timeout retrying one rejected tool (observed: TaskCreate) with
no source change, and the background completion receipt then said
"API calls: 0" because only the never-emitted type=result event carried
activity. ``summarize_claude_stream`` derives activity from the stream;
the wrapper aborts a rejected-tool loop instead of waiting out the hour.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tools.process_registry import format_process_notification, process_registry

# Real fake-binary subprocesses (same live-system guard carve-out the other
# cli-agent suites use); the child stays inside this test's own subtree.
_REAL_SUBPROC = pytest.mark.live_system_guard_bypass


# ---------------------------------------------------------------------------
# JSONL fixture builders — the exact stream-json shapes a run emits
# ---------------------------------------------------------------------------

def _use(call_id: str, name: str, tool_input) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def _assistant(msg_id=None, stop_reason=None, tool_uses=()) -> str:
    blocks = [dict(use, type="tool_use") for use in tool_uses]
    message = {
        "role": "assistant",
        "content": blocks or [{"type": "text", "text": "ok"}],
    }
    if msg_id is not None:
        message["id"] = msg_id
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return json.dumps({"type": "assistant", "message": message})


def _tool_result(call_id: str, text: str, is_error=None) -> str:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    if is_error is not None:
        block["is_error"] = is_error
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [block]}}
    )


def _pair(msg_id, call_id, tool, tool_input, result_text, is_error=None):
    """One tool round trip: assistant tool_use + user tool_result."""
    return [
        _assistant(msg_id, tool_uses=[_use(call_id, tool, tool_input)]),
        _tool_result(call_id, result_text, is_error=is_error),
    ]


def _read_pairs(n: int, start: int = 1):
    lines: list = []
    for i in range(start, start + n):
        lines += _pair(
            f"msg_r{i}",
            f"call_r{i}",
            "Read",
            {"file_path": f"f{i}.py"},
            f"contents {i}",
        )
    return lines


def _rejected_taskcreate_pairs(n: int, subject: str = "fix login"):
    lines: list = []
    for i in range(1, n + 1):
        lines += _pair(
            f"msg_{i}",
            f"call_{i}",
            "TaskCreate",
            {"subject": subject, "description": "d"},
            "rejected: tool not allowed",
            is_error=True,
        )
    return lines


# ---------------------------------------------------------------------------
# Pure summary: activity accounting
# ---------------------------------------------------------------------------

def test_api_calls_counts_distinct_assistant_ids_not_system_events():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        [
            _assistant("msg_a", tool_uses=[_use("c1", "TaskCreate", {"subject": "a"})]),
            _tool_result("c1", "Task #1 created successfully: a"),
            _assistant("msg_b", tool_uses=[_use("c2", "TaskCreate", {"subject": "b"})]),
            _tool_result("c2", "Task #2 created successfully: b"),
            json.dumps(
                {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 1}
            ),
        ]
    )
    summary = summarize_claude_stream(log)
    assert summary["api_calls"] == 2
    assert summary["assistant_records"] == 2


def test_api_calls_duplicate_message_id_counts_once():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        [
            _assistant("msg_same", tool_uses=[_use("c1", "Read", {"file_path": "a.py"})]),
            _tool_result("c1", "contents"),
            _assistant("msg_same", tool_uses=[_use("c2", "Read", {"file_path": "b.py"})]),
            _tool_result("c2", "contents"),
        ]
    )
    assert summarize_claude_stream(log)["api_calls"] == 1


def test_api_calls_falls_back_to_stop_reason_without_ids():
    from tools.claude_stream_progress import summarize_claude_stream

    log = _assistant(
        stop_reason="tool_use",
        tool_uses=[_use("c1", "Bash", {"command": "pytest -q"})],
    )
    assert summarize_claude_stream(log)["api_calls"] == 1


def test_api_calls_falls_back_to_event_count_without_ids_or_stop_reason():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        [
            _assistant(tool_uses=[_use("c1", "Read", {"file_path": "a.py"})]),
            _assistant(tool_uses=[_use("c2", "Read", {"file_path": "b.py"})]),
        ]
    )
    assert summarize_claude_stream(log)["api_calls"] == 2


def test_empty_and_malformed_input_is_inert():
    from tools.claude_stream_progress import summarize_claude_stream

    non_dict_line = json.dumps([1, 2])
    for log in ("", "\n\n", "not json\n", f"{non_dict_line}\n"):
        summary = summarize_claude_stream(log)
        assert summary["api_calls"] == 0
        assert summary["abort"] is False
        assert summary["blocker"] is None


# ---------------------------------------------------------------------------
# Pure summary: rejected-tool loop detection
# ---------------------------------------------------------------------------

def test_three_identical_rejections_abort_with_identical_blocker():
    from tools.claude_stream_progress import summarize_claude_stream

    summary = summarize_claude_stream("\n".join(_rejected_taskcreate_pairs(3)))
    assert summary["abort"] is True
    assert "repeated identical TaskCreate attempts without progress" in summary["blocker"]
    assert "last tool TaskCreate" in summary["blocker"]
    assert "rejected: tool not allowed" in summary["blocker"]


def test_two_rejections_do_not_abort():
    from tools.claude_stream_progress import summarize_claude_stream

    summary = summarize_claude_stream("\n".join(_rejected_taskcreate_pairs(2)))
    assert summary["abort"] is False
    assert summary["blocker"] is None


def test_three_rejections_with_different_inputs_abort_without_identical():
    from tools.claude_stream_progress import summarize_claude_stream

    lines: list = []
    for i in range(1, 4):
        lines += _pair(
            f"msg_{i}",
            f"call_{i}",
            "TaskCreate",
            {"subject": f"subject {i}"},
            f"rejected: tool not allowed ({i})",
            is_error=True,
        )
    summary = summarize_claude_stream("\n".join(lines))
    assert summary["abort"] is True
    assert "last tool TaskCreate" in summary["blocker"]
    assert "rejected: tool not allowed (3)" in summary["blocker"]
    assert "identical" not in summary["blocker"]


def test_is_error_false_is_never_a_rejection():
    from tools.claude_stream_progress import summarize_claude_stream

    lines: list = []
    for i in range(1, 4):
        lines += _pair(
            f"msg_{i}",
            f"call_{i}",
            "TaskCreate",
            {"subject": "s"},
            "rejected: tool not allowed",
            is_error=False,
        )
    assert summarize_claude_stream("\n".join(lines))["abort"] is False


def test_progress_tool_between_rejections_prevents_abort():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        _pair("m1", "c1", "TaskCreate", {"subject": "s"},
              "rejected: tool not allowed", is_error=True)
        + _pair("m2", "c2", "Edit", {"file_path": "a.py"}, "updated a.py")
        + _pair("m3", "c3", "TaskCreate", {"subject": "s"},
                "rejected: tool not allowed", is_error=True)
    )
    assert summarize_claude_stream(log)["abort"] is False


# ---------------------------------------------------------------------------
# Pure summary: no-progress tail (receipt annotation, never an abort)
# ---------------------------------------------------------------------------

def test_five_successful_same_tool_attempts_set_tail_blocker_without_abort():
    from tools.claude_stream_progress import summarize_claude_stream

    lines: list = []
    for i in range(1, 6):
        lines += _pair(
            f"msg_{i}",
            f"call_{i}",
            "TaskCreate",
            {"subject": f"s{i}"},
            f"Task #{i} created successfully: s{i}",
        )
    summary = summarize_claude_stream("\n".join(lines))
    assert summary["abort"] is False
    assert "repeated TaskCreate attempts without progress" in summary["blocker"]
    assert "last tool TaskCreate" in summary["blocker"]
    assert "Task #5 created successfully: s5" in summary["blocker"]


def test_progress_tool_attempt_suppresses_tail_blocker():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        _pair("msg_e", "c_e", "Edit", {"file_path": "a.py"},
              "The file a.py has been updated.")
        + _read_pairs(5)
    )
    summary = summarize_claude_stream(log)
    assert summary["blocker"] is None
    assert summary["abort"] is False


def test_long_read_dump_is_not_a_rejection_but_trips_tail():
    from tools.claude_stream_progress import summarize_claude_stream

    dump = "def f():\n    # error handling lives here\n" * 20
    assert len(dump) >= 400
    lines: list = []
    for i in range(1, 6):
        lines += _pair(f"msg_{i}", f"call_{i}", "Read",
                       {"file_path": f"f{i}.py"}, dump)
    summary = summarize_claude_stream("\n".join(lines))
    assert summary["abort"] is False
    assert "last tool Read" in summary["blocker"]


def test_readonly_bash_is_not_progress():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        _pair("msg_b", "b1", "Bash", {"command": "git status"},
              "nothing to commit")
        + _read_pairs(5)
    )
    assert summarize_claude_stream(log)["blocker"] is not None


def test_mutating_bash_is_progress():
    from tools.claude_stream_progress import summarize_claude_stream

    log = "\n".join(
        _pair("msg_b", "b1", "Bash", {"command": "pytest -q"}, "3 passed")
        + _read_pairs(5)
    )
    summary = summarize_claude_stream(log)
    assert summary["blocker"] is None
    assert summary["abort"] is False


# ---------------------------------------------------------------------------
# Integration: live abort of a rejected-tool loop
# ---------------------------------------------------------------------------

def _patch_binary(monkeypatch, binary: Path) -> None:
    monkeypatch.setattr(
        "tools.claude_agent_tool.resolve_claude_binary",
        lambda model=None: str(binary),
    )


def _write_stream_fake(
    tmp_path: Path, events: str, sleep_seconds: float, then_result: str = ""
) -> Path:
    """A fake wrapper that prints ``events``, sleeps, then prints a result."""
    script = f"""#!{sys.executable}
import sys, time
sys.stdout.write({events!r})
sys.stdout.flush()
time.sleep({sleep_seconds!r})
sys.stdout.write({then_result!r})
sys.stdout.flush()
"""
    binary = tmp_path / "claude-glm-stream"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@_REAL_SUBPROC
def test_rejected_tool_loop_aborts_live_run_before_timeout(monkeypatch, repo, tmp_path):
    """The "do not wait an hour" pin: three identical rejected TaskCreate
    attempts kill the run in seconds instead of riding the wall clock."""
    from tools import claude_agent_tool

    events = "\n".join(_rejected_taskcreate_pairs(3)) + "\n"
    binary = _write_stream_fake(tmp_path, events, sleep_seconds=30.0)
    _patch_binary(monkeypatch, binary)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="finish the work",
            workdir=str(repo),
            timeout_seconds=0,
        )
    )
    assert result["success"] is False
    assert result["error"].startswith("tool_loop")
    assert "last tool TaskCreate" in result["error"]
    assert "rejected: tool not allowed" in result["error"]
    assert result["duration_seconds"] < 15


@_REAL_SUBPROC
def test_two_rejections_then_success_still_completes(monkeypatch, repo, tmp_path):
    from tools import claude_agent_tool

    events = "\n".join(_rejected_taskcreate_pairs(2)) + "\n"
    result_event = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s1",
            "num_turns": 2,
            "duration_ms": 5,
            "total_cost_usd": 0.0,
            "modelUsage": {},
        }
    ) + "\n"
    binary = _write_stream_fake(
        tmp_path, events, sleep_seconds=0.0, then_result=result_event
    )
    _patch_binary(monkeypatch, binary)
    monkeypatch.setattr("tools.agent_cli_runner._MONITOR_POLL_SECONDS", 0.01)

    result = json.loads(
        claude_agent_tool.delegate_claude_agent(
            task="finish the work",
            workdir=str(repo),
            timeout_seconds=0,
        )
    )
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Integration: background timeout receipt wiring
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Scratch HERMES_HOME + a drained completion queue per test."""
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


def _patch_delivery(monkeypatch):
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True
    )


def _drain_one(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    return workdir


def _patch_timeout_run(monkeypatch, tmp_path: Path, fixture_log: str) -> Path:
    """Make _run_and_stream report a 1h timeout over the fixture log."""
    from tools import claude_agent_tool

    fake_log = tmp_path / "fake-run.jsonl"
    fake_log.write_text(fixture_log, encoding="utf-8")

    def _fake_run_and_stream(*args, **kwargs):
        return ("timeout", str(fake_log), fixture_log, 3600.0, -15)

    monkeypatch.setattr(claude_agent_tool, "_run_and_stream", _fake_run_and_stream)
    return fake_log


def test_timeout_receipt_carries_derived_api_calls_and_blocker(
    monkeypatch, repo, tmp_path
):
    """2 assistant ids + 5 successful TaskCreates, no result event: the
    receipt must show the derived activity and the tail blocker."""
    from tools import claude_agent_tool

    fixture = "\n".join(
        [
            _assistant(
                "msg_1",
                tool_uses=[
                    _use("c1", "TaskCreate", {"subject": "s1"}),
                    _use("c2", "TaskCreate", {"subject": "s2"}),
                    _use("c3", "TaskCreate", {"subject": "s3"}),
                ],
            ),
            _tool_result("c1", "Task #1 created successfully: s1"),
            _tool_result("c2", "Task #2 created successfully: s2"),
            _tool_result("c3", "Task #3 created successfully: s3"),
            _assistant(
                "msg_2",
                tool_uses=[
                    _use("c4", "TaskCreate", {"subject": "s4"}),
                    _use("c5", "TaskCreate", {"subject": "s5"}),
                ],
            ),
            _tool_result("c4", "Task #4 created successfully: s4"),
            _tool_result("c5", "Task #5 created successfully: s5"),
        ]
    )
    _patch_timeout_run(monkeypatch, tmp_path, fixture)
    _patch_delivery(monkeypatch)

    out = claude_agent_tool.delegate_claude_agent(
        task="finish the work", workdir=str(repo), background=True
    )
    envelope = json.loads(out)
    assert envelope["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None, "background run never produced a terminal event"
    assert evt["status"] == "timeout"
    assert evt["api_calls"] == 2
    assert evt["error"].startswith("timeout:")
    assert "last tool TaskCreate" in evt["error"]

    rendered = format_process_notification(evt)
    assert rendered is not None
    assert "API calls: 2" in rendered
    assert "API calls: 0" not in rendered


def test_timeout_receipt_without_blocker_keeps_plain_timeout_error(
    monkeypatch, repo, tmp_path
):
    """One Read and no result event: no tail, no blocker — the error stays
    exactly 'timeout'."""
    from tools import claude_agent_tool

    fixture = "\n".join(
        _pair("msg_1", "c1", "Read", {"file_path": "a.py"}, "file contents")
    )
    _patch_timeout_run(monkeypatch, tmp_path, fixture)
    _patch_delivery(monkeypatch)

    out = claude_agent_tool.delegate_claude_agent(
        task="finish the work", workdir=str(repo), background=True
    )
    assert json.loads(out)["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["status"] == "timeout"
    assert evt["api_calls"] == 1
    assert evt["error"] == "timeout"
