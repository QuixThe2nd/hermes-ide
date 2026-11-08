"""Successor delivery during a restart window: webhook (no overlap support).

The webhook transport has NO overlap/hub attachment: inbound webhooks keep
arriving at the predecessor for as long as it holds the listener — it still
feeds the predecessor mid-drain. There is no successor handoff path for it,
so the durable drain snapshot IS its successor-delivery vehicle: the
predecessor durably records the message (never silently processes it) and
the successor process replays it into a real turn at startup. This is the
transport that shrinks the predecessor-side queue instead of deleting it.
"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.event import ProcessingOutcome
from gateway.run_drain_queue import claimed_drain_queue_path, drain_queue_path, replay_drain_queue
from gateway.session import SessionEntry, build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)

QUEUED_MARKER = "queued for the next turn after it comes back"


def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(platform=Platform.WEBHOOK),
        message_id="m-1",
    )


def _session_entry(source):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.WEBHOOK,
        chat_type="dm",
        total_tokens=0,
    )


def _make_gate_runner():
    adapter = RestartTestAdapter(Platform.WEBHOOK)
    runner, _wired = make_restart_runner(adapter=adapter, platform=Platform.WEBHOOK)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry(
        make_restart_source(platform=Platform.WEBHOOK)
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
    return runner, adapter


async def _settle(runner) -> None:
    if runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))
    for adapter in runner.adapters.values():
        for call in getattr(adapter.handle_message, "await_args_list", []):
            callback = getattr(call.args[0], "_gateway_replay_completion", None)
            if callable(callback):
                callback(ProcessingOutcome.SUCCESS)


@pytest.mark.asyncio
async def test_webhook_message_mid_drain_is_durably_queued_never_processed():
    """A webhook message that reaches the draining predecessor is recorded in
    the durable drain snapshot and answered with the queued ack — the
    predecessor starts no turn for it (that would be "queued for later
    predecessor processing", exactly what must never happen silently)."""
    runner, adapter = _make_gate_runner()
    handler = adapter._message_handler
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("hook arrived mid-drain"))

    assert isinstance(result, str) and QUEUED_MARKER in result
    handler.assert_not_called()  # the predecessor never starts the turn
    session_key = runner._session_key_for_source(
        make_restart_source(platform=Platform.WEBHOOK)
    )
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert [e["event"]["text"] for e in events] == ["hook arrived mid-drain"]
    assert events[0]["source"]["platform"] == "webhook"
    assert events[0]["session_key"] == session_key
    assert session_key in adapter._pending_messages


@pytest.mark.asyncio
async def test_successor_process_replays_the_webhook_message_into_a_turn():
    """End to end: the successor process (fresh runner, same HERMES_HOME)
    replays the webhook message the draining predecessor recorded — one real
    turn on the successor, then the snapshot is fully settled."""
    predecessor, _padapter = _make_gate_runner()
    predecessor._draining = True
    predecessor._restart_requested = True
    predecessor._busy_input_mode = "queue"
    result = await predecessor._handle_message(_make_event("hook for the successor"))
    assert isinstance(result, str) and QUEUED_MARKER in result
    session_key = predecessor._session_key_for_source(
        make_restart_source(platform=Platform.WEBHOOK)
    )
    # The in-memory FIFO dies with the predecessor process; only the snapshot
    # survives — exactly what the successor boots from.

    successor, successor_adapter = make_restart_runner(
        adapter=RestartTestAdapter(Platform.WEBHOOK), platform=Platform.WEBHOOK
    )
    successor_adapter.handle_message = AsyncMock()

    assert replay_drain_queue(successor) == 1
    await _settle(successor)

    successor_adapter.handle_message.assert_awaited_once()
    replayed = successor_adapter.handle_message.await_args.args[0]
    assert replayed.text == "hook for the successor"
    assert replayed.source.platform == Platform.WEBHOOK
    assert replayed.redelivered is True
    # At-most-once: both snapshot files are gone once the turn settles.
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()
    assert not successor._is_session_running(session_key)


@pytest.mark.asyncio
async def test_webhook_interrupt_mode_refuses_honestly_without_a_queue_promise():
    """Control: with the default interrupt busy mode the predecessor gives
    the honest refusal — no queue promise, no file, no silent processing."""
    runner, adapter = _make_gate_runner()
    handler = adapter._message_handler
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "interrupt"

    result = await runner._handle_message(_make_event("interrupt-mode hook"))

    assert isinstance(result, str) and "not accepting new work" in result
    assert handler.assert_not_called() is None
    assert not drain_queue_path().exists()
    assert not adapter._pending_messages
