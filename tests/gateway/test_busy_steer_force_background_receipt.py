"""Gateway receipts for steer-driven force-backgrounding: when the user's busy
follow-up releases a blocking delegation (the still-running children move to the
background), BOTH live busy-steer routes — the adapter busy handler and the
_handle_message priority fast-path — send the force-background receipt showing the
delegation ids and exactly what was injected into the agent's context, and nothing
is queued behind the run. Sessions without registered blocking waits keep today's
queue-behind-the-run demotion."""

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from tools import delegate_tool_forcebg as forcebg


class _StashAgent:
    """Top-level running agent driving subagents; steers stash like AIAgent.steer."""

    def __init__(self):
        self._active_children = [object()]
        self.steered = []

    def steer(self, text, *, on_accept=None):
        if not text or not text.strip():
            return False
        if on_accept is not None:
            on_accept("steer")
        self.steered.append(text.strip())
        return True

    def get_activity_summary(self):
        return {"api_call_count": 4, "max_iterations": 60, "current_tool": "delegate_agent"}


def _registered_wait(agent, delegation_id="deleg-fb-1"):
    """Register one blocking wait on *agent* whose adoption returns *delegation_id*."""
    run = SimpleNamespace(
        parent_agent=agent, _adopt_running_worker=lambda future: delegation_id,
    )
    return forcebg.register_blocking_wait(run, Future(), threading.Event())


def _make_runner(agent):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._queued_events = {}
    runner._draining = False
    runner._running = True
    runner._background_tasks = set()
    runner.adapters = {}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=False,
    )
    runner.session_store = None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True

    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = SimpleNamespace(extra={})
    adapter.platform = Platform.DISCORD
    runner.adapters[Platform.DISCORD] = adapter

    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1",
        chat_type="dm", user_id="user-1",
    )
    session_key = build_session_key(source)
    runner._running_agents[session_key] = agent
    runner._gateway_loop = asyncio.get_running_loop()
    return runner, adapter, source, session_key


def _event(source, message_id, text):
    return MessageEvent(
        text=text, message_type=MessageType.TEXT,
        source=source, message_id=message_id,
        raw_message={"discord_hub_message_id": message_id},
    )


@pytest.fixture(autouse=True)
def _clean():
    forcebg._reset_for_tests()
    yield
    forcebg._reset_for_tests()


@pytest.fixture(autouse=True)
def _receipt_env(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})


class TestAdapterBusyRoute:
    @pytest.mark.asyncio
    async def test_force_background_receipt_with_injected_text(self):
        agent = _StashAgent()
        wait = _registered_wait(agent, "deleg-fb-1")
        runner, adapter, source, sk = _make_runner(agent)
        event = _event(source, "m1", "actually, make it red")

        assert await runner._handle_active_session_busy_message(event, sk) is True

        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "moved the running agent to the background" in content
        assert "deleg-fb-1" in content
        assert "actually, make it red" in content
        assert "```" in content  # the injected text rides in a fenced block
        # The steer landed on the agent with the origin preamble.
        assert agent.steered and "actually, make it red" in agent.steered[0]
        # Redirected: the follow-up is NOT queued behind the run it entered.
        assert sk not in adapter._pending_messages
        forcebg.unregister_blocking_wait(wait.run, wait)

    @pytest.mark.asyncio
    async def test_no_waits_keeps_queue_demotion(self):
        """Subagents live but none blocking on a registered wait (background
        children only): today's queue-behind-the-run behavior is untouched."""
        agent = _StashAgent()
        runner, adapter, source, sk = _make_runner(agent)
        event = _event(source, "m2", "status?")

        assert await runner._handle_active_session_busy_message(event, sk) is True

        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "queued" in content.lower()
        assert "moved the running agent" not in content
        # Queued for the next turn, not steered.
        assert agent.steered == []
        assert sk in adapter._pending_messages or runner._queued_events.get(sk)

    @pytest.mark.asyncio
    async def test_rail_refusal_falls_back_to_plain_steer_receipt(self):
        """Adoption refused (capacity race): the message still landed inside the run
        as a plain steer — steered receipt, nothing queued."""
        agent = _StashAgent()
        wait = _registered_wait(agent, None)  # adoption returns None (refused)
        runner, adapter, source, sk = _make_runner(agent)
        event = _event(source, "m3", "when you get a chance")

        assert await runner._handle_active_session_busy_message(event, sk) is True

        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Steered into current run" in content
        assert "moved the running agent" not in content
        assert agent.steered and "when you get a chance" in agent.steered[0]
        assert sk not in adapter._pending_messages
        forcebg.unregister_blocking_wait(wait.run, wait)


class TestPriorityRoute:
    @pytest.mark.asyncio
    async def test_priority_busy_route_sends_the_same_force_background_receipt(self):
        agent = _StashAgent()
        wait = _registered_wait(agent, "deleg-fb-pri")
        runner, adapter, source, sk = _make_runner(agent)
        event = _event(source, "m-pri", "priority nudge")

        assert await runner._handle_message(event) is None

        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "moved the running agent to the background" in content
        assert "deleg-fb-pri" in content
        assert "priority nudge" in content
        assert sk not in adapter._pending_messages
        forcebg.unregister_blocking_wait(wait.run, wait)

    @pytest.mark.asyncio
    async def test_priority_route_without_waits_queues(self):
        agent = _StashAgent()
        runner, adapter, source, sk = _make_runner(agent)
        event = _event(source, "m-pri-2", "hold my place")

        assert await runner._handle_message(event) is None

        assert agent.steered == []
        assert sk in adapter._pending_messages or runner._queued_events.get(sk)


class TestOutcomeShape:
    def test_outcome_defaults_unchanged(self):
        """The extended outcome keeps today's defaults for every existing branch."""
        from gateway.run import GatewayRunner

        outcome = GatewayRunner._BusySteerOutcome(
            effective_mode="queue", demoted_for_subagents=True,
            demoted_for_compression=False, redirected=False,
        )
        assert outcome.force_backgrounded is False
        assert outcome.force_background_ids == ()
        assert outcome.steered_text == ""
