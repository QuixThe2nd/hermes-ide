"""Webhook inbound during a restart drain (no overlap support).

The webhook transport has NO overlap/hub attachment, so the successor defers
it until the predecessor exits — during the drain window the draining
predecessor is the webhook's only server. A webhook that fires mid-drain is
therefore ADMITTED and runs as a normal turn in the draining process (see
``gateway.overlap_restart.admits_local_drain_turn``): no refusal text, no
durable drain snapshot, no successor turn synthesized for it.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.event import ProcessingOutcome
from gateway.run_drain_queue import claimed_drain_queue_path, drain_queue_path
from gateway.session import SessionEntry, build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)

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
    runner._handle_message_with_agent = AsyncMock(return_value="turn-done")
    runner._run_post_turn_hooks = AsyncMock()
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
async def test_webhook_message_mid_drain_runs_in_the_draining_predecessor():
    """A webhook message that reaches the draining predecessor runs as a
    normal turn there — the predecessor is the webhook's only server until it
    exits, so refusing it would lose the message for the whole drain."""
    runner, adapter = _make_gate_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("hook arrived mid-drain"))

    assert result == "turn-done"
    runner._handle_message_with_agent.assert_awaited_once()
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_webhook_interrupt_mode_also_runs_the_turn_locally():
    """Same admission under the default interrupt busy mode: no queue promise,
    no file, no refusal — the turn simply runs here."""
    runner, adapter = _make_gate_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "interrupt"

    result = await runner._handle_message(_make_event("interrupt-mode hook"))

    assert result == "turn-done"
    runner._handle_message_with_agent.assert_awaited_once()
    assert not drain_queue_path().exists()
    assert not adapter._pending_messages
