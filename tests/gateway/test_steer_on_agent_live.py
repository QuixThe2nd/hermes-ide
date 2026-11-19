"""Steer-mode follow-up recovery during turn drain.

A steer-mode follow-up that cannot be steered into the live run (agent still
being built) is kept in the pending FIFO; a run that finishes without the
agent ever promoting hands its event to the drain, which recovers it
pre-dequeue — never dropped.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


from gateway.platforms.base import Platform, SessionSource, build_session_key
from gateway.platforms.event import MessageEvent, MessageType


def _make_event(text="hello", chat_id="123"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="dm",
        user_id="user1",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_text_mode = "interrupt"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


def _make_adapter():
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = Platform.DISCORD
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    return adapter


def _setup(monkeypatch):
    import gateway.run as _gr

    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", raising=False)
    monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _make_event(text="focus on the parser tests")
    sk = build_session_key(event.source)
    runner.adapters[Platform.DISCORD] = adapter
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
    return runner, adapter, event, sk

class TestDrainRecovery:
    @pytest.mark.asyncio
    async def test_drain_flushes_still_parked_event(self, monkeypatch):
        """A run that finishes without the agent ever promoting (fast failure)
        hands its parked event to the drain, which recovers it pre-dequeue."""
        from gateway.run import GatewayRunner

        runner, adapter, event, sk = _setup(monkeypatch)
        result = {"final_response": "done"}

        # _dequeue_pending_event consumes through the adapter's pop method.
        adapter.get_pending_message = lambda key=None: adapter._pending_messages.pop(key, None)
        await runner._handle_active_session_busy_message(event, sk)
        pending_event, _pending = await runner._run_agent_drain_pending(
            result, adapter, event.source, sk
        )
        assert pending_event is event
