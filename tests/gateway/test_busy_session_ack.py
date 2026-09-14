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
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="telegram"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""


    @pytest.mark.asyncio
    async def test_telegram_grace_followups_respect_queue_fifo(self, monkeypatch):
        """Rapid Telegram text follow-ups in queue mode must not merge."""
        from gateway.run import GatewayRunner

        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        runner._queued_events = {}
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

        assert adapter._pending_messages[sk].text == "first"
        assert [event.text for event in runner._queued_events[sk]] == [
            "second",
            "third",
        ]
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_ack_when_agent_running(self):
        """First message during busy session should get a status ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
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
    async def test_steer_mode_calls_agent_steer_no_interrupt_no_queue(self, monkeypatch):
        """busy_input_mode='steer' injects via agent.steer() and skips queueing."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        with patch("gateway.platforms.base.merge_pending_message_event") as mock_merge:
            await runner._handle_active_session_busy_message(event, sk)

        # VERIFY: Agent was steered, NOT interrupted
        agent.steer.assert_called_once()
        injected = agent.steer.call_args.args[0]
        assert injected.endswith("also check the tests")
        assert '"chat_id": "123"' in injected
        agent.interrupt.assert_not_called()

        # VERIFY: No queueing — successful steer must NOT replay as next turn
        mock_merge.assert_not_called()

        # VERIFY: Ack mentions steer wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Steered" in content or "steer" in content.lower()
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_transcribes_voice_before_injection(self, monkeypatch):
        """A busy voice follow-up is transcribed and steered, never queued."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        runner._should_echo_stt_transcripts = MagicMock(return_value=False)
        runner._enrich_message_with_transcription = AsyncMock(
            return_value=('"yönü teknik mimariye çevir"', ["yönü teknik mimariye çevir"])
        )
        adapter = _make_adapter()

        event = _make_event(text="")
        event.message_type = MessageType.VOICE
        event.media_urls = ["/tmp/follow-up.ogg"]
        event.media_types = ["audio/ogg"]
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        runner._enrich_message_with_transcription.assert_awaited_once_with(
            "", ["/tmp/follow-up.ogg"]
        )
        agent.steer.assert_called_once()
        injected = agent.steer.call_args.args[0]
        assert injected.endswith('"yönü teknik mimariye çevir"')
        assert '"chat_id": "123"' in injected
        agent.interrupt.assert_not_called()
        assert sk not in adapter._pending_messages
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Steered" in content
        assert "Queued" not in content


    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_rejects(self):
        """If agent.steer() returns False, fall back to queue behavior."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="empty or rejected")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=False)  # rejected
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once()
        agent.interrupt.assert_not_called()
        # Fell back to queue semantics: event was stored for the next turn
        # via the FIFO path (each follow-up its own turn — no newline-merge
        # that would mash separate messages together, #43066).
        assert adapter._pending_messages.get(sk) is event

        # Ack uses queue-mode wording (not steer, not interrupt)
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content
        assert "Steered" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_pending(self):
        """If agent is still starting (sentinel), steer mode falls back to queue."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()

        event = _make_event(text="arrived too early")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Agent is still being set up — sentinel in place
        runner._running_agents[sk] = sentinel

        await runner._handle_active_session_busy_message(event, sk)

        # Event was queued instead of steered (FIFO path, #43066)
        assert adapter._pending_messages.get(sk) is event

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content

    @pytest.mark.asyncio
    async def test_interrupt_mode_text_followups_fifo_not_merged(self):
        """Two TEXT follow-ups during a busy turn (interrupt mode) must each
        get their OWN next-turn slot via FIFO — NOT newline-merged into one
        mashed-together turn (#43066 sub-bug 2). Before the fix the
        interrupt/steer-fallback path called merge_pending_message_event
        with merge_text=True, collapsing 'first' and 'second' into
        'first\\nsecond' and destroying message boundaries."""
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
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
        runner._busy_input_mode = "interrupt"
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
        runner._busy_input_mode = "interrupt"
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


class TestSteerDeliveredAck:
    """Follow-up "✅ Steer delivered" bubble for busy_input_mode=steer.

    The immediate busy-steer ack promises FUTURE delivery; a one-shot
    listener registered on the running agent fires when the steer text is
    actually injected into the model's context and sends the confirmation
    through the same adapter._send_with_retry lane as the busy ack.
    """

    class _SteerAgent:
        """Minimal agent exposing the real steer-delivery listener lane."""

        def __init__(self):
            self.steer_calls = []
            self.listeners = []

        def steer(self, text):
            self.steer_calls.append(text)
            return True

        def register_steer_delivery_listener(self, cb):
            self.listeners.append(cb)
            return True

        def unregister_steer_delivery_listener(self, cb):
            if cb in self.listeners:
                self.listeners.remove(cb)

        def fire_injection(self, text):
            """Simulate AIAgent's mid-run drain notifying its listeners."""
            for cb in list(self.listeners):
                cb(text)

        def get_activity_summary(self):
            return {}

    def _setup_steer_run(self, monkeypatch, config=None):
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.delenv(
            "HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", raising=False
        )
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: config or {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()
        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        agent = self._SteerAgent()
        runner._running_agents[sk] = agent
        return runner, adapter, event, sk, agent

    @pytest.mark.asyncio
    async def test_delivered_ack_sent_once_when_listener_fires(self, monkeypatch):
        runner, adapter, event, sk, agent = self._setup_steer_run(monkeypatch)
        runner._gateway_loop = asyncio.get_running_loop()

        assert await runner._handle_active_session_busy_message(event, sk) is True
        # Only the immediate busy-steer bubble so far — delivery hasn't happened.
        assert adapter._send_with_retry.await_count == 1
        assert "Steered" in adapter._send_with_retry.call_args.kwargs["content"]
        # Steer payload carries the message-origin preamble (see TestBusySessionAck).
        assert len(agent.steer_calls) == 1
        assert agent.steer_calls[0].endswith("also check the tests")
        assert '"chat_id": "123"' in agent.steer_calls[0]
        assert len(agent.listeners) == 1

        # Mid-run injection fires the listener (agent thread); the follow-up
        # bubble is marshalled onto the gateway loop. A duplicate drain
        # before the loop ticks must not double-ack (one-shot latch).
        agent.fire_injection("also check the tests")
        agent.fire_injection("also check the tests")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert adapter._send_with_retry.await_count == 2
        delivered = adapter._send_with_retry.call_args.kwargs
        assert "Steer delivered" in delivered["content"]
        assert delivered["chat_id"] == event.source.chat_id

        # One-shot: the listener removed itself from the agent and cleared
        # the turn slot, so a later injection cannot re-send.
        assert agent.listeners == []
        assert runner._session_state(sk).turn.steer_delivered_listener is None
        agent.fire_injection("also check the tests")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert adapter._send_with_retry.await_count == 2

    @pytest.mark.asyncio
    async def test_setting_off_registers_no_listener(self, monkeypatch):
        """display.busy_steer_delivered_ack_enabled=false → the steer still
        happens and the immediate ack still sends, but no delivered bubble
        can ever follow."""
        runner, adapter, event, sk, agent = self._setup_steer_run(
            monkeypatch,
            config={"display": {"busy_steer_delivered_ack_enabled": False}},
        )
        runner._gateway_loop = asyncio.get_running_loop()

        await runner._handle_active_session_busy_message(event, sk)

        assert len(agent.steer_calls) == 1
        assert agent.steer_calls[0].endswith("also check the tests")
        assert agent.listeners == []
        assert runner._session_state(sk).turn.steer_delivered_listener is None
        assert adapter._send_with_retry.await_count == 1  # immediate ack only

    @pytest.mark.asyncio
    async def test_env_override_off_disables_delivered_ack(self, monkeypatch):
        runner, adapter, event, sk, agent = self._setup_steer_run(
            monkeypatch,
            config={"display": {"busy_steer_delivered_ack_enabled": True}},
        )
        # Set AFTER setup — its delenv scrub cleans any inherited value first.
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_STEER_DELIVERED_ACK_ENABLED", "0")
        runner._gateway_loop = asyncio.get_running_loop()

        await runner._handle_active_session_busy_message(event, sk)

        # Env var wins over the enabled config value.
        assert agent.listeners == []
        assert adapter._send_with_retry.await_count == 1

    @pytest.mark.asyncio
    async def test_unfired_listener_dropped_at_turn_teardown(self, monkeypatch):
        """A steer that never lands must not leave a live listener on the
        agent — otherwise a later turn reusing the agent object would get a
        spurious 'delivered' bubble."""
        runner, adapter, event, sk, agent = self._setup_steer_run(monkeypatch)
        runner._gateway_loop = asyncio.get_running_loop()

        await runner._handle_active_session_busy_message(event, sk)
        assert len(agent.listeners) == 1

        assert runner._release_running_agent_state(sk) is True
        assert agent.listeners == []
        assert runner._session_state(sk).turn.steer_delivered_listener is None

        # The run is over — a stale injection notification must not send.
        agent.fire_injection("leftover")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert adapter._send_with_retry.await_count == 1

    @pytest.mark.asyncio
    async def test_second_steer_replaces_pending_listener(self, monkeypatch):
        """Two steers before any injection concatenate into one drain, so
        the older listener is replaced — exactly one delivered bubble."""
        runner, adapter, event, sk, agent = self._setup_steer_run(monkeypatch)
        runner._gateway_loop = asyncio.get_running_loop()

        await runner._handle_active_session_busy_message(event, sk)
        first = runner._session_state(sk).turn.steer_delivered_listener
        assert first is not None

        second = MessageEvent(
            text="and the migrations",
            message_type=MessageType.TEXT,
            source=event.source,  # same platform object → same adapter
            message_id="m2",
        )
        await runner._handle_active_session_busy_message(second, sk)

        assert len(agent.steer_calls) == 2
        assert agent.steer_calls[0].endswith("also check the tests")
        assert agent.steer_calls[1].endswith("and the migrations")
        assert len(agent.listeners) == 1
        assert runner._session_state(sk).turn.steer_delivered_listener is not first

        agent.fire_injection("also check the tests\nand the migrations")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Immediate ack (second is debounce-suppressed) + one delivered bubble.
        assert adapter._send_with_retry.await_count == 2
        assert "Steer delivered" in adapter._send_with_retry.call_args.kwargs["content"]


class TestBusySessionOnboardingHint:
    """First-touch hint appended to the busy-ack the first time it fires."""

    @pytest.mark.asyncio
    async def test_first_busy_ack_appends_interrupt_hint(self, tmp_path, monkeypatch):
        """First busy-while-running message gets an extra hint about /busy."""
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_hermes_home", tmp_path)
        # mark_seen imports utils.atomic_yaml_write; make sure it resolves
        # against a writable dir by pointing _hermes_home at tmp_path.
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="ping")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3, "max_iterations": 60,
            "current_tool": None, "last_activity_ts": time.time(),
            "last_activity_desc": "api", "seconds_since_activity": 0.1,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 5
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")

        # Normal ack body
        assert "Interrupting" in content
        # First-touch hint appended
        assert "First-time tip" in content
        assert "/busy queue" in content

        # The flag is now persisted to tmp_path/config.yaml
        import yaml
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["onboarding"]["seen"]["busy_input_prompt"] is True


class TestBusySteerResolverRouting:
    """Both live busy handlers — the adapter busy callback and the
    _handle_message priority fast-path — must resolve steer/redirect through
    the shared _resolve_busy_steer_or_redirect, not hand-maintained inline
    copies, so real adapter traffic runs the topical path.
    """

    @staticmethod
    def _steered_outcome():
        from gateway.run import GatewayRunner

        return GatewayRunner._BusySteerOutcome(
            effective_mode="steer",
            demoted_for_subagents=False,
            demoted_for_compression=False,
            steered=True,
            redirected=False,
        )

    def _steer_setup(self, monkeypatch):
        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
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
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        return runner, adapter, event, sk, agent

    @pytest.mark.asyncio
    async def test_handle_message_busy_flow_routes_through_resolver(self, monkeypatch):
        """(production routing) A busy follow-up entering _handle_message hits
        the shared resolver with the live session key, resolved busy mode and
        running agent — and honors its steered outcome (nothing queued)."""
        runner, adapter, event, sk, agent = self._steer_setup(monkeypatch)
        from gateway.run import GatewayRunner

        spy = AsyncMock(return_value=self._steered_outcome())
        with patch.object(GatewayRunner, "_resolve_busy_steer_or_redirect", spy):
            assert await runner._handle_message(event) is None

        spy.assert_awaited_once()
        resolved_event, resolved_key, resolved_mode, resolved_agent = (
            spy.call_args.args
        )
        assert resolved_event is event
        assert resolved_key == sk
        assert resolved_mode == "steer"
        assert resolved_agent is agent
        # Steered outcome honored: the follow-up must NOT also queue behind
        # the run it was injected into.
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_busy_session_handler_routes_through_resolver(self, monkeypatch):
        """(production routing) The adapter busy callback resolves through the
        same shared resolver and honors its steered outcome end-to-end."""
        runner, adapter, event, sk, agent = self._steer_setup(monkeypatch)
        from gateway.run import GatewayRunner

        spy = AsyncMock(return_value=self._steered_outcome())
        with patch.object(GatewayRunner, "_resolve_busy_steer_or_redirect", spy):
            assert await runner._handle_active_session_busy_message(event, sk) is True

        spy.assert_awaited_once()
        resolved_event, resolved_key, resolved_mode, resolved_agent = (
            spy.call_args.args
        )
        assert resolved_event is event
        assert resolved_key == sk
        assert resolved_mode == "steer"
        assert resolved_agent is agent
        # Steered outcome honored: steer-ack wording, nothing queued.
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Steered" in content
        assert "Queued" not in content
        assert sk not in adapter._pending_messages


class TestLongRunningNotificationOwnership:
    """The long-running heartbeat must stop once its run no longer owns the
    session slot or the executor finished — otherwise a stale
    'running: delegate_agent' bubble outlives the run that spawned it (#12029).
    Restart-drain suppression is scoped to sessions whose chat was actually
    notified of the pending drain.
    """

    @staticmethod
    def _qualifying_runner(agent):
        """Bare runner whose 'sess' turn slot is owned by ``agent`` — state
        that otherwise qualifies the heartbeat for emission."""
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}
        runner._running_agents["sess"] = agent
        return runner

    def test_notification_stops_after_session_ownership_moves(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}

        original_agent = MagicMock()
        replacement_agent = MagicMock()
        runner._running_agents["sess"] = replacement_agent

        assert runner._should_emit_long_running_notification(
            "sess", original_agent, executor_task=None
        ) is False

    def test_notification_emitted_for_session_not_notified_of_drain(self):
        """A restart pending with nobody told (SIGUSR1, updater, control
        socket) must not silence a working session's heartbeat: that chat
        never learned a drain was pending, so the heartbeat is the only
        liveness signal it has while the drain waits without timeout."""
        agent = MagicMock()
        runner = self._qualifying_runner(agent)
        runner._restart_requested = True
        # No restart command source and no park-steered sessions: no notice.

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=None
        ) is True

    def test_notification_emitted_for_working_session_in_other_chat(self):
        """Cross-chat restart: the requester's chat was told, but a different
        busy session keeps heartbeating through the whole drain."""
        agent = MagicMock()
        runner = self._qualifying_runner(agent)
        runner._restart_requested = True
        runner._restart_command_source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="999",
            chat_type="dm", user_id="requester",
        )

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=None
        ) is True

    def test_notification_suppressed_for_requesters_session(self):
        """The requester's chat — the one the restart wind-down embed targets —
        stays quiet for the drain (the original suppression contract)."""
        agent = MagicMock()
        runner = self._qualifying_runner(agent)
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="123",
            chat_type="dm", user_id="requester",
        )
        requester_key = runner._session_key_for_source(source)
        runner._running_agents[requester_key] = agent
        runner._restart_requested = True
        runner._restart_command_source = source

        assert runner._should_emit_long_running_notification(
            requester_key, agent, executor_task=None
        ) is False

    def test_notification_suppressed_for_park_steered_session(self):
        """A session whose agent accepted the cooperative park steer was told
        in-band to wind down; its heartbeat stays quiet for the drain."""
        agent = MagicMock()
        runner = self._qualifying_runner(agent)
        runner._restart_requested = True
        runner._cooperative_restart_steered_sessions = ["sess"]

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=None
        ) is False

    def test_notification_emitted_when_restart_not_requested(self):
        agent = MagicMock()
        runner = self._qualifying_runner(agent)
        runner._restart_requested = False

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=None
        ) is True

    def test_notification_emitted_when_restart_flag_missing(self):
        # Back-compat: bare runners built without the attribute still heartbeat.
        agent = MagicMock()
        runner = self._qualifying_runner(agent)

        assert runner._should_emit_long_running_notification(
            "sess", agent, executor_task=None
        ) is True


