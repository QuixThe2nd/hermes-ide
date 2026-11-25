"""Processing-hook parity for queued follow-up turns.

A message that arrives while a turn is already running is parked in the
adapter's ``_pending_messages`` slot and drained *in-band* by
``GatewayRunner._run_agent`` rather than by
``BasePlatformAdapter._process_message_background``.  The runner-side drain
must still fire the ``on_processing_start`` / ``on_processing_complete``
lifecycle hooks, otherwise every platform that renders a read-receipt
reaction from those hooks (Slack 👀, Discord, Feishu, Matrix,
Signal, ...) silently skips the acknowledgement for mid-turn messages.
"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource


class HookRecordingAdapter(BasePlatformAdapter):
    """Adapter that records the processing-hook lifecycle it is driven through."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.started: list = []
        self.completed: list = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.started.append(getattr(event, "message_id", None))

    async def on_processing_complete(self, event, outcome) -> None:
        self.completed.append((getattr(event, "message_id", None), outcome))


class _TwoTurnAgent:
    calls: list = []

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class _RaisingSecondTurnAgent:
    calls: list = []

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        if len(type(self).calls) >= 2:
            raise RuntimeError("boom in the queued follow-up turn")
        return {
            "final_response": "done-1",
            "messages": [],
            "api_calls": 1,
        }


class _NoReplyTerminalAgent:
    """First turn answers normally; the queued follow-up answers with a silence marker."""

    calls: list = []

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        if len(type(self).calls) >= 2:
            return {"final_response": "NO_REPLY", "messages": [], "api_calls": 1}
        return {"final_response": "done-1", "messages": [], "api_calls": 1}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _install_fake_agent(monkeypatch, tmp_path, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )


SESSION_KEY = "agent:main:discord:dm:4242"


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_last", [False, True])
async def test_queue_terminal_presentation_belongs_to_last_turn(monkeypatch, tmp_path, diagnostic_last):
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)
    (tmp_path / "config.yaml").write_text("display: {suppress_warning_notifications: true}")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="follow-up", source=_source(), internal=diagnostic_last,
        metadata={"notification_category": "diagnostic"} if diagnostic_last else {}, message_id="queued")
    result = await runner._run_agent(message="first", context_prompt="", history=[], source=_source(),
        session_id="queue-policy", session_key=SESSION_KEY,
        persist_user_display_metadata=None if diagnostic_last else {"notification_category": "diagnostic"})
    assert _TwoTurnAgent.calls == ["first", "follow-up"]
    assert result["final_response"] == "done-2"
    assert result["_notification_reply_muted"] is diagnostic_last
    from gateway.warning_notifications import diagnostic_wake_muted
    outer = MessageEvent(text="first", source=_source(), internal=not diagnostic_last)
    outer._notification_reply_muted = result["_notification_reply_muted"]
    assert diagnostic_wake_muted(outer) is diagnostic_last


def _source():
    return SessionSource(platform=Platform.DISCORD, chat_id="4242", chat_type="dm")


@pytest.mark.asyncio
async def test_queued_followup_fires_processing_hooks(monkeypatch, tmp_path):
    """The runner-drained follow-up gets the same start/complete hooks as a
    message that arrives while the session is idle."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-1",
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks",
        session_key=SESSION_KEY,
    )

    # The follow-up really did run in-band.
    assert result["final_response"] == "done-2"
    assert _TwoTurnAgent.calls == ["the first turn", "the follow-up"]

    # ...and it was acknowledged through the lifecycle hooks.
    assert adapter.started == ["queued-1"]
    assert adapter.completed == [("queued-1", ProcessingOutcome.SUCCESS)]


@pytest.mark.asyncio
async def test_queued_followup_failure_completes_the_hook(monkeypatch, tmp_path):
    """A follow-up turn that blows up still closes its hook, so a platform
    never strands a 'still working' marker on the user's message."""
    _RaisingSecondTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _RaisingSecondTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the doomed follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-2",
    )

    with pytest.raises(RuntimeError):
        await runner._run_agent(
            message="the first turn",
            context_prompt="",
            history=[],
            source=_source(),
            session_id="sess-hooks-failure",
            session_key=SESSION_KEY,
        )

    assert adapter.started == ["queued-2"]
    assert adapter.completed == [("queued-2", ProcessingOutcome.FAILURE)]


