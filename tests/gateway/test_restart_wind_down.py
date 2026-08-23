"""Cooperative restart: steer live sessions to park, then auto-continue."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import asyncio
import pytest

from gateway.restart_wind_down import (
    COOPERATIVE_RESTART_REASON,
    COOPERATIVE_RESTART_STEER,
    is_cooperative_restart_reason,
    mark_cooperative_restart_sessions,
    should_preserve_cooperative_restart_marker,
    steer_running_agents_for_restart,
)
from gateway.run import GatewayRunner, build_resume_recovery_note
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def test_cooperative_reason_is_recognized():
    assert is_cooperative_restart_reason(COOPERATIVE_RESTART_REASON) is True
    assert is_cooperative_restart_reason("restart_timeout") is False
    assert is_cooperative_restart_reason(None) is False


def test_preserve_marker_only_during_drain_for_coop_reason():
    assert (
        should_preserve_cooperative_restart_marker(
            draining=True, resume_reason=COOPERATIVE_RESTART_REASON
        )
        is True
    )
    assert (
        should_preserve_cooperative_restart_marker(
            draining=False, resume_reason=COOPERATIVE_RESTART_REASON
        )
        is False
    )
    assert (
        should_preserve_cooperative_restart_marker(
            draining=True, resume_reason="restart_timeout"
        )
        is False
    )


def test_steer_skips_requester_and_is_idempotent():
    runner, _adapter = make_restart_runner()
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    requester_key = runner._session_key_for_source(requester)

    other = MagicMock()
    other.steer.return_value = True
    requester_agent = MagicMock()
    requester_agent.steer.return_value = True
    runner._running_agents[requester_key] = requester_agent
    runner._running_agents["agent:main:telegram:dm:other"] = other

    first = steer_running_agents_for_restart(runner)
    assert first == ["agent:main:telegram:dm:other"]
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    requester_agent.steer.assert_not_called()

    runner._cooperative_restart_sessions = first
    second = steer_running_agents_for_restart(runner)
    assert second == []
    assert other.steer.call_count == 1


def test_steer_skips_agents_without_steer():
    runner, _adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:1"] = object()
    assert steer_running_agents_for_restart(runner) == []


def test_mark_resume_pending_uses_cooperative_reason():
    store = MagicMock()
    store.mark_resume_pending.return_value = True
    runner = MagicMock()
    runner.session_store = store
    marked = mark_cooperative_restart_sessions(
        runner, ["agent:main:telegram:dm:other"]
    )
    assert marked == 1
    store.mark_resume_pending.assert_called_once_with(
        "agent:main:telegram:dm:other", COOPERATIVE_RESTART_REASON
    )


@pytest.mark.asyncio
async def test_request_restart_steers_other_sessions_and_marks_them():
    runner, _adapter = make_restart_runner()
    runner.stop = MagicMock()
    runner._launch_detached_restart_command = MagicMock()
    runner._restart_after_turn_timeout = 0.0
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    requester_key = runner._session_key_for_source(requester)

    other = MagicMock()
    other.steer.return_value = True
    runner._running_agents["agent:main:telegram:dm:other"] = other
    runner._running_agents[requester_key] = MagicMock()
    runner.session_store.mark_resume_pending.return_value = True

    assert runner.request_restart(detached=False, via_service=True) is True
    other.steer.assert_called_once_with(COOPERATIVE_RESTART_STEER)
    runner.session_store.mark_resume_pending.assert_called_once_with(
        "agent:main:telegram:dm:other", COOPERATIVE_RESTART_REASON
    )
    assert runner._cooperative_restart_sessions == [
        "agent:main:telegram:dm:other"
    ]
    if runner._restart_task is not None:
        runner._restart_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner._restart_task


def test_cooperative_reason_is_auto_resumed():
    assert COOPERATIVE_RESTART_REASON in GatewayRunner._AUTO_RESUME_REASONS


def test_cooperative_resume_note_continues_parked_work():
    note = build_resume_recovery_note(
        COOPERATIVE_RESTART_REASON, "", interactive=True
    )
    assert "cooperative gateway restart" in note
    assert "CONTINUE the parked task" in note
    assert "ask what they would like to do next" not in note
    assert "Do not ask what next" in note


def test_session_store_accepts_cooperative_reason(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore, SessionSource
    from gateway.config import Platform

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1",
        chat_type="dm",
        user_id="u1",
    )
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key, COOPERATIVE_RESTART_REASON)
    assert store._entries[entry.session_key].resume_reason == COOPERATIVE_RESTART_REASON
    assert store._entries[entry.session_key].resume_pending is True


def test_session_entry_roundtrip_keeps_cooperative_reason():
    now = datetime.now()
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:1",
        session_id="sid",
        created_at=now,
        updated_at=now,
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    restored = SessionEntry.from_dict(entry.to_dict())
    assert restored.resume_reason == COOPERATIVE_RESTART_REASON
    assert restored.resume_pending is True