class TestLongRunningHeartbeatIterationDetail:
    """The long-running heartbeat renders its status detail through the
    shared iteration formatter — unbounded runs must not print the
    sys.maxsize sentinel (#102806), matching the busy acknowledgement.
    """

    def test_unbounded_iterations_render_without_the_sentinel(self):
        from gateway.run import GatewayRunner

        detail = GatewayRunner._format_long_running_status_detail(
            {"api_call_count": 3, "max_iterations": sys.maxsize,
             "current_tool": "terminal"},
            include_iterations=True,
        )

        assert detail == " — iteration 3, terminal"
        assert str(sys.maxsize) not in detail

    def test_bounded_iterations_keep_the_denominator(self):
        from gateway.run import GatewayRunner

        detail = GatewayRunner._format_long_running_status_detail(
            {"api_call_count": 21, "max_iterations": 60,
             "last_activity_desc": "packing context"},
            include_iterations=True,
        )

        assert detail == " — iteration 21/60, packing context"

    def test_iteration_counter_hidden_when_detail_not_opted_in(self):
        from gateway.run import GatewayRunner

        detail = GatewayRunner._format_long_running_status_detail(
            {"api_call_count": 21, "max_iterations": 60, "current_tool": "bash"},
            include_iterations=False,
        )

        assert detail == " — bash"
        assert "iteration" not in detail

    def test_missing_summary_renders_no_detail(self):
        from gateway.run import GatewayRunner

        assert GatewayRunner._format_long_running_status_detail(
            {}, include_iterations=True
        ) == ""
        assert GatewayRunner._format_long_running_status_detail(
            None, include_iterations=True
        ) == ""
    @pytest.mark.asyncio
    async def test_restart_during_heartbeat_edit_sends_no_fallback_bubble(self, monkeypatch):
        """The guard is rechecked after the awaited edit: a restart that begins while the edit is
        in flight must not be followed by a fresh "Working" send when that edit fails (#10990)."""
        import asyncio
        from types import SimpleNamespace
        from gateway.run import GatewayRunner
        from gateway.turn_context import TurnContext

        monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0.01")
        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}
        runner._draining = runner._restart_requested = False
        adapter = MagicMock()
        first_send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="hb-1"))
        adapter.send = first_send

        async def _edit_then_restart(*a, **k):
            runner._restart_requested = True  # restart notice goes out while the edit is awaited
            return SimpleNamespace(success=False)

        adapter.edit_message = AsyncMock(side_effect=_edit_then_restart)
        runner._adapter_for_source = lambda source: adapter
        runner._agent_activity_summary = staticmethod(lambda agent: None)
        agent = MagicMock()
        runner._running_agents["sess"] = agent
        disp = MagicMock()
        disp._display_surface_mode.return_value = "on"
        disp.resolve_display_setting.return_value = False
        ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform="telegram"), session_key="sess")
        ctx.agent_holder[0] = agent

        await asyncio.wait_for(runner._run_agent_notify_long_running(disp, ctx, [None]), 5)

        assert first_send.await_count == 1  # the original heartbeat only
        adapter.edit_message.assert_awaited_once()

    @pytest.mark.parametrize("flag", ["_draining", "_restart_requested"])
    def test_notification_stops_once_shutdown_or_restart_begins(self, flag):
        """After the restart/shutdown notice a heartbeat would contradict it (#10990)."""
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}
        agent = MagicMock()
        runner._running_agents["sess"] = agent
        assert runner._should_emit_long_running_notification("sess", agent, executor_task=None) is True
        setattr(runner, flag, True)
        assert runner._should_emit_long_running_notification("sess", agent, executor_task=None) is False


