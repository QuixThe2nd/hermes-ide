"""PRIORITY busy fast-path steers get the same receipts as the classic busy handler.

Regression for the 2026-11-06 incident: a Discord message steered into a
running agent via ``_hm_handle_running_session_message`` (priority fast-path)
landed silently — no ⏩ reaction on the source message and no
"✅ Steer delivered" follow-up — while steers admitted through
``_handle_active_session_busy_message`` got both. The two admission routes
must be receipt-equivalent.
"""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _FakeAdapter:
    """Discord-shaped delivery adapter with a recording reaction lane."""

    def __init__(self):
        self.reactions = []

    def _reactions_enabled(self):
        return True

    async def _add_reaction(self, raw, emoji):
        self.reactions.append((raw, emoji))


class _SteerableAgent:
    def __init__(self, accept=True):
        self._accept = accept
        self.steered_texts = []
        self.listeners = []

    def steer(self, text):
        self.steered_texts.append(text)
        return self._accept

    def register_steer_delivery_listener(self, listener):
        self.listeners.append(listener)


def _make_event(platform=Platform.DISCORD):
    source = SessionSource(platform=platform, chat_id="c1", user_id="u1", chat_type="dm")
    raw = object()
    event = MessageEvent(text="adjust course", source=source, message_id="m1")
    event.raw_message = raw
    return event, source, raw


async def _drive(runner, event, source):
    """Push the event through the priority busy fast-path exactly as production does."""
    runner._busy_input_mode = "steer"
    return await runner._hm_handle_running_session_message(event, source, "key")


@pytest.mark.asyncio
async def test_priority_busy_steer_gets_discord_reaction_and_delivered_ack(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", raising=False)

    runner = GatewayRunner(config=GatewayConfig())
    adapter = _FakeAdapter()
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_for_source = lambda source: adapter

    event, source, raw = _make_event()
    agent = _SteerableAgent()
    runner._session_state("key").turn.agent = agent

    result = await _drive(runner, event, source)

    assert result is None
    assert agent.steered_texts and agent.steered_texts[0].endswith("adjust course")
    assert adapter.reactions == [(raw, "⏩")]
    assert len(agent.listeners) == 1


@pytest.mark.asyncio
async def test_priority_busy_steer_non_discord_gets_no_reaction(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", raising=False)

    runner = GatewayRunner(config=GatewayConfig())
    adapter = _FakeAdapter()
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_for_source = lambda source: adapter

    event, source, _ = _make_event(platform=Platform.TELEGRAM)
    agent = _SteerableAgent()
    runner._session_state("key").turn.agent = agent

    await _drive(runner, event, source)

    assert agent.steered_texts  # the steer itself still lands
    assert adapter.reactions == []


@pytest.mark.asyncio
async def test_priority_busy_steer_rejected_falls_back_to_queue_without_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    runner = GatewayRunner(config=GatewayConfig())
    adapter = _FakeAdapter()
    runner._delivery_adapter_for = lambda source: adapter
    runner._adapter_for_source = lambda source: adapter
    queued = []
    runner._queue_or_replace_pending_event = lambda key, ev: queued.append((key, ev))

    event, source, _ = _make_event()
    agent = _SteerableAgent(accept=False)
    runner._session_state("key").turn.agent = agent

    await _drive(runner, event, source)

    assert queued == [("key", event)]
    assert adapter.reactions == []
    assert agent.listeners == []


@pytest.mark.asyncio
async def test_priority_busy_steer_survives_missing_adapter(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", raising=False)

    runner = GatewayRunner(config=GatewayConfig())
    runner._delivery_adapter_for = lambda source: None
    runner._adapter_for_source = lambda source: None

    event, source, _ = _make_event()
    agent = _SteerableAgent()
    runner._session_state("key").turn.agent = agent

    result = await _drive(runner, event, source)  # must not raise

    assert result is None
    assert agent.steered_texts  # silent steer still delivered
