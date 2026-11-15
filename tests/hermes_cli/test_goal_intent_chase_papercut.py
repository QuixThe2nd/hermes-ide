"""The auto-goal chase papercut is keyed by CAUSE, not the raw user excerpt, and its
``observed`` block carries the evidence a triager needs: the last assistant reply, its
age in minutes (unknown, never an invented 0), and whether background or delegated work
was live for the session when the user chased."""

from unittest.mock import patch

import pytest

import hermes_cli.goal_intent as goal_intent
from hermes_cli.goal_intent import _log_chase_papercut


@pytest.fixture(autouse=True)
def _fresh_chase_dedupe():
    goal_intent._CHASE_LAST_LOGGED.clear()
    yield
    goal_intent._CHASE_LAST_LOGGED.clear()


def _record(message="status?"):
    return {
        "session_id": "sess-1",
        "platform": "telegram",
        "message_excerpt": message,
        "label": "chase",
        "reason": "jev decision",
        "confidence": 0.9,
    }


def _context(last_assistant="The deploy is running.", age_minutes=4.0):
    return {
        "goal_active": True,
        "goal_recently_completed": False,
        "goal_paused": False,
        "last_assistant": last_assistant,
        "user_previous_message": "",
        "last_assistant_age_minutes": age_minutes,
    }


def _dormant_context(age_minutes=1129.1):
    context = _context(last_assistant="", age_minutes=age_minutes)
    context.update(
        goal_active=False,
        goal_recently_completed=False,
        goal_paused=False,
    )
    return context


def _log(context, live=False, bg_process=False, session="sess-1"):
    with (
        patch("plugins.papercuts.handle_papercuts", return_value='{"success":true}') as handle,
        patch("tools.async_delegation.has_live_for_session", return_value=live),
        patch(
            "tools.process_registry.process_registry.has_active_for_session",
            return_value=bg_process,
        ) as has_active,
    ):
        ok = _log_chase_papercut(session, _record(), context)
    assert ok is True
    return handle.call_args.args[0], has_active


def test_observed_carries_snippet_age_and_live_flag():
    args, has_active = _log(_context("Still deploying the cluster." * 20, age_minutes=7.5))
    has_active.assert_called_once()
    observed = args["observed"]
    assert "Still deploying the cluster." in observed
    assert "minutes_since_last_assistant=7.5" in observed
    # The flag is present even when false.
    assert "background_work_live=False" in observed
    assert args["summary"] != f"User had to chase: status?"


def test_snippet_is_truncated_for_observed():
    long_reply = "x" * 1200
    args, _ = _log(_context(long_reply, age_minutes=None))
    observed = args["observed"]
    assert "x" * 200 in observed
    assert "x" * 201 not in observed
    # No timestamp on the row -> unknown, never an invented 0.
    assert "minutes_since_last_assistant=unknown" in observed


def test_live_work_makes_a_different_papercut_than_no_work():
    live_args, _ = _log(_context(), live=True, session="sess-live")
    idle_args, _ = _log(_context(), live=False, session="sess-idle")
    assert live_args["summary"] != idle_args["summary"]
    assert "background_work_live=True" in live_args["observed"]
    assert "background_work_live=False" in idle_args["observed"]


def test_background_process_counts_as_live_work():
    args, _ = _log(_context(), live=False, bg_process=True)
    assert "background_work_live=True" in args["observed"]




@pytest.mark.parametrize("age_minutes", [1129.1, None])
def test_dormant_session_is_suppressed_without_dedupe_state(age_minutes):
    with (
        patch("plugins.papercuts.handle_papercuts", return_value='{"success":true}') as handle,
        patch("tools.async_delegation.has_live_for_session", return_value=False),
        patch(
            "tools.process_registry.process_registry.has_active_for_session",
            return_value=False,
        ),
    ):
        assert _log_chase_papercut(
            "sess-dormant", _record(), _dormant_context(age_minutes)
        ) is False

    handle.assert_not_called()
    assert goal_intent._CHASE_LAST_LOGGED == {}


def test_dormant_session_with_live_work_still_logs():
    args, _ = _log(_dormant_context(), live=True, session="sess-dormant-live")
    assert args["summary"] == (
        "User had to chase while background or delegated work was live"
    )
    assert "background_work_live=True" in args["observed"]


@pytest.mark.parametrize(
    "goal_flag", ["goal_active", "goal_recently_completed", "goal_paused"]
)
def test_underway_goal_state_still_logs_without_live_work(goal_flag):
    context = _dormant_context()
    context[goal_flag] = True
    args, _ = _log(context, session=f"sess-{goal_flag}")
    assert args["summary"] == (
        "User had to chase while no background or delegated work was live"
    )


def test_recent_assistant_reply_still_logs_without_live_work():
    args, _ = _log(_dormant_context(age_minutes=4.0), session="sess-recent-reply")
    assert "minutes_since_last_assistant=4.0" in args["observed"]
    assert "background_work_live=False" in args["observed"]
