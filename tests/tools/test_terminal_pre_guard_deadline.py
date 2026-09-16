"""Terminal pre-execution guards share the command's wall-clock deadline (#111922).

Adapted from upstream tests/tools/test_terminal_pre_guard_deadline.py
(c1e749d679 + c832920275) onto this fork's seams: the fork monolith has no
_plan_execution/_acquire_env/_run_foreground split, so tests stub
_get_env_config/_create_environment and read the JSON envelope terminal_tool
returns instead of a bare string. A guard that misses the deadline fails
CLOSED: the command is refused, never executed unguarded.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import tools.terminal_tool as terminal_module


def _install_stub_env(monkeypatch):
    """Stub config/env/approval/plugin stages; returns the execution recorder."""
    calls: list[str] = []

    def _stub_execute(*_a, **_k):
        calls.append("executed")
        return {"output": "foreground-ran", "returncode": 0}

    def _stub_create(**_kwargs):
        return SimpleNamespace(execute=_stub_execute, cwd="/tmp")

    monkeypatch.setattr(terminal_module, "_active_environments", {})
    monkeypatch.setattr(
        terminal_module, "_get_env_config",
        lambda: {"env_type": "local", "timeout": 10, "cwd": "/tmp",
             "lifetime_seconds": 900},
    )
    monkeypatch.setattr(terminal_module, "_create_environment", _stub_create)
    # Upstream's tests mock _run_approval_guards; on this fork the approval
    # stage is the inline _check_all_guards call, so mock the same stage.
    monkeypatch.setattr(
        terminal_module, "_check_all_guards",
        lambda *_a, **_k: {"approved": True},
    )
    # Upstream's tests mock _run_foreground, which also skips the
    # transform_terminal_output plugin hook; on this fork that hook runs
    # inline and its first call scans the whole plugin tree (seconds).
    import hermes_cli.lifecycle as _lifecycle
    monkeypatch.setattr(_lifecycle, "invoke_hook", lambda *a, **k: [])
    return calls


def test_terminal_tool_bounds_a_wedged_pre_execution_guard(monkeypatch):
    """A stalled supervised-gateway identity probe cannot wedge terminal_tool,
    and a guard that misses the deadline refuses the command (fail-closed)."""
    calls = _install_stub_env(monkeypatch)

    def _wedged_supervised_gateway_probe(*_a, **_k):
        time.sleep(1)

    monkeypatch.setattr(terminal_module, "_pre_exec_block", _wedged_supervised_gateway_probe)

    start = time.monotonic()
    result = terminal_module.terminal_tool("echo ok", timeout=0.05)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"pre-execution guard wedged terminal_tool for {elapsed:.2f}s"
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["exit_code"] == -1
    assert "did not finish within" in payload["error"]
    assert calls == [], "a wedged guard must refuse the command, not run it"


def test_terminal_tool_runs_normal_pre_execution_guard(monkeypatch):
    """A normal guard result still reaches foreground execution unchanged."""
    _install_stub_env(monkeypatch)
    monkeypatch.setattr(terminal_module, "_pre_exec_block", lambda *_a, **_k: None)

    result = terminal_module.terminal_tool("echo ok")
    payload = json.loads(result)
    assert payload["output"] == "foreground-ran"
    assert payload["exit_code"] == 0


def test_terminal_tool_preserves_pre_execution_rejection(monkeypatch):
    """A completed guard rejection still returns its original tool result."""
    _install_stub_env(monkeypatch)
    rejected_json = json.dumps(
        {"output": "", "exit_code": -1, "error": "nope", "status": "blocked"},
        ensure_ascii=False,
    )

    def _rejecting_pre_exec_block(*_a, **_k):
        raise terminal_module._Rejected(rejected_json)

    monkeypatch.setattr(terminal_module, "_pre_exec_block", _rejecting_pre_exec_block)

    assert terminal_module.terminal_tool("echo ok") == rejected_json
