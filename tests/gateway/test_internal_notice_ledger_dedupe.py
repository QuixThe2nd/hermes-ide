"""Durable replay dedupe for internal async-delegation completion notices."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_drain_queue import _replay_event_already_processed
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


def test_internal_notice_settles_once_after_durable_restart(tmp_path):
    """The write and replay sides use the same durable internal-event identity."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    session_id = db.create_session("sess-internal-notice", "gateway")
    event = MessageEvent(
        text="delegation complete",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="internal-ledger-replay"),
        internal=True,
        ledger_message_id=LEDGER_MESSAGE_ID,
    )
    session_key = "agent:main:discord:dm:internal-ledger-replay"

    fresh_store = MagicMock()
    fresh_store.peek_session_id.return_value = session_id
    fresh_store.has_platform_message_id.side_effect = db.has_platform_message_id
    fresh_runner = SimpleNamespace(session_store=fresh_store)
    assert not _replay_event_already_processed(fresh_runner, session_key, event)

    # Exercise the gateway fallback-row write path with the event's durable id.
    store = SessionStore.__new__(SessionStore)
    store._db = db
    store._append_transcript_message(
        session_id,
        {
            "role": "user",
            "content": event.text,
            "ledger_message_id": event.ledger_message_id,
        },
    )

    restarted_db = SessionDB(db_path)
    restarted_store = MagicMock()
    restarted_store.peek_session_id.return_value = session_id
    restarted_store.has_platform_message_id.side_effect = restarted_db.has_platform_message_id
    restarted_runner = SimpleNamespace(session_store=restarted_store)
    assert _replay_event_already_processed(restarted_runner, session_key, event)
