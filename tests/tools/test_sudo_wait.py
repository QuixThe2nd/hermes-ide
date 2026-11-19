"""Interactive sudo waits are bounded and excluded from tool deadlines."""

from __future__ import annotations

import threading

import pytest

import tools.session_context as session_context
import tools.sudo_wait as sudo_wait
import tools.terminal_tool as terminal_tool
import tools.terminal_tool_sudo as terminal_tool_sudo


@pytest.fixture(autouse=True)
def _clean_human_wait_state():
    with sudo_wait._human_wait_lock:
        sudo_wait._human_wait_states.clear()
    yield
    with sudo_wait._human_wait_lock:
        sudo_wait._human_wait_states.clear()


def test_sudo_callback_wait_accrues_human_wait(monkeypatch):
    session = "sudo-test-session"
    monkeypatch.setattr(session_context, "get_current_session_key", lambda default="": session)

    def _slow_callback():
        import time

        time.sleep(0.3)
        return "pw"

    monkeypatch.setattr(terminal_tool, "_get_sudo_password_callback", lambda: _slow_callback)
    before = sudo_wait.human_wait_seconds(session)

    password = terminal_tool_sudo._prompt_for_sudo_password(timeout_seconds=5)

    assert password == "pw"
    assert sudo_wait.human_wait_seconds(session) > before


def test_thread_join_path_preserves_wait_accounting(monkeypatch):
    session = "sudo-join-session"
    monkeypatch.setattr(session_context, "get_current_session_key", lambda default="": session)
    monkeypatch.setattr(terminal_tool, "_get_sudo_password_callback", lambda: None)
    monkeypatch.setattr(terminal_tool, "_is_windows", False, raising=False)
    before = sudo_wait.human_wait_seconds(session)

    password = terminal_tool_sudo._prompt_for_sudo_password(timeout_seconds=1)

    assert password == ""
    assert sudo_wait.human_wait_seconds(session) >= before


def test_sudo_wait_ceiling_is_safe_for_platform_locks():
    from agent.deadline import MAX_SAFE_TIMEOUT_S
    from agent.tool_executor import _authorization_gate_lock_timeout

    ceiling = sudo_wait.human_wait_ceiling()
    lock = threading.Lock()

    assert 0 < ceiling <= MAX_SAFE_TIMEOUT_S
    assert _authorization_gate_lock_timeout() == ceiling
    assert lock.acquire(timeout=ceiling)
    lock.release()