@pytest.mark.asyncio
async def test_synthetic_followup_is_not_acknowledged(monkeypatch, tmp_path):
    """Drains with no inbound platform message — /goal continuations, wake-ups,
    CLI hand-offs — carry no message_id and must stay silent: there is nothing
    on the platform to react to."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="synthetic continuation",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=None,
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-synthetic",
        session_key=SESSION_KEY,
    )

    # It still ran — we only suppressed the acknowledgement, not the turn.
    assert result["final_response"] == "done-2"
    assert _TwoTurnAgent.calls == ["the first turn", "synthetic continuation"]

    assert adapter.started == []
    assert adapter.completed == []


@pytest.mark.asyncio
async def test_raw_envelope_only_followup_is_acknowledged(monkeypatch, tmp_path):
    """Signal never sets message_id — its hook keys off the raw envelope
    (sender + timestamp_ms) — and Discord's reads raw_message. An event
    carrying only a raw envelope is still a real inbound message."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="signal-shaped follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=None,
        raw_message={"sender": "+15550100", "timestamp_ms": 1700000000000},
    )

    await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-raw",
        session_key=SESSION_KEY,
    )

    assert adapter.started == [None]
    assert adapter.completed == [(None, ProcessingOutcome.SUCCESS)]


class CompleteOnlyAdapter(HookRecordingAdapter):
    """Google Chat and webhook implement on_processing_complete WITHOUT
    on_processing_start; theirs is end-of-cycle teardown (reap the typing
    card / end the delivery session), not a reaction."""

    on_processing_start = BasePlatformAdapter.on_processing_start


@pytest.mark.asyncio
async def test_complete_only_adapter_is_left_alone(monkeypatch, tmp_path):
    """We bracket, so both halves must belong to us. An adapter that only
    implements the completion half must not be handed a completion here: at
    this point the follow-up's reply has not been delivered yet, so its
    teardown would fire against a live turn."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = CompleteOnlyAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-3",
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-complete-only",
        session_key=SESSION_KEY,
    )

    assert result["final_response"] == "done-2"

    assert adapter.completed == []


@pytest.mark.asyncio
async def test_live_chain_human_opener_internal_terminal_silence_is_allowed(monkeypatch, tmp_path):
    """#916 review: a human message opens the turn and an internal notification runs as its
    queued follow-up. The terminal ``NO_REPLY`` must classify as MACHINERY silence at the
    live recursive boundary — the chain stays silent instead of surfacing the
    "silence marker rejected on a user turn" fallback keyed on the opening human event."""
    _NoReplyTerminalAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _NoReplyTerminalAgent)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="wake: background delegation finished",
        message_type=MessageType.TEXT,
        source=_source(),
        internal=True,
        message_id="wake-1",
    )

    result = await runner._run_agent(
        message="the human opener",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-live-internal-terminal",
        session_key=SESSION_KEY,
    )

    assert _NoReplyTerminalAgent.calls == ["the human opener", "wake: background delegation finished"]
    # The same predicate _hmwa_shape_agent_response applies, with the opening human
    # turn's display kind as the fallback classification.
    from gateway.response_filters import (
        is_intentional_silence_agent_result,
        is_machinery_display_kind,
    )

    assert is_intentional_silence_agent_result(result, result["final_response"])
    assert is_machinery_display_kind(result.get("queued_terminal_display_kind", None))
    assert result["queued_terminal_inbound_id"] == "wake-1"


@pytest.mark.asyncio
async def test_live_chain_internal_opener_human_terminal_marker_is_rejected(monkeypatch, tmp_path):
    """Reverse chain: an internal wake opens, a human message runs as its queued follow-up.
    The terminal human turn's ``NO_REPLY`` must be REJECTED (visible fallback), never
    silently accepted under the opening internal event's machinery classification."""
    _NoReplyTerminalAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _NoReplyTerminalAgent)
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="and what happened next?",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="q-1",
    )

    result = await runner._run_agent(
        message="wake: scheduled check",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-live-human-terminal",
        session_key=SESSION_KEY,
        persist_user_display_kind="internal_notification",
    )

    assert _NoReplyTerminalAgent.calls == ["wake: scheduled check", "and what happened next?"]
    from gateway.response_filters import (
        is_intentional_silence_agent_result,
        is_machinery_display_kind,
    )

    assert is_intentional_silence_agent_result(result, result["final_response"])
    # Exactly the shaping helper's expression: the TERMINAL kind (None for the human
    # follow-up) wins over the opener's "internal_notification" default.
    assert not is_machinery_display_kind(
        result.get("queued_terminal_display_kind", "internal_notification"))
    assert adapter.completed == [("q-1", ProcessingOutcome.SUCCESS)]
