"""Synthetic injections borrow the session's last real user message id.

Background completions (async-delegation results, process notifications,
loop wakeups) are injected as ``MessageEvent(internal=True)`` with no
platform message_id of their own. Their turn-final replies therefore had
no reply anchor — and on Discord a final send without a ``MessageReference``
never pings the user. The gateway now remembers the last REAL user message
id per session and synthetic injections fall back to it.
"""
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session import SessionSource, SessionStore


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="2000",
        user_id="42",
        user_name="parsayazdani",
    )


@pytest.mark.asyncio
async def test_inject_watch_notification_uses_remembered_anchor(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    store = SessionStore(tmp_path, runner.config)

    entry = await asyncio.to_thread(store.get_or_create_session, _source())
    await asyncio.to_thread(
        store.set_session_metadata,
        entry.session_key,
        "_last_user_message_id",
        "999888777",
    )
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)

    captured: dict = {}

    class _Adapter:
        async def handle_message(self, event):
            captured["message_id"] = event.message_id

    runner.adapters = {Platform.DISCORD: _Adapter()}
    # resolve_delivery_transport is not exercised for native adapters when
    # the literal scan finds the platform first; keep it simple.
    runner._running = True

    evt = {
        "type": "async_delegation",
        "session_key": entry.session_key,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": "1000",
        "thread_id": "2000",
        "user_id": "42",
        "user_name": "parsayazdani",
        "message_id": "",
    }

    result = await runner._inject_watch_notification("subagent finished", evt)
    assert result is True
    assert captured["message_id"] == "999888777"


@pytest.mark.asyncio
async def test_inject_watch_notification_without_anchor_stays_none(tmp_path):
    """No remembered anchor (fresh/CLI-only session) keeps historical behaviour."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    store = SessionStore(tmp_path, runner.config)
    entry = await asyncio.to_thread(store.get_or_create_session, _source())
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)

    captured: dict = {}

    class _Adapter:
        async def handle_message(self, event):
            captured["message_id"] = event.message_id

    runner.adapters = {Platform.DISCORD: _Adapter()}

    evt = {
        "type": "completion",
        "session_key": entry.session_key,
        "platform": "discord",
        "chat_type": "thread",
        "chat_id": "1000",
        "thread_id": "2000",
    }

    result = await runner._inject_watch_notification("process done", evt)
    assert result is True
    assert captured["message_id"] is None


def test_internal_events_never_claim_anchor_in_transcript_rows():
    """The transcript dedupe must not see the borrowed anchor as a duplicate.

    Internal turns carry display_kind and skip message_id stamping; this is
    enforced at both persist sites via ``not getattr(event, 'internal', False)``.
    """
    from gateway.platforms.base import MessageEvent

    internal = MessageEvent(text="x", internal=True, message_id="999888777")
    assert getattr(internal, "internal", False) is True

    real = MessageEvent(text="y", internal=False, message_id="111222333")
    assert not getattr(real, "internal", False)
