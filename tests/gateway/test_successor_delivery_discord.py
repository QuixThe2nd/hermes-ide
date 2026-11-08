"""Successor delivery during a restart window: Discord (shared hub transport).

The restart pipeline earns deletion of the predecessor-side drain queue only
if every transport routes NEW user messages away from a draining predecessor.
Discord attaches through the multi-generation hub: once the overlap successor
has attached, a new user message that reaches the draining predecessor is
returned to the hub (``handoff_event``) — never queued on the predecessor and
never silently dropped. When the successor has NOT attached (hub spawn
failed / never became ready), the predecessor keeps the durable drain
snapshot as the safety net — the one case the queue legitimately serves.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run_drain_queue import drain_queue_path
from gateway.session import SessionEntry, build_session_key
from plugins.platforms.discord.hub_client import DiscordHubClientAdapter
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

QUEUED_MARKER = "queued for the next turn after it comes back"


def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(platform=Platform.DISCORD),
        message_id="m-1",
    )


def _session_entry(source):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="dm",
        total_tokens=0,
    )


def _make_gate_runner(adapter):
    """Bare runner carrying a message through the real ``_handle_message``
    cold path to the authoritative drain gate (shape proven by
    test_restart_drain_gate.py), wired with the REAL Discord hub client."""
    runner, _wired = make_restart_runner(adapter=adapter, platform=Platform.DISCORD)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry(
        make_restart_source(platform=Platform.DISCORD)
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_a, **_k: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **k: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._queued_events = {}
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_task_counter = 0
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._peek_session_state = lambda _key: None
    runner._is_session_running = lambda _key: False
    return runner


def _hub_adapter(*, successor_ready: bool):
    adapter = DiscordHubClientAdapter(
        PlatformConfig(token="fake-token", typing_indicator=False)
    )
    handoffs = []

    async def fake_call(method, params=None):
        assert method == "handoff", f"unexpected hub RPC: {method}"
        handoffs.append(params or {})
        return True

    adapter._call = fake_call
    adapter._running = True
    if successor_ready:
        adapter.mark_overlap_successor_ready()
    return adapter, handoffs


@pytest.mark.asyncio
async def test_new_user_message_mid_drain_hands_off_to_successor_never_queues():
    """Successor attached: a new user message on the draining predecessor is
    returned to the hub for the successor to deliver — the predecessor's
    drain queue stays empty even in queue mode (the posture where the queue
    would otherwise claim it)."""
    adapter, handoffs = _hub_adapter(successor_ready=True)
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"  # the queue would take it if handoff failed
    event = _make_event("run me in the successor")

    result = await runner._handle_message(event)

    assert result is None  # no ack from the predecessor: the successor owns it
    session_key = runner._session_key_for_source(event.source)
    assert len(handoffs) == 1
    assert handoffs[0]["session_key"] == session_key
    assert handoffs[0]["record"]["event"]["text"] == "run me in the successor"
    assert handoffs[0]["record"]["source"]["platform"] == "discord"
    assert not adapter._pending_messages  # nothing parked in-memory either
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_no_ready_successor_falls_back_to_durable_queue_not_a_silent_drop():
    """Successor NOT attached (spawn failed / still pending): the hub handoff
    is not offered, and the predecessor must not silently process or drop the
    message — the durable drain snapshot is the delivery vehicle the
    successor replays at startup. This is the shrunk case that keeps the
    predecessor-side queue alive."""
    adapter, handoffs = _hub_adapter(successor_ready=False)
    adapter.set_message_handler(AsyncMock())
    handler = adapter._message_handler
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("no successor ever attached")

    result = await runner._handle_message(event)

    assert isinstance(result, str) and QUEUED_MARKER in result
    assert handoffs == []  # readiness gate refused before any hub RPC
    handler.assert_not_called()  # the predecessor starts no turn for it
    session_key = runner._session_key_for_source(event.source)
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert [e["event"]["text"] for e in events] == ["no successor ever attached"]
    assert events[0]["session_key"] == session_key
    assert session_key in adapter._pending_messages


@pytest.mark.asyncio
async def test_busy_owned_session_followup_stays_with_predecessor_in_memory():
    """Owned in-flight work is what the drain waits out: a follow-up on a
    RUNNING session stays with the predecessor's in-memory FIFO — never the
    drain queue (the successor takes the session only after handover)."""
    adapter, handoffs = _hub_adapter(successor_ready=True)
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("owned follow-up")
    session_key = runner._session_key_for_source(event.source)
    runner._is_session_running = lambda key: key == session_key
    runner._running_agents[session_key] = MagicMock()

    result = await runner._handle_message(event)

    # Owned work: the drain gate is transparent and the busy path queues the
    # follow-up in-memory for the turn the predecessor is still finishing.
    assert result is None
    assert handoffs == []
    assert adapter._pending_messages.get(session_key) is event or (
        session_key in adapter._pending_messages
    )
    assert not drain_queue_path().exists()
