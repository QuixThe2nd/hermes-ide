"""The authoritative drain gate in ``_handle_message`` (#77184 follow-up).

``_draining`` used to be consulted only ~600 lines into the cold path —
AFTER the dispatch of heavy executor commands (/compress, /refine, /review
branches and the /bg background-task spawn in the plain map) and after the
busy-session slash resolver (whose busy_policy="dispatch" commands, /bg and
/btw, start work by design). A message that landed after a confirmed
restart therefore started work the drain had already promised to wait out.

These regressions drive the REAL ``_handle_message`` pipeline with
``_draining`` set exactly as ``request_restart`` sets it (synchronously,
before its first await) and prove the heavy handlers are never entered —
on both the busy-session fast path (the requester turn still running) and
the cold path (the requester turn already ended, drain still waiting on
other work). Allowed commands (restart entry, in-flight-work lifecycle,
read-only visibility) keep dispatching.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

_DRAIN_NOTICE_MARKER = "not accepting new work"


def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        internal=True,
    )


def _session_entry(source):
    from gateway.config import Platform
    from gateway.session import SessionEntry

    return SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_gate_runner():
    """A bare runner that carries a message through the real pipeline.

    The attribute set mirrors the proven ``_make_runner`` harness in
    ``tests/hermes_cli/test_pre_command_hook.py`` — enough of the real
    ``_handle_message`` pipeline to reach command dispatch without any
    platform/agent machinery.
    """
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner, adapter = make_restart_runner()
    # make_restart_runner wires a RestartTestAdapter; the hook harness's
    # MagicMock adapter shape is what the pipeline bits before dispatch
    # expect (pending-message bookkeeping, extract_media, ...).
    mock_adapter = MagicMock()
    mock_adapter.send = AsyncMock()
    mock_adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: mock_adapter}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry(
        make_restart_source()
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
    return runner, mock_adapter


def _mark_busy(runner, *, source=None):
    """Put the event's session into the running-agent fast path."""
    key = build_session_key(source or make_restart_source())
    runner._is_session_running = lambda _key: True
    runner._running_agents[key] = MagicMock()
    return key


# ── the gate predicate itself ────────────────────────────────────────────────


def test_gate_notice_denies_by_default_and_passes_only_allowed_commands():
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True

    notice = runner._slash_drain_gate_notice("compress")
    assert notice is not None and _DRAIN_NOTICE_MARKER in notice
    assert "restarting" in notice  # _status_action_gerund wording

    # Plain text / media (canonical=None) is always denied while draining.
    assert runner._slash_drain_gate_notice(None) is not None

    # An as-yet-unregistered command name is denied — deny-by-default, so a
    # command added tomorrow is gated until it is deliberately allowed.
    assert runner._slash_drain_gate_notice("some-future-command") is not None

    for allowed in ("restart", "stop", "approve", "deny", "pause", "status",
                    "context", "agents", "help", "commands", "version",
                    "whoami", "start"):
        assert runner._slash_drain_gate_notice(allowed) is None, allowed


def test_gate_notice_is_transparent_when_admission_is_open():
    runner, _adapter = make_restart_runner()
    runner._draining = False
    assert runner._slash_drain_gate_notice("compress") is None
    assert runner._slash_drain_gate_notice(None) is None
    assert runner._slash_drain_gate_notice("restart") is None


# ── cold path: the requester turn has ended, the drain still waits ──────────


