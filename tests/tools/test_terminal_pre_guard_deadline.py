"""Terminal pre-execution guards must not outlive the tool deadline.

Adapted from upstream tests/tools/test_terminal_pre_guard_deadline.py
(c1e749d679) onto this fork's seams: the fork monolith has no
_plan_execution/_acquire_env/_run_foreground split, so tests stub
_get_env_config/_create_environment and read the JSON envelope
terminal_tool returns instead of a bare string.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import tools.terminal_tool as terminal_module


def _install_stub_env(monkeypatch):
    """Give terminal_tool a cached stub env so no real environment spawns."""

    def _stub_create(**_kwargs):
        return SimpleNamespace(
            execute=lambda *_a, **_k: {"output": "foreground-ran", "returncode": 0},
            cwd="/tmp",
        )

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


def test_terminal_tool_bounds_a_wedged_pre_execution_guard(monkeypatch):
    """A stalled supervised-gateway identity probe cannot wedge terminal_tool."""
    _install_stub_env(monkeypatch)

    def _wedged_supervised_gateway_probe(*_a, **_k):
        time.sleep(1)

    monkeypatch.setattr(terminal_module, "_pre_exec_block", _wedged_supervised_gateway_probe)

    start = time.monotonic()
    result = terminal_module.terminal_tool("echo ok", timeout=0.05)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"pre-execution guard wedged terminal_tool for {elapsed:.2f}s"
    payload = json.loads(result)
    assert payload["output"] == "foreground-ran"
    assert payload["exit_code"] == 0


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
