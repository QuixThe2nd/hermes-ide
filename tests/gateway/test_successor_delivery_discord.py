"""Successor delivery during a restart window: Discord (shared hub transport).

The restart pipeline earns deletion of the predecessor-side drain queue only
if every transport routes NEW user messages away from a draining predecessor.
Discord attaches through the multi-generation hub: a new user message that
reaches the draining predecessor is returned to the hub (``handoff_event``) —
never queued on the predecessor and never silently dropped. This holds in the
PRE-ATTACH window too: the hub retains a handed-off record with no eligible
generation and replays it when the successor attaches, so a restart stays
invisible to the user. The durable drain snapshot stays as the crash-only
fallback — a hub that refuses every handoff past the successor-attach
deadline (hub died, connection gone).
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


def _hub_adapter(*, successor_ready: bool, router=None):
    adapter = DiscordHubClientAdapter(
        PlatformConfig(token="fake-token", typing_indicator=False)
    )
    handoffs = []

    async def fake_call(method, params=None):
        assert method == "handoff", f"unexpected hub RPC: {method}"
        handoffs.append(params or {})
        if router is not None:
            return await router.handoff(
                params["session_key"], adapter._generation_id,
                int(params["version"]), params["record"],
                target_pid=params.get("target_pid"),
            )
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
async def test_pre_attach_message_parks_at_hub_and_reaches_the_successor():
    """Successor NOT yet attached (the overlap pre-attach window): the hub
    accepts the handoff anyway — it retains the record for the successor and
    replays it on attach — so the restart stays invisible: no drain-queue
    snapshot, no re-delivery banner, the successor handles the message
    directly once it attaches."""
    from plugins.platforms.discord.hub import DiscordSessionRouter

    router = DiscordSessionRouter()
    predecessor_received = []
    successor_received = []

    adapter, handoffs = _hub_adapter(successor_ready=False, router=router)
    router.attach(adapter._generation_id, lambda key, record: _record(
        predecessor_received, key, record))
    adapter.set_message_handler(AsyncMock())
    handler = adapter._message_handler
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("parked during the pre-attach window")
    session_key = runner._session_key_for_source(event.source)
    # Establish the session's hub version the way the delivering hub does: the
    # hub routes the original inbound delivery to the predecessor generation.
    assert await router.route(session_key, {
        "session_key": session_key, "event": {"text": "original delivery"},
    }) == adapter._generation_id
    adapter._session_versions[session_key] = predecessor_received[0][1]["_discord_hub_version"]

    result = await runner._handle_message(event)

    assert result is None  # the hub owns the event; the predecessor acks nothing
    assert len(handoffs) == 1
    handler.assert_not_called()  # the predecessor starts no turn for it
    assert not adapter._pending_messages
    assert not drain_queue_path().exists()
    assert len(predecessor_received) == 1  # the handoff never re-delivered to it
    assert len(router.undelivered) == 1  # parked at the hub, unacknowledged

    # The successor generation attaches later and claims the parked record.
    router.attach("successor-gen", lambda key, record: _record(
        successor_received, key, record))
    assert await router.replay_undelivered() == 1
    assert router.undelivered == []
    assert len(successor_received) == 1
    assert successor_received[0][0] == session_key
    assert successor_received[0][1]["event"]["text"] == "parked during the pre-attach window"


async def _record(sink, key, record):
    sink.append((key, record))


@pytest.mark.asyncio
async def test_dead_hub_falls_back_to_durable_queue_after_attach_deadline():
    """Crash-only fallback: when the hub itself refuses every handoff (hub
    died, connection gone) and no successor ever attaches, the predecessor
    waits out the successor-attach deadline and only then leaves the event to
    the durable drain snapshot — never a silent drop."""
    adapter, handoffs = _hub_adapter(successor_ready=False)
    adapter.overlap_successor_attach_timeout = 0.02

    async def dead_hub(method, params=None):
        handoffs.append(params or {})
        raise ConnectionError("Discord hub is not attached")

    adapter._call = dead_hub
    adapter.set_message_handler(AsyncMock())
    handler = adapter._message_handler
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("hub is gone")

    result = await runner._handle_message(event)

    assert isinstance(result, str) and QUEUED_MARKER in result
    assert len(handoffs) >= 2  # retried past the first refusal before giving up
    handler.assert_not_called()
    session_key = runner._session_key_for_source(event.source)
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert [e["event"]["text"] for e in events] == ["hub is gone"]
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
