"""Non-handoff adapters must keep working through a restart drain window.

Only hub adapters (``supports_overlap_restart is True`` — Discord/Matrix
hub clients) can hand an inbound event to the overlap successor; every other
adapter (WhatsApp bridge, webhook, api_server, ...) is DEFERRED by the
successor until the predecessor exits (``defer_primary_adapter`` /
``defer_secondary_adapter``). During the drain window the draining
predecessor is therefore the ONLY process that can serve those platforms,
and refusing their events ("not accepting new work right now") loses the
message for as long as the drain lasts — a live incident saw a WhatsApp DM
refused twice across an 8+ hour drain while the successor never connected
WhatsApp at all.

Invariants proven here:
- a plain-text event on a non-handoff adapter runs a normal turn in the
  draining process, with and without a pending successor, and no refusal
  text is produced;
- hub adapters (Discord/Matrix) keep the existing handoff path — their
  events are never run by the draining predecessor;
- terminal shutdown (adapters already disconnecting, ``_running`` cleared)
  keeps its refusal.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, build_session_key
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)

_DRAIN_NOTICE_MARKER = "not accepting"


def _make_event(text: str, *, platform: Platform = Platform.WHATSAPP):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(platform=platform),
        message_id="m-1",
    )


def _session_entry(source):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=source.platform,
        chat_type="dm",
        total_tokens=0,
    )


def _make_drain_runner(*, platform: Platform = Platform.WHATSAPP):
    """A draining predecessor serving a non-handoff adapter (WhatsApp-like)."""
    adapter = RestartTestAdapter(platform)
    runner, _wired = make_restart_runner(adapter=adapter, platform=platform)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry(
        make_restart_source(platform=platform)
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
    # The drain window exactly as request_restart leaves it: admission closed,
    # adapters still connected, a successor (maybe) pending.
    runner._draining = True
    runner._restart_requested = True
    # The turn itself — mocked one level below the admission decision.
    runner._handle_message_with_agent = AsyncMock(return_value="turn-done")
    runner._run_post_turn_hooks = AsyncMock()
    return runner, adapter


def _mark_busy(runner, *, platform: Platform = Platform.WHATSAPP):
    """Put the event's session into the running-agent busy path."""
    from gateway.session_state import SessionState

    key = build_session_key(make_restart_source(platform=platform))
    runner._is_session_running = lambda _key: True
    agent = MagicMock()
    runner._running_agents[key] = agent
    state = SessionState()
    state.turn.agent = agent
    state.turn.started_ts = 0.0  # outside every follow-up grace window
    runner._peek_session_state = lambda _key: state
    return key


@pytest.mark.asyncio
async def test_drain_window_plain_text_runs_a_turn_in_the_draining_process():
    """Without any successor pending: the WhatsApp DM runs as a normal turn —
    no refusal text, the agent handler is entered."""
    runner, _adapter = _make_drain_runner()

    result = await runner._handle_message(_make_event("hello during drain"))

    assert result == "turn-done"
    runner._handle_message_with_agent.assert_awaited_once()
    assert not isinstance(result, str) or _DRAIN_NOTICE_MARKER not in result


