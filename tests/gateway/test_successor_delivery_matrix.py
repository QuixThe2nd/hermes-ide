"""Successor delivery during a restart window: Matrix (shared hub transport).

Mirror of the Discord successor-delivery proof for the Matrix hub client: a
new user message reaching a draining predecessor is returned to the hub —
never queued on the predecessor — including the PRE-ATTACH window, where the
hub retains the record and replays it when the successor attaches. The
durable drain snapshot stays as the crash-only fallback for a hub that
refuses every handoff past the successor-attach deadline.
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
from plugins.platforms.matrix.hub_client import MatrixHubClientAdapter
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

QUEUED_MARKER = "queued for the next turn after it comes back"


def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(platform=Platform.MATRIX),
        message_id="m-1",
    )


def _session_entry(source):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.MATRIX,
        chat_type="dm",
        total_tokens=0,
    )


def _make_gate_runner(adapter):
    runner, _wired = make_restart_runner(adapter=adapter, platform=Platform.MATRIX)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry(
        make_restart_source(platform=Platform.MATRIX)
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
    adapter = MatrixHubClientAdapter(PlatformConfig(token="fake-token"))
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


async def _record(sink, key, record):
    sink.append((key, record))


@pytest.mark.asyncio
async def test_new_user_message_mid_drain_hands_off_to_successor_never_queues():
    """Successor attached: the draining predecessor returns the event to the
    hub; its drain queue and in-memory FIFO stay empty."""
    adapter, handoffs = _hub_adapter(successor_ready=True)
    runner = _make_gate_runner(adapter)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("route me to the successor")

    result = await runner._handle_message(event)

    assert result is None
    session_key = runner._session_key_for_source(event.source)
    assert len(handoffs) == 1
    assert handoffs[0]["session_key"] == session_key
    assert handoffs[0]["record"]["event"]["text"] == "route me to the successor"
    assert handoffs[0]["record"]["source"]["platform"] == "matrix"
    assert not adapter._pending_messages
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_pre_attach_message_parks_at_hub_and_reaches_the_successor():
    """Successor NOT yet attached: the hub retains the handed-off record and
    replays it on the successor's attach — no drain-queue snapshot on the
    predecessor, no predecessor turn, the successor delivers it directly."""
    from plugins.platforms.discord.hub import DiscordSessionRouter

    router = DiscordSessionRouter(version_field="_matrix_hub_version")
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
    assert await router.route(session_key, {
        "session_key": session_key, "event": {"text": "original delivery"},
    }) == adapter._generation_id
    adapter._session_versions[session_key] = (
        predecessor_received[0][1]["_matrix_hub_version"])

    result = await runner._handle_message(event)

    assert result is None
    assert len(handoffs) == 1
    handler.assert_not_called()
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


@pytest.mark.asyncio
async def test_dead_hub_falls_back_to_durable_queue_after_attach_deadline():
    """Crash-only fallback: a hub refusing every handoff (hub died) with no
    successor attaching leaves the event to the durable drain snapshot —
    only after the successor-attach deadline, never a silent drop."""
    adapter, handoffs = _hub_adapter(successor_ready=False)
    adapter.overlap_successor_attach_timeout = 0.02

    async def dead_hub(method, params=None):
        handoffs.append(params or {})
        raise ConnectionError("Matrix hub is not attached")

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
