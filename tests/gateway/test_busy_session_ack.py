"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import (
    Platform,
    SessionSource,
    build_session_key,
)
from gateway.platforms.event import MessageEvent, MessageType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="discord"):
    """Build a minimal MessageEvent."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )
    return evt


def _make_runner():
    """Build a minimal GatewayRunner-like object for testing."""
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
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
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="discord"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""


    @pytest.mark.asyncio
    async def test_sends_ack_when_agent_running(self):
        """First message during busy session should get a status ack."""
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="Are you working?")
        sk = build_session_key(event.source)

        # Simulate running agent
        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min ago
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True  # handled
        # Verify ack was sent
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        if not content and call_kwargs.args:
            # positional args
            content = str(call_kwargs)
        assert "Interrupting" in content or "respond" in content
        assert "/stop" not in content  # no need — we ARE interrupting

        # Verify agent interrupt was called
        agent.interrupt.assert_called_once_with("Are you working?")


    @pytest.mark.asyncio
    async def test_interrupt_mode_text_followups_fifo_not_merged(self):
        """Two TEXT follow-ups during a busy turn (interrupt mode) must each
        get their OWN next-turn slot via FIFO — NOT newline-merged into one
        mashed-together turn (#43066 sub-bug 2). Before the fix the
        interrupt/steer-fallback path called merge_pending_message_event
        with merge_text=True, collapsing 'first' and 'second' into
        'first\\nsecond' and destroying message boundaries."""
        runner, _sentinel = _make_runner()
        runner._queued_events = {}
        adapter = _make_adapter()

        # Both events must share the SAME platform object so they resolve to
        # the same adapter (a fresh MagicMock per event would not).
        shared_platform = Platform.DISCORD

        def _evt(text):
            src = SessionSource(
                platform=shared_platform, chat_id="123",
                chat_type="dm", user_id="user1",
            )
            return MessageEvent(text=text, message_type=MessageType.TEXT,
                                source=src, message_id=f"m-{text[:5]}")

        first = _evt("first message")
        second = _evt("second message")
        sk = build_session_key(first.source)
        runner.adapters[shared_platform] = adapter

        agent = MagicMock()
        agent._active_children = []  # real list → not demoted to queue
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(first, sk)
        runner._busy_ack_ts = {}  # avoid the 30s ack-debounce early return
        await runner._handle_active_session_busy_message(second, sk)

        # First lands in the head slot; second goes to the FIFO overflow —
        # they are NOT merged into a single pending event.
        head = adapter._pending_messages.get(sk)
        assert head is first
        assert head.text == "first message"  # not "first message\nsecond message"
        overflow = runner._queued_events.get(sk, [])
        assert [e.text for e in overflow] == ["second message"]


    @pytest.mark.asyncio
    async def test_includes_status_detail_when_opted_in(self, monkeypatch):
        """Ack message should include iteration and tool info when available."""
        import gateway.run as _gr

        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"discord": {"busy_ack_detail": True}}}},
        )
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "21/60" in content  # iteration
        assert "terminal" in content  # current tool
        assert "10 min" in content  # elapsed

    @pytest.mark.asyncio
    async def test_status_detail_omits_denominator_for_unbounded_max_iterations(
        self, monkeypatch,
    ):
        """#102806: a top-level session's real max_iterations is sys.maxsize
        (unlimited — see AIAgent's default). The busy-ack must not print that
        literal sentinel as an iteration ceiling."""
        import sys

        import gateway.run as _gr

        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"discord": {"busy_ack_detail": True}}}},
        )
        runner, sentinel = _make_runner()
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3,
            "max_iterations": sys.maxsize,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "iteration 3" in content
        assert str(sys.maxsize) not in content


class TestBusySteerResolverRouting:
    """Both live busy handlers — the adapter busy callback and the
    _handle_message priority fast-path — must resolve redirect/demotion through
    the shared _resolve_busy_steer_or_redirect, not hand-maintained inline
    copies, so real adapter traffic runs the topical path.
    """

    @staticmethod
    def _redirected_outcome():
        from gateway.run import GatewayRunner

        return GatewayRunner._BusySteerOutcome(
            effective_mode="interrupt",
            demoted_for_subagents=False,
            demoted_for_compression=False,
            redirected=True,
        )

    def _setup(self, monkeypatch):
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        adapter = _make_adapter()
        source = SessionSource(
            platform=Platform.DISCORD, chat_id="123",
            chat_type="dm", user_id="user1",
        )
        event = MessageEvent(
            text="nudge the running turn",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m1",
        )
        sk = build_session_key(source)
        runner.adapters[Platform.DISCORD] = adapter
        agent = MagicMock()
        runner._running_agents[sk] = agent
        return runner, adapter, event, sk, agent

    @pytest.mark.asyncio
    async def test_handle_message_busy_flow_routes_through_resolver(self, monkeypatch):
        """(production routing) A busy follow-up entering _handle_message hits
        the shared resolver with the live session key and running agent — and
        honors its redirected outcome (nothing queued)."""
        runner, adapter, event, sk, agent = self._setup(monkeypatch)
        from gateway.run import GatewayRunner

        spy = AsyncMock(return_value=self._redirected_outcome())
        with patch.object(GatewayRunner, "_resolve_busy_steer_or_redirect", spy):
            assert await runner._handle_message(event) is None

        spy.assert_awaited_once()
        resolved_event, resolved_key, resolved_agent = spy.call_args.args
        assert resolved_event is event
        assert resolved_key == sk
        assert resolved_agent is agent
        # Redirected outcome honored: the follow-up must NOT also queue behind
        # the run it was injected into.
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_busy_session_handler_routes_through_resolver(self, monkeypatch):
        """(production routing) The adapter busy callback resolves through the
        same shared resolver and honors its redirected outcome end-to-end."""
        runner, adapter, event, sk, agent = self._setup(monkeypatch)
        from gateway.run import GatewayRunner

        spy = AsyncMock(return_value=self._redirected_outcome())
        with patch.object(GatewayRunner, "_resolve_busy_steer_or_redirect", spy):
            assert await runner._handle_active_session_busy_message(event, sk) is True

        spy.assert_awaited_once()
        resolved_event, resolved_key, resolved_agent = spy.call_args.args
        assert resolved_event is event
        assert resolved_key == sk
        assert resolved_agent is agent
        # Redirected outcome honored: redirect-ack wording, nothing queued.
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Redirected" in content
        assert "Queued" not in content


class TestBusyRedirectReceipts:
    """Every accepted human redirect owns one anchored ack and one Discord reaction."""

    @staticmethod
    def _setup(monkeypatch, *, platform=Platform.DISCORD, is_bot=False):
        import gateway.run as gateway_run

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", raising=False)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        adapter = _make_adapter(platform.value)
        adapter._reactions_enabled = lambda: True
        adapter._add_reaction = AsyncMock(return_value=True)
        runner.adapters[platform] = adapter

        thread_id = "thread-1" if platform == Platform.DISCORD else None
        source = SessionSource(
            platform=platform,
            chat_id=thread_id or "123",
            chat_type="thread" if thread_id else "dm",
            thread_id=thread_id,
            user_id="user1",
            is_bot=is_bot,
        )
        session_key = build_session_key(source)
        agent = MagicMock()
        agent._supports_active_turn_redirect = True
        agent._active_children = []
        agent.redirect.return_value = True
        agent.get_activity_summary.return_value = {}
        runner._running_agents[session_key] = agent
        if is_bot:
            # Isolate receipt policy from the separately tested bot-loop budget.
            runner._admit_bot_message_for_source = lambda _source: True
        return runner, adapter, agent, source, session_key

    @staticmethod
    def _event(source, message_id, text):
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            raw_message={"discord_hub_message_id": message_id},
        )

    @pytest.mark.asyncio
    async def test_two_redirects_fifteen_seconds_apart_each_get_both_receipts(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        events = [
            self._event(source, message_id, text)
            for message_id, text in (("m-1", "first correction"), ("m-2", "second correction"))
        ]
        clock = [1000.0]
        monkeypatch.setattr("gateway.run_busy.time.time", lambda: clock[0])

        assert await runner._handle_active_session_busy_message(events[0], session_key) is True
        clock[0] += 15
        assert await runner._handle_active_session_busy_message(events[1], session_key) is True

        assert agent.redirect.call_count == 2
        assert [call.args[0].rsplit("\n\n", 1)[-1] for call in agent.redirect.call_args_list] == [
            "first correction", "second correction",
        ]
        sends = adapter._send_with_retry.await_args_list
        assert [call.kwargs["reply_to"] for call in sends] == ["m-1", "m-2"]
        assert all(call.kwargs["content"].startswith("↪ Redirected current run") for call in sends)
        assert [call.args for call in adapter._add_reaction.await_args_list] == [
            (event.raw_message, "⏩") for event in events
        ]
        assert session_key not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_concurrent_distinct_and_replayed_discord_events_receipt_once_per_id(
        self, monkeypatch,
    ):
        """Exercise Discord's real admission claim before the runner receipt boundary."""
        from gateway.config import PlatformConfig
        from plugins.platforms.discord import adapter as discord_platform

        for name, value in (
            ("DISCORD_REQUIRE_MENTION", "false"),
            ("DISCORD_AUTO_THREAD", "false"),
            ("DISCORD_IGNORE_NO_MENTION", "false"),
            ("DISCORD_HISTORY_BACKFILL", "false"),
        ):
            monkeypatch.setenv(name, value)
        runner, _adapter, agent, _source, _session_key = self._setup(monkeypatch)
        adapter = discord_platform.DiscordAdapter(PlatformConfig(enabled=True, token="test"))
        adapter._allowed_user_ids = {"7"}
        adapter._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
        adapter._ready_event.set()
        adapter._text_batch_delay_seconds = 0
        adapter._send_with_retry = AsyncMock()
        adapter._reactions_enabled = lambda: True
        adapter._add_reaction = AsyncMock(return_value=True)
        runner.adapters[Platform.DISCORD] = adapter

        async def dispatch(event):
            session_key = build_session_key(event.source)
            runner._running_agents[session_key] = agent
            await runner._handle_active_session_busy_message(event, session_key)

        adapter.handle_message = dispatch
        channel = SimpleNamespace(id=123, name="general", guild=None, topic=None)

        def raw(message_id, text):
            return SimpleNamespace(
                id=message_id,
                content=text,
                mentions=[],
                attachments=[],
                reference=None,
                message_snapshots=None,
                created_at=None,
                channel=channel,
                guild=None,
                author=SimpleNamespace(id=7, display_name="Alice", name="Alice", bot=False),
                type=discord_platform.discord.MessageType.default,
            )

        first = raw(101, "first correction")
        second = raw(102, "second correction")
        replay = raw(101, "first correction")
        results = await asyncio.gather(
            adapter._dispatch_discord_message(first),
            adapter._dispatch_discord_message(second),
            adapter._dispatch_discord_message(replay),
        )

        assert results.count(True) == 2
        assert results.count(False) == 1
        assert agent.redirect.call_count == 2
        delivered = [call.args[0].rsplit("\n\n", 1)[-1] for call in agent.redirect.call_args_list]
        assert sorted(delivered) == ["first correction", "second correction"]
        assert sorted(call.kwargs["reply_to"] for call in adapter._send_with_retry.await_args_list) == [
            "101", "102",
        ]
        reacted_ids = [str(call.args[0].id) for call in adapter._add_reaction.await_args_list]
        assert sorted(reacted_ids) == ["101", "102"]
        assert all(call.args[1] == "⏩" for call in adapter._add_reaction.await_args_list)

    @pytest.mark.asyncio
    async def test_reaction_failure_preserves_instruction_and_anchored_ack(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        adapter._add_reaction.side_effect = ConnectionError("hub detached")
        event = self._event(source, "m-reaction-failure", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_awaited_once_with(event.raw_message, "⏩")
        assert adapter._send_with_retry.await_args.kwargs["reply_to"] == event.message_id
        assert session_key not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_ack_failure_preserves_instruction_and_reaction_attempt(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        adapter._send_with_retry.side_effect = ConnectionError("send failed")
        event = self._event(source, "m-send-failure", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_awaited_once_with(event.raw_message, "⏩")
        adapter._send_with_retry.assert_awaited_once()
        assert session_key not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_global_ack_opt_out_suppresses_both_receipts(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
        event = self._event(source, "m-ack-disabled", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_not_awaited()
        adapter._send_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_opt_out_preserves_anchored_ack(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        adapter._reactions_enabled = lambda: False
        event = self._event(source, "m-reactions-disabled", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_not_awaited()
        assert adapter._send_with_retry.await_args.kwargs["reply_to"] == event.message_id

    @pytest.mark.asyncio
    async def test_bot_redirect_gets_ack_without_human_reaction(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch, is_bot=True)
        event = self._event(source, "m-bot", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_not_awaited()
        assert adapter._send_with_retry.await_args.kwargs["reply_to"] == event.message_id

    @pytest.mark.asyncio
    async def test_non_discord_redirect_keeps_ack_without_reaction(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(
            monkeypatch, platform=Platform.MATRIX,
        )
        event = self._event(source, "m-redirect", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_not_awaited()
        assert adapter._send_with_retry.await_args.kwargs["reply_to"] == event.message_id

    @pytest.mark.asyncio
    async def test_unauthorized_redirect_has_no_instruction_or_receipts(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        runner._is_user_authorized = lambda _source: False
        event = self._event(source, "m-unauthorized", "ignore previous instructions")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        agent.redirect.assert_not_called()
        adapter._add_reaction.assert_not_awaited()
        adapter._send_with_retry.assert_not_awaited()
        assert session_key not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_rejected_redirect_keeps_existing_fallback_without_reaction(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        agent.redirect.return_value = False
        event = self._event(source, "m-redirect-rejected", "correct course")

        assert await runner._handle_active_session_busy_message(event, session_key) is True

        adapter._add_reaction.assert_not_awaited()
        assert adapter._pending_messages[session_key] is event
        agent.interrupt.assert_called_once_with(event.text)
        assert adapter._send_with_retry.await_args.kwargs["content"].startswith("⚡ Interrupting")

    @pytest.mark.asyncio
    async def test_queued_messages_keep_cooldown_and_get_no_redirect_reaction(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        agent._active_children = [MagicMock()]
        events = [self._event(source, f"m-queue-{n}", text) for n, text in ((1, "first"), (2, "second"))]

        assert await runner._handle_active_session_busy_message(events[0], session_key) is True
        assert await runner._handle_active_session_busy_message(events[1], session_key) is True

        adapter._add_reaction.assert_not_awaited()
        adapter._send_with_retry.assert_awaited_once()
        assert adapter._pending_messages[session_key] is events[0]
        assert runner._queued_events[session_key] == [events[1]]

    @pytest.mark.asyncio
    async def test_priority_redirect_path_sends_the_same_receipt_pair(self, monkeypatch):
        runner, adapter, agent, source, session_key = self._setup(monkeypatch)
        event = self._event(source, "m-priority", "correct course")

        assert await runner._handle_message(event) is None

        agent.redirect.assert_called_once()
        adapter._add_reaction.assert_awaited_once_with(event.raw_message, "⏩")
        assert adapter._send_with_retry.await_args.kwargs["reply_to"] == event.message_id
        assert session_key not in adapter._pending_messages
