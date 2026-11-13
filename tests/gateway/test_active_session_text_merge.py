"""Regression tests for active-session TEXT follow-up handling.

When the agent is actively running, rapid text follow-ups should survive as
one next-turn pending message instead of clobbering each other, and control /
clarify messages must bypass that merge lane entirely.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal telegram stub so importing gateway.platforms.base does not pull
# in the real python-telegram-bot dependency.
_tg = sys.modules.get("telegram") or types.ModuleType("telegram")
_tg.constants = sys.modules.get("telegram.constants") or types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.PRIVATE = "private"
_ct.GROUP = "group"
_ct.SUPERGROUP = "supergroup"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

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
        platform=Platform.TELEGRAM,
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
    return _DummyAdapter(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)


def _make_adapter() -> BasePlatformAdapter:
    """Build a BasePlatformAdapter without running its heavy __init__."""
    adapter = object.__new__(_DummyAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***")
    adapter.platform = Platform.TELEGRAM
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
async def test_non_dm_message_does_not_wait_for_topic_recovery_executor(monkeypatch):
    """Group messages must not queue behind the shared thread pool.

    Topic recovery only applies to Telegram DM topic mode. Offloading that
    no-op check for every group message makes ingress wait behind unrelated
    blocking jobs when the default executor is saturated.
    """
    adapter = _make_adapter()
    recovery = MagicMock(return_value=None)
    adapter.set_topic_recovery_fn(recovery)
    executor_called = False
    never_release = asyncio.Event()

    async def _blocked_to_thread(*args, **kwargs):
        nonlocal executor_called
        executor_called = True
        await never_release.wait()

    monkeypatch.setattr(asyncio, "to_thread", _blocked_to_thread)

    await asyncio.wait_for(
        adapter.handle_message(_make_event("/status", chat_type="group")),
        timeout=1.0,
    )
    await asyncio.sleep(0)

    assert executor_called is False
    recovery.assert_not_called()


@pytest.mark.asyncio
async def test_dm_topic_recovery_stays_offloaded(monkeypatch):
    """Real Telegram DM topic recovery must still run outside the event loop."""
    adapter = _make_adapter()
    recovery = MagicMock(return_value="topic-222")
    adapter.set_topic_recovery_fn(recovery)
    offloaded = False

    async def _inline_to_thread(func, *args, **kwargs):
        nonlocal offloaded
        offloaded = True
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    event = _make_event("hello", chat_type="dm", thread_id="1")
    original_source = event.source

    await adapter.handle_message(event)
    await asyncio.sleep(0)

    assert offloaded is True
    assert recovery.call_count == 1
    assert recovery.call_args.args[0] is original_source
    assert event.source.thread_id == "topic-222"


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
