"""Cooperative restart: steer live sessions to park, then auto-continue."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

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


def _patch_resume_home(monkeypatch, tmp_path):
    # Patch only this module. Patching hermes_constants.get_hermes_home
    # poisons later imports of restart_loop_guard in the same process.
    monkeypatch.setattr("gateway.restart_wind_down.get_hermes_home", lambda: tmp_path)


@pytest.mark.asyncio
async def test_request_restart_steers_other_sessions_and_marks_them(
    tmp_path, monkeypatch
):
    _patch_resume_home(monkeypatch, tmp_path)
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


def test_empty_active_set_writes_empty_resume_allowlist(tmp_path, monkeypatch):
    from gateway.restart_wind_down import (
        consume_resume_allowlist,
        load_resume_allowlist,
        write_resume_allowlist,
    )

    _patch_resume_home(monkeypatch, tmp_path)
    write_resume_allowlist([])
    assert load_resume_allowlist() == set()
    assert consume_resume_allowlist() == set()
    assert load_resume_allowlist() is None


def test_missing_allowlist_means_crash_path():
    from gateway.restart_wind_down import should_auto_resume_session

    assert should_auto_resume_session("any", None) is True
    assert should_auto_resume_session("live", {"live"}) is True
    assert should_auto_resume_session("stale", {"live"}) is False
    assert should_auto_resume_session("stale", set()) is False


def test_request_restart_with_no_live_chats_still_snapshots_empty_allowlist(
    tmp_path, monkeypatch
):
    from gateway.restart_wind_down import load_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    requester = make_restart_source(chat_id="req")
    runner._restart_command_source = requester
    requester_key = runner._session_key_for_source(requester)
    runner._running_agents[requester_key] = MagicMock()

    steered = runner._request_cooperative_restart_wind_down()
    assert steered == []
    assert runner._cooperative_restart_sessions == []
    assert load_resume_allowlist() == set()


def test_snapshot_includes_active_chats_even_when_steer_is_rejected(
    tmp_path, monkeypatch
):
    from gateway.restart_wind_down import load_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    runner, _adapter = make_restart_runner()
    stubborn = MagicMock()
    stubborn.steer.return_value = False
    runner._running_agents["agent:main:telegram:dm:live"] = stubborn

    steered = runner._request_cooperative_restart_wind_down()
    assert steered == []
    assert load_resume_allowlist() == {"agent:main:telegram:dm:live"}
    assert runner._cooperative_restart_sessions == ["agent:main:telegram:dm:live"]


@pytest.mark.asyncio
async def test_startup_resume_only_revives_snapshotted_active_chats(
    tmp_path, monkeypatch
):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.restart_wind_down import write_resume_allowlist

    _patch_resume_home(monkeypatch, tmp_path)
    write_resume_allowlist(["agent:main:telegram:dm:live"])

    runner, adapter = make_restart_runner()
    runner._persist_active_agents = MagicMock()
    live_source = make_restart_source(chat_id="live")
    stale_source = make_restart_source(chat_id="stale")
    now = datetime.now()
    live = SessionEntry(
        session_key="agent:main:telegram:dm:live",
        session_id="sid-live",
        created_at=now,
        updated_at=now,
        origin=live_source,
        platform=live_source.platform,
        chat_type="dm",
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    stale = SessionEntry(
        session_key="agent:main:telegram:dm:stale",
        session_id="sid-stale",
        created_at=now,
        updated_at=now,
        origin=stale_source,
        platform=stale_source.platform,
        chat_type="dm",
        resume_pending=True,
        resume_reason=COOPERATIVE_RESTART_REASON,
        last_resume_marked_at=now,
    )
    runner.session_store._entries = {
        live.session_key: live,
        stale.session_key: stale,
    }
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_called_once()
    event = adapter.handle_message.call_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.source == live_source
    assert event.message_type == MessageType.TEXT
