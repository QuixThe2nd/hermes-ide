"""Durable queueing for messages that arrive while the gateway is draining.

The invariant (see the drain-gate tests for the admission side): a user
message landing in the restart drain window is answered after the restart
completes — never silently dropped (idle path used to refuse and forget)
and never parked only in the adapter's in-memory FIFO, which dies with the
process bounce (the busy path's "queued for the next turn after it comes
back" ack used to be a promise nothing could keep).

These tests drive the production seams:

- drain-time queueing: ``_handle_active_session_busy_message`` (bound real
  method — the ack goes out through the adapter's real send path) and the
  ``queue_drain_refused_message`` idle-path admission the cold gate calls;
- startup replay: ``replay_drain_queue`` on a FRESH runner sharing the same
  ``HERMES_HOME`` (the per-test sandbox the conftest fixture provides), the
  way the post-restart process finds the previous process's snapshot.

The previous process is always simulated through the real write path
(``queue_drain_refused_message`` on a draining runner) — never by hand-
writing the snapshot — so the on-disk shape is exactly what production
writes and exactly what replay parses.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.restart_wind_down import write_resume_allowlist
from gateway.run_drain_queue import (
    claimed_drain_queue_path,
    drain_queue_path,
    queue_drain_refused_message,
    replay_drain_queue,
)
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

QUEUED_MARKER = "queued for the next turn after it comes back"
REFUSAL_MARKER = "not accepting another turn"


def _event(
    text: str = "what needs my attention the most rn?",
    *,
    source=None,
    message_type: MessageType = MessageType.TEXT,
    media=None,
    message_id: str = "m-1",
):
    media = list(media or [])
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source or make_restart_source(),
        message_id=message_id,
        media_urls=media,
        media_types=["photo"] * len(media),
    )


def _snapshot_events() -> list:
    data = json.loads(drain_queue_path().read_text(encoding="utf-8"))
    return data["events"]


def _queue_in_draining_process(event: MessageEvent, session_key: str) -> str:
    """Write one event to the drain snapshot the way the draining gateway does.

    Uses the real idle-path admission (the cold gate's seam) on a throwaway
    runner, so the file this leaves behind is byte-for-byte what production
    writes. The in-memory FIFO that runner also fills is discarded with it —
    exactly the process bounce.
    """
    runner, adapter = make_restart_runner()
    runner._draining = True
    ack = queue_drain_refused_message(runner, event, session_key)
    assert ack is not None and QUEUED_MARKER in ack
    assert session_key in adapter._pending_messages  # live process queued it too
    return ack


def _resume_entry(source, session_key: str) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=session_key,
        session_id="sid-drain",
        created_at=now,
        updated_at=now,
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type or "dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=now,
    )


async def _settle(runner) -> None:
    if runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))


# ── drain-time durability ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_message_during_drain_is_persisted_not_just_in_memory():
    """The busy path promised a queue; the snapshot makes the promise true."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    source = make_restart_source(chat_id="busy-chat", thread_id="t-9")
    session_key = runner._session_key_for_source(source)
    event = _event("status ping please", source=source, message_id="m-42")

    assert await runner._handle_active_session_busy_message(event, session_key) is True

    assert any(QUEUED_MARKER in msg for msg in adapter.sent)
    # Durably recorded with the routing the replay will need…
    events = _snapshot_events()
    assert len(events) == 1
    record = events[0]
    assert record["session_key"] == session_key
    assert record["source"]["platform"] == "telegram"
    assert record["source"]["chat_id"] == "busy-chat"
    assert record["source"]["thread_id"] == "t-9"
    assert record["event"]["message_id"] == "m-42"
    assert record["event"]["text"] == "status ping please"
    assert record["event"]["message_type"] == "text"
    assert record["queued_at"] > 0
    # …and still parked in the in-memory FIFO of the live process.
    assert adapter._pending_messages[session_key] is event


@pytest.mark.asyncio
async def test_busy_interrupt_mode_makes_no_queue_promise():
    """Control: interrupt mode keeps the honest refusal — no ack, no file."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "interrupt"  # the default posture
    source = make_restart_source(chat_id="interrupt-chat")
    session_key = runner._session_key_for_source(source)

    await runner._handle_active_session_busy_message(
        _event(source=source), session_key
    )

    assert any(REFUSAL_MARKER in msg for msg in adapter.sent)
    assert not any(QUEUED_MARKER in msg for msg in adapter.sent)
    assert not drain_queue_path().exists()
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_cap_reached_during_drain_refuses_honestly_without_file_growth():
    """Overflow at ``_BUSY_QUEUE_MAX_PENDING`` drops the message but tells the
    user — the OLD refusal, never a queued ack, and never a longer file."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    runner._BUSY_QUEUE_MAX_PENDING = 1
    source = make_restart_source(chat_id="cap-chat")
    session_key = runner._session_key_for_source(source)

    await runner._handle_active_session_busy_message(
        _event("first fits", source=source, message_id="m-1"), session_key
    )
    assert any(QUEUED_MARKER in msg for msg in adapter.sent)

    await runner._handle_active_session_busy_message(
        _event("second overflows", source=source, message_id="m-2"), session_key
    )

    assert any(REFUSAL_MARKER in msg for msg in adapter.sent[-1:])
    assert len(_snapshot_events()) == 1
    assert _snapshot_events()[0]["event"]["text"] == "first fits"
    # The in-memory FIFO is unchanged too — no phantom queue either side.
    assert session_key in adapter._pending_messages
    assert adapter._pending_messages[session_key].text == "first fits"


@pytest.mark.asyncio
async def test_photo_burst_during_drain_merges_and_survives_the_restart():
    """Album semantics survive the bounce: the burst merges into the head slot
    live, is recorded per-event durably, and replay re-merges it."""
    media_a = "/tmp/hermes/media/a.jpg"
    media_b = "/tmp/hermes/media/b.jpg"
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    source = make_restart_source(chat_id="photo-chat")
    session_key = runner._session_key_for_source(source)

    await runner._handle_active_session_busy_message(
        _event("look at this", source=source, message_type=MessageType.PHOTO,
               media=[media_a], message_id="m-1"),
        session_key,
    )
    await runner._handle_active_session_busy_message(
        _event("and this too", source=source, message_type=MessageType.PHOTO,
               media=[media_b], message_id="m-2"),
        session_key,
    )

    # Live album merge preserved in the FIFO head…
    head = adapter._pending_messages[session_key]
    assert head.media_urls == [media_a, media_b]
    # …while the snapshot keeps each event separately.
    events = _snapshot_events()
    assert [e["event"]["media_urls"] for e in events] == [[media_a], [media_b]]

    # A fresh process replays with the same merge semantics.
    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.message_type == MessageType.PHOTO
    assert replayed.media_urls == [media_a, media_b]
    assert "look at this" in replayed.text and "and this too" in replayed.text


# ── startup replay ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_process_replays_snapshot_and_starts_the_turn():
    """The invariant end to end: message queued during drain, process bounces,
    the new gateway starts a turn whose text is the queued message."""
    source = make_restart_source(chat_id="replay-chat", thread_id="t-4")
    fresh, fresh_adapter = make_restart_runner()
    session_key = fresh._session_key_for_source(source)
    _queue_in_draining_process(
        _event("what needs my attention the most rn?", source=source, message_id="m-1"),
        session_key,
    )
    # A second message for the same chat queued later: the FIFO tail.
    _queue_in_draining_process(
        _event("second thought", source=source, message_id="m-2"), session_key
    )

    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    # The turn started with the HEAD event (arrival order), on the owning
    # adapter, with the routing metadata the user actually used.
    fresh_adapter.handle_message.assert_awaited_once()
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.text == "what needs my attention the most rn?"
    assert replayed.message_id == "m-1"
    assert replayed.source.chat_id == "replay-chat"
    assert replayed.source.thread_id == "t-4"
    # The tail stays parked for that turn's post-turn drain — the /queue shape.
    assert session_key not in fresh_adapter._pending_messages
    state = fresh._peek_session_state(session_key)
    assert [e.text for e in state.conversation.queued_events] == ["second thought"]
    # At-most-once: both files are gone, and a second replay is a no-op.
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()
    assert replay_drain_queue(fresh) == 0
    assert not fresh._is_session_running(session_key)  # no leaked slot claim


@pytest.mark.asyncio
async def test_cooperative_resume_session_enqueues_without_a_second_turn():
    """A session steered into the cooperative wind-down is resumed by the
    scheduler — replay must only enqueue its message, and the combined
    startup sequence delivers exactly ONE turn for it."""
    source = make_restart_source(chat_id="coop-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("queued while winding down", source=source), session_key
    )
    # The wind-down snapshotted this session as actively running.
    assert write_resume_allowlist([session_key])
    # …and its session entry carries the resume marker the scheduler reads.
    runner.session_store._entries = {session_key: _resume_entry(source, session_key)}
    adapter.handle_message = AsyncMock()

    assert replay_drain_queue(runner) == 0  # enqueue-only: no turn from replay
    adapter.handle_message.assert_not_called()
    assert adapter._pending_messages[session_key].text == "queued while winding down"
    assert not drain_queue_path().exists()

    # The real startup then resumes the session: one turn, and the queued
    # text waits for that turn's post-turn drain instead of duplicating it.
    assert runner._schedule_resume_pending_sessions() == 1
    await _settle(runner)
    assert adapter.handle_message.await_count == 1
    assert adapter._pending_messages[session_key].text == "queued while winding down"


@pytest.mark.asyncio
async def test_missing_snapshot_is_a_silent_no_op():
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock()

    assert replay_drain_queue(runner) == 0

    adapter.handle_message.assert_not_called()
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_logged_and_startup_proceeds(caplog):
    drain_queue_path().parent.mkdir(parents=True, exist_ok=True)
    drain_queue_path().write_text("{not json at all", encoding="utf-8")
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock()

    with caplog.at_level("WARNING", logger="gateway.run_drain_queue"):
        assert replay_drain_queue(runner) == 0

    assert any("corrupt" in record.message.lower() for record in caplog.records)
    adapter.handle_message.assert_not_called()
    # The claim is cleaned up: the next boot must not treat the corpse as a
    # crashed replay and skip a fresh snapshot.
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_leftover_claim_from_crashed_replay_is_discarded_not_replayed():
    """At-most-once across crashes: a claim file a crashed replay left behind
    is thrown away — its events may already have been delivered."""
    import os

    source = make_restart_source(chat_id="crash-chat")
    fresh, fresh_adapter = make_restart_runner()
    session_key = fresh._session_key_for_source(source)
    # The crashed process had queued one message…
    _queue_in_draining_process(
        _event("from the crashed replay", source=source), session_key
    )
    # …claimed it for replay (atomic rename)…
    os.replace(drain_queue_path(), claimed_drain_queue_path())
    # …and died. The restart then wrote one NEW message during its own drain.
    _queue_in_draining_process(
        _event("queued after the crash", source=source), session_key
    )

    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    # Only the new live-snapshot event runs — the claimed one is not retried.
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.text == "queued after the crash"
    assert not claimed_drain_queue_path().exists()
    assert not drain_queue_path().exists()
