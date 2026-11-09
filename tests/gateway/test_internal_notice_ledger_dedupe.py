"""Stable durable identity for internal async-delegation completion notices."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import AsyncSessionStore, SessionStore
from hermes_state import SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


LEDGER_MESSAGE_ID = "watch:async_delegation:deleg_123:"


@pytest.mark.asyncio
async def test_delegation_notice_gets_stable_ledger_message_id(tmp_path):
    runner, adapter = make_restart_runner()
    store = SessionStore(tmp_path, runner.config)
    source = make_restart_source(chat_id="internal-ledger-injection")
    entry = await asyncio.to_thread(store.get_or_create_session, source)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    adapter.handle_message = AsyncMock()

    async def _admit(event, *args, **kwargs):
        event._gateway_accepted = True
        return None

    adapter.handle_message.side_effect = _admit
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_123",
        "session_key": entry.session_key,
        "platform": source.platform.value,
        "chat_type": source.chat_type,
        "chat_id": source.chat_id,
        "user_id": source.user_id,
    }

    assert await runner._inject_watch_notification("delegation complete", evt) is True

    injected = adapter.handle_message.await_args.args[0]
    assert injected.internal is True
    assert injected.ledger_message_id == LEDGER_MESSAGE_ID
