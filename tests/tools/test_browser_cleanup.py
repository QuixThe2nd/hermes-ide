"""Camofox session cleanup, owner-scope, and atexit contracts."""

from unittest.mock import patch

import pytest

from tools import browser_tool_lifecycle as bt_lifecycle


@pytest.fixture
def browser_state():
    from tools import browser_tool

    names = (
        "_active_sessions",
        "_session_last_activity",
        "_session_owner_homes",
        "_cleanup_failures",
    )
    saved = {name: getattr(browser_tool, name).copy() for name in names}
    cleanup_done = browser_tool._cleanup_done
    timeout = browser_tool.BROWSER_SESSION_INACTIVITY_TIMEOUT
    for name in names:
        getattr(browser_tool, name).clear()
    browser_tool._cleanup_done = False
    yield browser_tool
    browser_tool._cleanup_done = cleanup_done
    browser_tool.BROWSER_SESSION_INACTIVITY_TIMEOUT = timeout
    for name, state in saved.items():
        live = getattr(browser_tool, name)
        live.clear()
        live.update(state)


def test_ephemeral_cleanup_closes_camofox_and_clears_tracking(browser_state):
    browser_state._active_sessions["task-1"] = {"backend": "camofox"}
    browser_state._session_last_activity["task-1"] = 123.0

    with (
        patch("tools.browser_camofox.camofox_soft_cleanup", return_value=False),
        patch("tools.browser_camofox.camofox_close") as close,
    ):
        bt_lifecycle.cleanup_browser("task-1")

    close.assert_called_once_with("task-1")
    assert "task-1" not in browser_state._active_sessions
    assert "task-1" not in browser_state._session_last_activity


def test_managed_cleanup_preserves_remote_profile(browser_state):
    browser_state._active_sessions["task-1"] = {"backend": "camofox"}

    with (
        patch("tools.browser_camofox.camofox_soft_cleanup", return_value=True) as soft_cleanup,
        patch("tools.browser_camofox.camofox_close") as close,
    ):
        bt_lifecycle.cleanup_browser("task-1")

    soft_cleanup.assert_called_once_with("task-1")
    close.assert_not_called()
    assert browser_state._active_sessions == {}


def test_cleanup_all_includes_direct_camofox_sessions(browser_state):
    from tools import browser_camofox

    browser_state._active_sessions["facade-task"] = {"backend": "camofox"}
    # Other tests in the browser suite may leave entries in _sessions; isolate this test.
    with browser_camofox._sessions_lock:
        saved_sessions = dict(browser_camofox._sessions)
        browser_camofox._sessions.clear()
        browser_camofox._sessions["direct-task"] = {
            "user_id": "u1",
            "tab_id": "tab-1",
            "session_key": "s1",
            "managed": False,
        }
    try:
        with patch("tools.browser_tool_lifecycle.cleanup_browser") as cleanup:
            bt_lifecycle.cleanup_all_browsers()
        assert {call.args[0] for call in cleanup.call_args_list} == {"facade-task", "direct-task"}
    finally:
        with browser_camofox._sessions_lock:
            browser_camofox._sessions.clear()
            browser_camofox._sessions.update(saved_sessions)


def test_emergency_cleanup_clears_tracking_after_cleanup(browser_state):
    browser_state._active_sessions["task-1"] = {"backend": "camofox"}
    browser_state._active_sessions["task-2"] = {"backend": "camofox"}
    browser_state._session_last_activity.update({"task-1": 1.0, "task-2": 2.0})

    with patch("tools.browser_tool_lifecycle.cleanup_all_browsers") as cleanup_all:
        bt_lifecycle._emergency_cleanup_all_sessions()

    cleanup_all.assert_called_once_with()
    # The mocked teardown does not clear state itself; the one-shot hook is the contract here.
    assert browser_state._cleanup_done is True


def test_janitor_tears_down_under_owner_profile_scope(browser_state, tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    profile = tmp_path / "profiles" / "p1"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("CAMOFOX_URL=http://127.0.0.1:1\n", encoding="utf-8")
    browser_state.BROWSER_SESSION_INACTIVITY_TIMEOUT = 0
    secret_scope.set_multiplex_active(True)
    try:
        home_token = set_hermes_home_override(str(profile))
        scope_token = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(profile))
        try:
            bt_lifecycle._update_session_activity("t1")
            browser_state._active_sessions["t1"] = {"backend": "camofox"}
        finally:
            secret_scope.reset_secret_scope(scope_token)
            reset_hermes_home_override(home_token)
        browser_state._session_last_activity["t1"] -= 10

        seen = {}

        def fake_soft_cleanup(task_id):
            seen["task_id"] = task_id
            seen["home"] = str(get_hermes_home())
            seen["url"] = secret_scope.get_secret("CAMOFOX_URL")
            return True

        with patch("tools.browser_camofox.camofox_soft_cleanup", side_effect=fake_soft_cleanup):
            bt_lifecycle._cleanup_inactive_browser_sessions()
    finally:
        secret_scope.set_multiplex_active(False)

    assert seen == {"task_id": "t1", "home": str(profile), "url": "http://127.0.0.1:1"}
    assert "t1" not in browser_state._session_last_activity
    assert "t1" not in browser_state._active_sessions
    assert "t1" not in browser_state._session_owner_homes


def test_repeated_janitor_failures_force_reap_tracking(browser_state):
    browser_state.BROWSER_SESSION_INACTIVITY_TIMEOUT = 0
    browser_state._active_sessions["t1"] = {"backend": "camofox"}
    browser_state._session_last_activity["t1"] = 1.0

    with patch("tools.browser_tool_lifecycle._cleanup_single_browser_session", side_effect=RuntimeError("boom")):
        for _ in range(browser_state.MAX_INACTIVITY_CLEANUP_FAILURES - 1):
            bt_lifecycle._cleanup_inactive_browser_sessions()
        bt_lifecycle._update_session_activity("t1")
        browser_state._session_last_activity["t1"] = 1.0
        assert browser_state._cleanup_failures["t1"] == browser_state.MAX_INACTIVITY_CLEANUP_FAILURES - 1
        assert "t1" in browser_state._active_sessions

        bt_lifecycle._cleanup_inactive_browser_sessions()

    assert "t1" not in browser_state._active_sessions
    assert "t1" not in browser_state._session_last_activity
    assert "t1" not in browser_state._cleanup_failures


def test_second_ctrl_c_during_janitor_join_does_not_propagate(browser_state, monkeypatch):
    class _InterruptedJoin:
        def join(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(browser_state, "_cleanup_thread", _InterruptedJoin())
    monkeypatch.setattr(browser_state, "_cleanup_running", True)
    bt_lifecycle._stop_browser_cleanup_thread()
    assert browser_state._cleanup_running is False


def test_atexit_hooks_stay_silent_when_origin_import_fails(browser_state, monkeypatch):
    import tools.browser_tool_origin as origin_mod

    def _boom(_depth=2):
        raise ImportError("cannot import name 'file_signature' from 'utils'")

    monkeypatch.setattr(origin_mod, "origin_module", _boom)
    bt_lifecycle._emergency_cleanup_all_sessions()
    bt_lifecycle._stop_browser_cleanup_thread()
