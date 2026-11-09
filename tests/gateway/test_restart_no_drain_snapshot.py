"""Restart must not write or replay a durable file of other chats' work.

User contract (Parsa, 2026-11-09): the predecessor keeps running its current
turns; when a turn ends, the hub delivers the NEXT user message to the
successor. The only session the successor actively continues is the one that
called the ``restart()`` tool (and the idle ``/restart`` requester's comeback
note). A drain/restart therefore never snapshots other sessions' in-flight or
drain-window messages to ``drain_message_queue.json`` /
``drain_message_queue.replaying.json``, and startup never replays those files
into model turns — a pre-existing snapshot is discarded with one warning.
"""

import asyncio
import json
import logging
from datetime import datetime

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    RestartTestAdapter,
    make_restart_runner,
    make_restart_source,
)
from tests.gateway.test_restart_drain_gate import (
    _DRAIN_NOTICE_MARKER,
    _make_event,
    _make_gate_runner,
)


def _drain_paths():
    from gateway.run_drain_queue import claimed_drain_queue_path, drain_queue_path

    return drain_queue_path(), claimed_drain_queue_path()


def _write_snapshot(path, text="stale in-flight work"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {
                        "session_key": "agent:main:discord:dm:other-chat",
                        "queued_at": 0,
                        "source": {"platform": "discord", "chat_id": "other-chat",
                                   "chat_type": "dm", "user_id": "u9"},
                        "event": {"text": text, "message_type": "text"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


# ── 1. a drain/restart never writes the durable queue ───────────────────────


@pytest.mark.asyncio
async def test_idle_plain_text_during_restart_drain_is_refused_not_snapshotted():
    """Cold-path plain text during a confirmed restart drain gets the honest
    refusal notice — never a durable queue of the message."""
    live, claimed = _drain_paths()

    runner, _mock_adapter = _make_gate_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    result = await runner._handle_message(_make_event("one more thing…"))

    assert isinstance(result, str) and _DRAIN_NOTICE_MARKER in result
    assert not live.exists() and not claimed.exists()


@pytest.mark.asyncio
async def test_busy_follow_up_during_restart_drain_is_refused_not_snapshotted():
    """A busy session's follow-up during the drain window is refused honestly;
    the hub hands NEW messages to the successor instead."""
    live, claimed = _drain_paths()

    runner, mock_adapter = _make_gate_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    key = runner._session_key_for_source(make_restart_source())
    runner._is_session_running = lambda _key: True
    runner._running_agents[key] = object()

    result = await runner._handle_message(_make_event("and another thing"))

    assert result is not None
    assert not live.exists() and not claimed.exists()


@pytest.mark.asyncio
async def test_startup_restore_queueing_stays_process_local():
    """An inbound message during startup restore parks in the in-memory queue
    only — no durable snapshot write."""
    live, _claimed = _drain_paths()

    runner, _adapter = make_restart_runner()
    event = _make_event("arrived while booting")
    runner._queue_startup_restore_event(event)

    assert not live.exists()
    assert list(getattr(runner, "_startup_restore_queue", [])) == [event]


# ── 2. boot discards a pre-existing snapshot without injecting a turn ───────


@pytest.mark.asyncio
async def test_boot_discards_preexisting_drain_snapshots_with_one_warning(caplog):
    from gateway.run_drain_queue import discard_drain_queue_snapshots_for_profiles

    live, claimed = _drain_paths()
    _write_snapshot(live)
    _write_snapshot(claimed)

    runner, adapter = make_restart_runner()
    adapter.handle_message = type(adapter).handle_message  # never called below

    with caplog.at_level(logging.WARNING, logger="gateway.run_drain_queue"):
        discard_drain_queue_snapshots_for_profiles(runner)

    assert not live.exists() and not claimed.exists()
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "drain" in r.getMessage().lower()
    ]
    assert warnings, "discarding a legacy drain snapshot must warn once"
    # No turn was injected for the discarded records.
    assert runner._running_agents == {}


# ── 3. no resume_pending auto-resume solely because a restart happened ─────


def _resume_entry(source, reason):
    return SessionEntry(
        session_key=f"agent:main:discord:dm:{source.chat_id}",
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type="dm",
        resume_pending=True,
        resume_reason=reason,
        last_resume_marked_at=datetime.now(),
    )


@pytest.mark.asyncio
async def test_restart_timeout_session_is_not_auto_resumed_at_boot():
    """A session marked restart_timeout (drain-timeout interrupt) keeps its
    flag for the next user message, but the successor's boot scheduler never
    synthesizes a continuation turn for it."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="interrupted-chat")
    entry = _resume_entry(source, "restart_timeout")
    runner.session_store._entries = {entry.session_key: entry}
    adapter.handle_message = type(adapter).handle_message  # sentinel: must not run

    scheduled = runner._schedule_resume_pending_sessions()

    assert scheduled == 0
    assert entry.resume_pending is True  # next user message still auto-resumes


@pytest.mark.asyncio
async def test_restart_requester_session_is_still_auto_resumed_at_boot():
    """The requester comeback reason keeps its boot auto-resume (the idle
    /restart ask-what-next note and the restart() tool continuation)."""
    from gateway.run_resume_reasons import RESTART_REQUESTER_RESUME_REASON

    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="requester-chat")
    entry = _resume_entry(source, RESTART_REQUESTER_RESUME_REASON)
    runner.session_store._entries = {entry.session_key: entry}
    from unittest.mock import AsyncMock

    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1


# ── 4. hub overlap delivery survives, file-free ────────────────────────────


@pytest.mark.asyncio
async def test_hub_handoff_during_drain_delivers_without_writing_a_snapshot():
    """A NEW user message on a hub transport during drain is handed to the
    successor via handoff_overlap_event — and nothing lands in a drain file."""
    from gateway.config import Platform
    from unittest.mock import AsyncMock

    live, claimed = _drain_paths()

    runner, _mock_adapter = _make_gate_runner()
    adapter = RestartTestAdapter(Platform.DISCORD)
    adapter.supports_overlap_restart = True
    adapter._running = True
    adapter.admits_owned_work_while_draining = lambda _source=None: True
    adapter.handoff_event = AsyncMock(return_value=True)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    event = _make_event("run in the successor now")
    event.source = make_restart_source(platform=Platform.DISCORD)
    event.internal = False

    result = await runner._handle_message(event)

    assert result is None
    adapter.handoff_event.assert_awaited_once()
    assert not live.exists() and not claimed.exists()
