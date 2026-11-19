"""Terminal and execute_code preserve real interrupt behavior without approvals."""

import json
import threading
import time

import pytest

from tools import terminal_tool as tt
from tools.interrupt import (
    set_interrupt,
    is_interrupted,
    _interrupted_threads,
    _lock,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "logs").mkdir(exist_ok=True)
    with _lock:
        _interrupted_threads.clear()
    yield
    with _lock:
        _interrupted_threads.clear()


def _wait_for_sentinel(sentinel, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sentinel.exists():
            return True
        time.sleep(0.02)
    return sentinel.exists()


def test_terminal_preexisting_interrupt_still_interrupts():
    set_interrupt(True)
    assert is_interrupted()

    result = json.loads(tt.terminal_tool(command="sleep 0.5; echo DONE"))

    assert result["exit_code"] == 130, result
    assert "[Command interrupted]" in result["output"]


@pytest.mark.platforms("linux")
def test_terminal_genuine_interrupt_after_start_still_kills(tmp_path):
    sentinel = tmp_path / "cmd_started"
    holder = {}

    def worker():
        holder["result"] = tt.terminal_tool(
            command=f"touch {sentinel}; sleep 5; echo DONE"
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert _wait_for_sentinel(sentinel), "command did not start"
    set_interrupt(True, thread_id=thread.ident)
    thread.join(timeout=15)
    assert not thread.is_alive(), "worker did not exit after a genuine interrupt"

    result = json.loads(holder["result"])
    assert result["exit_code"] == 130, result
    assert "[Command interrupted]" in result["output"]
    set_interrupt(False, thread_id=thread.ident)


def test_retry_backoff_does_not_clear_genuine_interrupt(monkeypatch):
    from tools.environments.local import LocalEnvironment

    attempts = {"n": 0}
    retry_state = {"interrupted": None}

    def fake_execute(self, command, **kw):
        if "sleep 1" not in command:
            return {"output": "", "returncode": 0}
        attempts["n"] += 1
        if attempts["n"] == 1:
            set_interrupt(True)
            raise RuntimeError("transient backend error")
        retry_state["interrupted"] = is_interrupted()
        return {"output": "partial\n[Command interrupted]", "returncode": 130}

    monkeypatch.setattr(LocalEnvironment, "execute", fake_execute)
    monkeypatch.setattr("tools.terminal_tool.time.sleep", lambda *a, **k: None)
    set_interrupt(False)

    result = json.loads(tt.terminal_tool(command="sleep 1", task_id="retry-test"))

    assert attempts["n"] == 2, attempts
    assert retry_state["interrupted"] is True
    assert result["exit_code"] == 130, result


def test_execute_code_preexisting_interrupt_still_interrupts():
    from tools.code_execution_tool import execute_code

    set_interrupt(True)
    assert is_interrupted()

    result = json.loads(execute_code(
        code='import time; time.sleep(0.5); print("CODE_DONE")',
        task_id="test-interrupt",
    ))

    assert "CODE_DONE" not in result["output"], result
    assert result["status"] == "interrupted", result
    assert result["output"] == "[execution interrupted]"
