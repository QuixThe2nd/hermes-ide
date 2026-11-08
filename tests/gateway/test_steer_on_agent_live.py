"""Steer-on-live: a steer-mode follow-up arriving while the turn's agent is still
being built (``_AGENT_PENDING_SENTINEL``) is parked and steered into the run the
moment ``_run_agent_track_agent`` promotes the agent — not parked as a second
user turn in the pending FIFO.

Behavior contracts:
- steer-eligible (plain text / all-voice) + steer mode + agent-pending → parked,
  never queued; the ack says it will be injected once the run accepts.
- promotion consumes the slot: ``agent.steer()`` receives the text.
- steer failure at promotion recovers the event into the pending FIFO.
- non-steerable payloads (photo media) keep the fallback-queue behavior and ack.
- teardown (/stop / turn release) and the 60s hung-build timeout flush the
  parked event back to the FIFO — never dropped.
"""
import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import Platform, SessionSource, build_session_key
from gateway.platforms.event import MessageEvent, MessageType


def _make_event(text="hello", chat_id="123"):
    source = SessionSource(
        platform=Platform.TELEGRAM,
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
    adapter.platform = Platform.TELEGRAM
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
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL
    return runner, adapter, event, sk


class TestParkOnAgentPending:
    @pytest.mark.asyncio
    async def test_plain_text_parks_not_queues_and_ack_explains(self, monkeypatch):
        runner, adapter, event, sk = _setup(monkeypatch)

        assert await runner._handle_active_session_busy_message(event, sk) is True

        # Parked in the steer-on-live slot, NOT in the pending FIFO.
        assert runner._parked_steer_map()[sk] is event
        assert adapter._pending_messages.get(sk) is None

        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "pending start" in content
        assert "injected as soon as the run accepts" in content
        assert "Queued" not in content
        assert "Couldn't steer" not in content

    @pytest.mark.asyncio
    async def test_media_does_not_park_queues_with_fallback_ack(self, monkeypatch):
        runner, adapter, event, sk = _setup(monkeypatch)
        event.message_type = MessageType.PHOTO
        event.text = ""
        event.media_urls = ["/tmp/photo.jpg"]
        event.media_types = ["image/jpeg"]

        await runner._handle_active_session_busy_message(event, sk)

        assert sk not in runner._parked_steer_map()
        assert adapter._pending_messages.get(sk) is event
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Couldn't steer into the current run" in content
        assert "queued for the next turn" in content

    @pytest.mark.asyncio
    async def test_command_does_not_park(self, monkeypatch):
        runner, adapter, event, sk = _setup(monkeypatch)
        event.text = "/status"

        await runner._handle_active_session_busy_message(event, sk)

        assert sk not in runner._parked_steer_map()


class TestConsumeOnPromotion:
    def _agent(self, steer_result=True):
        agent = MagicMock()
        agent.steer = MagicMock(return_value=steer_result)
        return agent

    @pytest.mark.asyncio
    async def test_track_agent_promotion_steers_parked_event(self, monkeypatch):
        from gateway.turn_context import TurnContext

        runner, adapter, event, sk = _setup(monkeypatch)
        agent = self._agent()
        ctx = TurnContext(session_key=sk, agent_holder=[agent], run_generation=None)
        runner._session_state(sk).turn.agent = None

        # Park via the busy path, then promote through the real tracker.
        await runner._handle_active_session_busy_message(event, sk)
        await runner._run_agent_track_agent(ctx)

        agent.steer.assert_called_once()
        injected = agent.steer.call_args.args[0]
        assert injected.endswith("focus on the parser tests")
        assert runner._parked_steer_map().get(sk) is None
        assert adapter._pending_messages.get(sk) is None
        assert runner._session_state(sk).turn.agent is agent

    @pytest.mark.asyncio
    async def test_steer_failure_at_promotion_recovers_to_fifo(self, monkeypatch):
        runner, adapter, event, sk = _setup(monkeypatch)
        agent = self._agent(steer_result=False)

        await runner._handle_active_session_busy_message(event, sk)
        await runner._consume_parked_steer_on_agent_live(sk, agent)

        agent.steer.assert_called_once()
        assert runner._parked_steer_map().get(sk) is None
        assert adapter._pending_messages.get(sk) is event  # never dropped

    @pytest.mark.asyncio
    async def test_consume_without_slot_is_noop(self, monkeypatch):
        runner, _adapter, _event, sk = _setup(monkeypatch)
        agent = self._agent()
        await runner._consume_parked_steer_on_agent_live(sk, agent)
        agent.steer.assert_not_called()


class TestRecovery:
    @pytest.mark.asyncio
    async def test_stop_flushes_parked_event_to_fifo(self, monkeypatch):
        """Turn teardown (the /stop / finalize lane releases the running-agent
        slot) must recover a still-parked event into the pending FIFO."""
        runner, adapter, event, sk = _setup(monkeypatch)

        await runner._handle_active_session_busy_message(event, sk)
        assert runner._parked_steer_map().get(sk) is event

        assert runner._release_running_agent_state(sk) is True
        assert runner._parked_steer_map().get(sk) is None
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_flush_is_idempotent(self, monkeypatch):
        runner, _adapter, _event, sk = _setup(monkeypatch)
        runner._flush_parked_steer(sk)  # nothing parked — no error
        runner._flush_parked_steer(sk)

    @pytest.mark.asyncio
    async def test_hung_build_timeout_flushes_to_fifo(self, monkeypatch):
        runner, adapter, event, sk = _setup(monkeypatch)
        runner._STEER_ON_LIVE_TIMEOUT_SECONDS = 0.05

        await runner._handle_active_session_busy_message(event, sk)
        assert runner._parked_steer_map().get(sk) is event

        await asyncio.sleep(0.2)
        assert runner._parked_steer_map().get(sk) is None
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_drain_flushes_still_parked_event(self, monkeypatch):
        """A run that finishes without the agent ever promoting (fast failure)
        hands its parked event to the drain, which recovers it pre-dequeue."""
        from gateway.run import GatewayRunner

        runner, adapter, event, sk = _setup(monkeypatch)
        result = {"final_response": "done"}

        async def _noop_transcribe(*a, **k):
            return "", []

        monkeypatch.setattr(GatewayRunner, "_transcribe_and_echo_pending_voice", _noop_transcribe)
        # _dequeue_pending_event consumes through the adapter's pop method.
        adapter.get_pending_message = lambda key=None: adapter._pending_messages.pop(key, None)
        await runner._handle_active_session_busy_message(event, sk)
        pending_event, _pending = await runner._run_agent_drain_pending(
            result, adapter, event.source, sk
        )
        assert pending_event is event
