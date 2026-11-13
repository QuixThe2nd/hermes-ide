"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import gateway code without heavy deps
# ---------------------------------------------------------------------------
import sys, types

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

from gateway.platforms.base import (
    Platform,
    SessionSource,
    build_session_key,
)
from gateway.platforms.event import MessageEvent, MessageType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="telegram"):
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


def _make_adapter(platform_val="telegram"):
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
    async def test_telegram_grace_followups_merge_without_interrupt(self, monkeypatch):
        """Rapid Telegram text follow-ups inside the post-start grace window
        merge into one pending turn and never interrupt the running agent."""
        from gateway.run import GatewayRunner

        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")

        runner, _sentinel = _make_runner()
        adapter = _make_adapter()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            user_id="user1",
        )
        sk = build_session_key(source)
        runner.adapters[source.platform] = adapter

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "seconds_since_activity": 0.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time()

        events = [
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=f"m-{idx}",
            )
            for idx, text in enumerate(("first", "second", "third"), start=1)
        ]

        for event in events:
            result = await GatewayRunner._handle_message(runner, event)
            assert result is None

        assert adapter._pending_messages[sk].text == "first\nsecond\nthird"
        agent.interrupt.assert_not_called()

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
        shared_platform = Platform.TELEGRAM

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
            lambda: {"display": {"platforms": {"telegram": {"busy_ack_detail": True}}}},
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
            lambda: {"display": {"platforms": {"telegram": {"busy_ack_detail": True}}}},
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
        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        adapter = _make_adapter()
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="123",
            chat_type="dm", user_id="user1",
        )
        event = MessageEvent(
            text="nudge the running turn",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m1",
        )
        sk = build_session_key(source)
        runner.adapters[Platform.TELEGRAM] = adapter
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