@pytest.mark.asyncio
async def test_drain_window_plain_text_runs_with_overlap_successor_pending():
    """Same admission with a live overlap successor pending: the successor
    defers WhatsApp until the predecessor exits, so the predecessor must run
    the turn itself rather than refuse."""
    runner, _adapter = _make_drain_runner()
    runner._overlap_restart_successor_started = True

    result = await runner._handle_message(_make_event("still only you serving me"))

    assert result == "turn-done"
    runner._handle_message_with_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_window_busy_follow_up_joins_the_running_turn():
    """A follow-up on a session whose turn is still running keeps the normal
    busy semantics (queue mode → the FIFO) instead of the drain refusal."""
    runner, adapter = _make_drain_runner()
    key = _mark_busy(runner)
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("and one more thing"))

    assert result is None
    assert key in adapter._pending_messages
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_hub_adapter_keeps_the_handoff_refusal_during_drain():
    """Discord-style hub adapter: admission stays closed on the predecessor —
    its events belong to the successor via handoff, never to this process."""
    runner, adapter = _make_drain_runner(platform=Platform.DISCORD)
    adapter.supports_overlap_restart = True
    # No running turn here and no handoff_event callable → the hub path is
    # exhausted and the honest refusal is the answer.
    adapter.admits_owned_work_while_draining = lambda _source=None: False

    result = await runner._handle_message(_make_event("hand me off", platform=Platform.DISCORD))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_shutdown_still_refuses_non_handoff_events():
    """Once stop() began (``_running`` cleared, adapters disconnecting) the
    refusal stands — a dying process must not start new turns."""
    runner, _adapter = _make_drain_runner()
    runner._running = False

    result = await runner._handle_message(_make_event("too late now"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_task_pending_still_refuses_non_handoff_events():
    """``_running`` alone is not the terminal marker: once ``stop()`` is
    tasked (``_stop_task`` set) adapters are disconnecting even before the
    flag flips — the refusal stands."""
    runner, _adapter = _make_drain_runner()
    runner._stop_task = MagicMock()

    result = await runner._handle_message(_make_event("too late now"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_quiesce_still_refuses_non_handoff_events():
    """An operator quiesce (``_external_drain_active``) drives active work to
    zero before a stop; admitting new local turns would break that contract,
    so even a resolvable non-handoff source is refused."""
    runner, _adapter = _make_drain_runner()
    runner._external_drain_active = True

    result = await runner._handle_message(_make_event("quiescing"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_pending_follow_up_survives_the_turn_drain():
    """A queued follow-up on a non-handoff adapter is kept for the next turn:
    no successor can redeliver it, so discarding would lose the message."""
    runner, adapter = _make_drain_runner()
    source = make_restart_source(platform=Platform.WHATSAPP)
    key = build_session_key(source)
    adapter._pending_messages[key] = _make_event("follow up")

    pending_event, pending = await runner._run_agent_drain_pending(
        {"interrupted": False}, adapter, source, key
    )

    assert pending_event is not None
    assert pending == "follow up"


@pytest.mark.asyncio
async def test_hub_and_unresolvable_pending_follow_ups_are_still_discarded():
    """Hub transports redeliver through the successor, and an unresolvable
    source has no proven transport owner: both keep the legacy discard."""
    for platform, hub in ((Platform.DISCORD, True), (Platform.WHATSAPP, False)):
        runner, adapter = _make_drain_runner(platform=platform)
        source = make_restart_source(platform=platform)
        key = build_session_key(source)
        adapter._pending_messages[key] = _make_event("follow up", platform=platform)
        if hub:
            adapter.supports_overlap_restart = True
        else:
            runner.adapters = {}

        pending_event, pending = await runner._run_agent_drain_pending(
            {"interrupted": False}, adapter, source, key
        )

        assert pending_event is None and pending is None, (platform, hub)


@pytest.mark.asyncio
async def test_inflight_local_admission_counts_as_active_work_until_done():
    """The restart's destructive boundary must not fire between admission and
    the session claim: while the admitted event is anywhere in the pipeline
    the authoritative active-work count stays above zero even with no agent
    registered yet, and returns to zero once handling completes."""
    runner, _adapter = _make_drain_runner()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_pipeline(event):
        entered.set()
        await release.wait()
        return "turn-done"

    runner._handle_message_pipeline = _blocked_pipeline
    task = asyncio.create_task(runner._handle_message(_make_event("hold the drain open")))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert runner._running_agent_count() == 0  # pre-claim window
        assert runner._authoritative_active_work_count() > 0
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) == "turn-done"
    assert runner._authoritative_active_work_count() == 0
