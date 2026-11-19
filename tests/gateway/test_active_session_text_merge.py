"""Regression tests for active-session TEXT follow-up handling.

When the agent is actively running, rapid text follow-ups should survive as
one next-turn pending message instead of clobbering each other, and control /
clarify messages must bypass that merge lane entirely.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


def _make_event(
    text: str,
    chat_id: str = "12345",
    *,
    chat_type: str = "dm",
    user_id: str = "u1",
    user_name: str | None = None,
    thread_id: str | None = None,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_name,
        thread_id=thread_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=f"msg-{text[:8]}",
    )


class _DummyAdapter(BasePlatformAdapter):  # type: ignore[misc]
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, *args, **kwargs):
        return SendResult(success=True, message_id="x")


def _make_initialized_adapter() -> BasePlatformAdapter:
    return _DummyAdapter(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)


def _make_adapter() -> BasePlatformAdapter:
    """Build a BasePlatformAdapter without running its heavy __init__."""
    adapter = object.__new__(_DummyAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***")
    adapter.platform = Platform.DISCORD
    adapter._message_handler = AsyncMock(return_value=None)
    adapter._busy_session_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._session_tasks = {}
    adapter._background_tasks = set()
    adapter._post_delivery_callbacks = {}
    adapter._expected_cancelled_tasks = set()
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._running = True
    adapter._typing_paused = set()
    return adapter



@pytest.mark.asyncio
async def test_rapid_text_followups_accumulate_instead_of_replacing():
    """Rapid TEXT follow-ups must all survive in the pending event."""
    adapter = _make_adapter()
    first = _make_event("part one")
    session_key = build_session_key(first.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(_make_event("part two"))
    await adapter.handle_message(_make_event("part three"))

    pending = adapter._pending_messages[session_key]
    assert pending.text == "part two\npart three"
    assert not adapter._active_sessions[session_key].is_set()


@pytest.mark.asyncio
async def test_control_and_clarify_messages_bypass_pending_merge():
    adapter = _make_adapter()
    started: list[str] = []

    def _fake_start(event, session_key, *, interrupt_event=None):
        started.append(event.text)
        return True

    adapter._start_session_processing = _fake_start  # type: ignore[method-assign]

    await adapter.handle_message(_make_event("/status"))
    assert started == ["/status"]

    answer = _make_event("clarify answer")
    session_key = build_session_key(answer.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._message_handler = AsyncMock(return_value=None)

    with patch("tools.clarify_gateway.get_pending_for_session", return_value=object()):
        await adapter.handle_message(answer)

    adapter._message_handler.assert_awaited_once_with(answer)
    assert session_key not in adapter._pending_messages