@pytest.mark.asyncio
async def test_cold_compress_after_confirmation_never_enters_the_handler():
    runner, _adapter = _make_gate_runner()
    runner._draining = True  # exactly what request_restart sets synchronously
    heavy = AsyncMock(return_value="compressed")
    runner._handle_compress_command = heavy

    result = await runner._handle_message(_make_event("/compress here"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    heavy.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_refine_and_review_after_confirmation_never_run():
    runner, _adapter = _make_gate_runner()
    runner._draining = True
    refine = AsyncMock(return_value="refined")
    review = AsyncMock(return_value="reviewed")
    runner._handle_refine_command = refine
    runner._handle_review_command = review

    for text in ("/refine tighten it", "/review"):
        result = await runner._handle_message(_make_event(text))
        assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result, text

    refine.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_background_spawn_after_confirmation_never_runs():
    """The plain map's /bg handler is the cold-path work spawn point."""
    runner, _adapter = _make_gate_runner()
    runner._draining = True
    bg = AsyncMock(return_value="queued")
    runner._handle_background_command = bg

    result = await runner._handle_message(_make_event("/bg compile the docs"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    bg.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_plain_text_after_confirmation_is_queued_durably():
    """Plain text during drain is no longer refused-and-forgotten: it lands
    in the durable drain snapshot AND the in-memory FIFO, and the ack says
    so (the drain-queue tests cover the post-restart replay side)."""
    from gateway.run_drain_queue import drain_queue_path

    runner, mock_adapter = _make_gate_runner()
    runner._draining = True

    result = await runner._handle_message(_make_event("one more thing…"))

    assert isinstance(result, str)
    assert "queued for the next turn after it comes back" in result
    session_key = runner._session_key_for_source(make_restart_source())
    assert session_key in mock_adapter._pending_messages
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert events[0]["event"]["text"] == "one more thing…"
    assert events[0]["session_key"] == session_key


@pytest.mark.asyncio
async def test_cold_disallowed_command_during_drain_is_refused_and_not_persisted():
    """Commands keep the gate's refusal (lifecycle-sensitive during drain) —
    and are never written to the durable queue."""
    from gateway.run_drain_queue import drain_queue_path

    runner, mock_adapter = _make_gate_runner()
    runner._draining = True
    heavy = AsyncMock(return_value="compressed")
    runner._handle_compress_command = heavy

    result = await runner._handle_message(_make_event("/compress secret context"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    heavy.assert_not_awaited()
    assert not drain_queue_path().exists()
    assert not mock_adapter._pending_messages


@pytest.mark.asyncio
async def test_gate_is_transparent_when_not_draining():
    """Control: with admission open the same messages reach their handlers."""
    runner, _adapter = _make_gate_runner()
    assert runner._draining is False
    heavy = AsyncMock(return_value="compressed")
    runner._handle_compress_command = heavy
    bg = AsyncMock(return_value="queued")
    runner._handle_background_command = bg

    assert await runner._handle_message(_make_event("/compress here")) == "compressed"
    assert await runner._handle_message(_make_event("/bg compile the docs")) == "queued"
    heavy.assert_awaited_once()
    bg.assert_awaited_once()


# ── busy path: the requester turn is still running ──────────────────────────


@pytest.mark.asyncio
async def test_busy_bg_after_confirmation_never_dispatches():
    """The race the old ordering lost: requester turn still live, admission
    already closed, a busy_policy="dispatch" command arrives. It must get
    the drain notice, not a background-task spawn."""
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True
    resolver = AsyncMock(return_value="dispatched")
    runner._dispatch_busy_slash_command = resolver

    result = await runner._handle_message(_make_event("/bg compile the docs"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_compress_after_confirmation_never_dispatches():
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True
    resolver = AsyncMock(return_value="dispatched")
    runner._dispatch_busy_slash_command = resolver

    result = await runner._handle_message(_make_event("/compress here"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_plain_text_after_confirmation_gets_the_notice():
    """Non-command input on a busy session is refused by the busy-path
    branch's own drain handling ("not accepting another turn") — that
    pre-existing refusal is the point; the cold gate is the backstop for
    command dispatch, not a duplicate of it."""
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True

    result = await runner._handle_message(_make_event("and another thing"))

    assert isinstance(result, str) and "not accepting another turn" in result


@pytest.mark.asyncio
async def test_busy_plain_text_in_queue_mode_during_drain_is_queued_durably():
    """With the busy path's queue-during-drain policy active, the same
    fast-path answers with the queued ack and persists the event — the
    in-memory-only promise is gone."""
    from gateway.run_drain_queue import drain_queue_path

    runner, mock_adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("queue me please"))

    assert isinstance(result, str)
    assert "queued for the next turn after it comes back" in result
    session_key = runner._session_key_for_source(make_restart_source())
    assert session_key in mock_adapter._pending_messages
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert events[0]["event"]["text"] == "queue me please"


@pytest.mark.asyncio
async def test_busy_dispatch_still_works_when_not_draining():
    """Control: the resolver is untouched while admission is open."""
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    assert runner._draining is False
    resolver = AsyncMock(return_value="dispatched")
    runner._dispatch_busy_slash_command = resolver

    assert await runner._handle_message(_make_event("/bg compile the docs")) == "dispatched"
    resolver.assert_awaited_once()


# ── busy sub-paths that used to bypass the durable drain seam ───────────────


def _sub_path_state(*, started_ts: float, agent) -> None:
    """Session state that steers the busy region into one specific sub-path:
    a fresh ``started_ts`` selects the Telegram follow-up grace window, the
    pending sentinel selects the agent-setup window."""
    from gateway.session_state import SessionState

    state = SessionState()
    state.turn.started_ts = started_ts
    state.turn.agent = agent
    return state


@pytest.mark.asyncio
async def test_telegram_grace_followup_during_drain_is_queued_durably():
    """A text follow-up inside the post-start grace window used to merge
    in-memory and return None BEFORE the durable seam — parked in a queue
    that dies with the process bounce while promising nothing."""
    import time

    from gateway.run_drain_queue import drain_queue_path

    runner, mock_adapter = _make_gate_runner()
    key = _mark_busy(runner)
    runner._peek_session_state = lambda _key: _sub_path_state(
        started_ts=time.time(), agent=object()  # real agent, not the sentinel
    )
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("grace follow-up"))

    assert isinstance(result, str)
    assert "queued for the next turn after it comes back" in result
    assert key in mock_adapter._pending_messages
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert events[0]["event"]["text"] == "grace follow-up"

    # Control: with admission open the same sub-path keeps its in-memory-only
    # shape — None, nothing durably recorded (the branch really was taken).
    runner._draining = False
    runner._restart_requested = False
    drain_queue_path().unlink(missing_ok=True)
    mock_adapter._pending_messages.clear()
    assert await runner._handle_message(_make_event("grace control")) is None
    assert key in mock_adapter._pending_messages
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_pending_sentinel_followup_during_drain_is_queued_durably():
    """Same gap in the agent-setup window: a follow-up while the pending
    sentinel holds the slot used to merge in-memory and return None before
    the durable seam ever ran."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    from gateway.run_drain_queue import drain_queue_path

    runner, mock_adapter = _make_gate_runner()
    key = _mark_busy(runner)
    runner._peek_session_state = lambda _key: _sub_path_state(
        started_ts=0.0, agent=_AGENT_PENDING_SENTINEL  # outside the grace window
    )
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("while the agent spins up"))

    assert isinstance(result, str)
    assert "queued for the next turn after it comes back" in result
    assert key in mock_adapter._pending_messages
    events = json.loads(drain_queue_path().read_text(encoding="utf-8"))["events"]
    assert events[0]["event"]["text"] == "while the agent spins up"

    # Control: admission open — the sentinel window keeps merging in-memory
    # (None, no file), exactly as before the drain queue existed.
    runner._draining = False
    runner._restart_requested = False
    drain_queue_path().unlink(missing_ok=True)
    mock_adapter._pending_messages.clear()
    assert await runner._handle_message(_make_event("sentinel control")) is None
    assert key in mock_adapter._pending_messages
    assert not drain_queue_path().exists()


# ── preserved entries: restart request + lifecycle + read-only ──────────────


@pytest.mark.asyncio
async def test_restart_request_entry_survives_the_gate_on_both_paths():
    runner, _adapter = _make_gate_runner()
    runner._draining = True
    restart = AsyncMock(return_value="already restarting")
    runner._handle_restart_command = restart

    assert await runner._handle_message(_make_event("/restart")) == "already restarting"
    restart.assert_awaited_once()

    # Same answer while the requester session is still mid-turn.
    _mark_busy(runner)
    resolver = AsyncMock(return_value="busy-restart")
    runner._dispatch_busy_slash_command = resolver
    assert await runner._handle_message(_make_event("/restart")) == "busy-restart"
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_in_flight_lifecycle_commands_survive_the_gate():
    """/stop (interrupt the running turn) and /approve //deny (its approval
    flow) stay dispatchable while draining — lifecycle of in-flight work."""
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True
    resolver = AsyncMock(return_value="lifecycle-handled")
    runner._dispatch_busy_slash_command = resolver

    for text in ("/stop", "/approve", "/deny"):
        assert await runner._handle_message(_make_event(text)) == "lifecycle-handled", text
    assert resolver.await_count == 3


@pytest.mark.asyncio
async def test_read_only_status_and_context_stay_pre_gate_while_draining():
    """/status and /context are intentionally pre-gate: users always see
    session state, including during a drain."""
    runner, _adapter = _make_gate_runner()
    _mark_busy(runner)
    runner._draining = True
    status = AsyncMock(return_value="status-ok")
    context = AsyncMock(return_value="context-ok")
    runner._handle_status_command = status
    runner._handle_context_command = context

    assert await runner._handle_message(_make_event("/status")) == "status-ok"
    assert await runner._handle_message(_make_event("/context")) == "context-ok"
    status.assert_awaited_once()
    context.assert_awaited_once()
