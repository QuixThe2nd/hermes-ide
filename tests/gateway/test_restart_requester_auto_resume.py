"""Auto-resume the idle /restart requester after the comeback notice delivers."""

import asyncio
import json
import threading
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import _AGENT_PENDING_SENTINEL
from gateway.session import SessionEntry, SessionSource, build_session_key
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _wire_session_store(runner, entries: dict[str, SessionEntry]) -> None:
    runner.session_store._entries = entries
    runner.session_store._lock = threading.Lock()
    runner.session_store._ensure_loaded_locked = lambda: None
    runner.session_store._generate_session_key = lambda src: build_session_key(src)

    def _mark_resume_pending(session_key: str, reason: str = "restart_timeout") -> bool:
        entry = entries.get(session_key)
        if entry is None or entry.suspended:
            return False
        entry.resume_pending = True
        entry.resume_reason = reason
        entry.last_resume_marked_at = datetime.now()
        return True

    runner.session_store.mark_resume_pending = _mark_resume_pending


def _idle_requester_entry(source: SessionSource) -> tuple[str, SessionEntry]:
    session_key = build_session_key(source)
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid-idle",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
        resume_pending=False,
    )
    return session_key, entry


@pytest.mark.asyncio
async def test_restart_notification_marks_idle_requester_and_schedules_resume_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    source = make_restart_source(chat_id="42")
    session_key, entry = _idle_requester_entry(source)

    runner, adapter = make_restart_runner()
    _wire_session_store(runner, {session_key: entry})
    runner._restored_source = gateway_run.GatewayRunner._restored_source.__get__(
        runner, gateway_run.GatewayRunner
    )
    runner._adapter_for_source = gateway_run.GatewayRunner._adapter_for_source.__get__(
        runner, gateway_run.GatewayRunner
    )
    runner._run_startup_resume_event = AsyncMock()
    runner._persist_active_agents = MagicMock()

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps(
            {
                "platform": "telegram",
                "chat_id": "42",
                "chat_type": "dm",
            }
        )
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    await runner._send_restart_notification()
    await asyncio.sleep(0)

    assert entry.resume_pending is True
    assert entry.resume_reason == "cooperative_restart"
    assert session_key in runner._running_agents
    assert runner._running_agents[session_key] is _AGENT_PENDING_SENTINEL
    runner._run_startup_resume_event.assert_awaited_once()
    assert runner._run_startup_resume_event.await_args.args[2] == session_key


@pytest.mark.asyncio
async def test_restart_notification_skips_running_or_suspended_requester(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    source = make_restart_source(chat_id="busy")
    session_key, entry = _idle_requester_entry(source)
    entry.suspended = True

    runner, adapter = make_restart_runner()
    _wire_session_store(runner, {session_key: entry})
    runner._run_startup_resume_event = AsyncMock()

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "busy", "chat_type": "dm"})
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m1"))

    await runner._send_restart_notification()

    assert entry.resume_pending is False
    runner._run_startup_resume_event.assert_not_awaited()

    entry.suspended = False
    runner._session_state(session_key).turn.agent = object()

    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "busy", "chat_type": "dm"})
    )
    await runner._send_restart_notification()

    assert entry.resume_pending is False
    runner._run_startup_resume_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_notification_send_failure_does_not_mark_requester(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    source = make_restart_source(chat_id="42")
    session_key, entry = _idle_requester_entry(source)

    runner, adapter = make_restart_runner()
    _wire_session_store(runner, {session_key: entry})
    runner._run_startup_resume_event = AsyncMock()

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "42", "chat_type": "dm"})
    )
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found"),
    )

    await runner._send_restart_notification()

    assert entry.resume_pending is False
    runner._run_startup_resume_event.assert_not_awaited()
