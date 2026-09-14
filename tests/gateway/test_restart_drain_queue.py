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
import os
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.restart_wind_down import write_resume_allowlist
from gateway.run_drain_queue import (
    claimed_drain_queue_path,
    deserialize_drain_event,
    drain_queue_path,
    queue_drain_refused_message,
    replay_drain_queue,
    replay_drain_queues_for_profiles,
    serialize_drain_event,
)
from gateway.session import SessionEntry
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
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


def _queue_in_draining_process(event: MessageEvent, session_key: str, runner=None):
    """Write one event to the drain snapshot the way the draining gateway does.

    Uses the real idle-path admission (the cold gate's seam) on a throwaway
    runner, so the file this leaves behind is byte-for-byte what production
    writes. The in-memory FIFO that runner also fills is discarded with it —
    exactly the process bounce. (A caller with multi-platform adapters may
    pass its own ``runner`` — the draining process that owns them all.)
    """
    if runner is None:
        runner, adapter = make_restart_runner()
    else:
        adapter = runner._adapter_for_source(event.source)
    # The production drain-queue posture: a confirmed restart whose busy
    # policy says messages survive it (the idle seam queues only then —
    # same condition the busy path applies).
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
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
async def test_drain_cap_counts_inmemory_and_durable_backlog_together():
    """One ``_BUSY_QUEUE_MAX_PENDING`` budget across both pools: a session
    that already holds an in-memory backlog when the drain starts must not
    get a second full cap's worth of durable appends (the 2× window)."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    runner._BUSY_QUEUE_MAX_PENDING = 2
    source = make_restart_source(chat_id="combined-cap-chat")
    session_key = runner._session_key_for_source(source)

    # One follow-up already parked in the FIFO before the restart request.
    runner._enqueue_fifo(
        session_key,
        _event("pre-drain backlog", source=source, message_id="m-0"),
        adapter,
    )
    # First drain-time message fits the combined budget (1 in memory + 1).
    assert await runner._handle_active_session_busy_message(
        _event("drain one", source=source, message_id="m-1"), session_key
    )
    # The second would make 3 pending against a cap of 2: honest refusal.
    await runner._handle_active_session_busy_message(
        _event("drain two", source=source, message_id="m-2"), session_key
    )

    assert any(REFUSAL_MARKER in msg for msg in adapter.sent[-1:])
    assert [e["event"]["text"] for e in _snapshot_events()] == ["drain one"]
    assert adapter._pending_messages[session_key].text == "pre-drain backlog"


@pytest.mark.asyncio
async def test_full_routing_metadata_and_thread_prospect_survive_the_roundtrip():
    """Routing metadata beyond the security keys (``whatsapp_from_owner``)
    and ``SessionSource.prospective_thread_id`` decide where and how a
    replayed message lands — they persist too. The wire-invisible trust
    flags never do: restoring them from disk would re-grant an
    authorization the new process was never given."""
    runner, _adapter = make_restart_runner()
    source = make_restart_source(
        chat_id="meta-chat",
        prospective_thread_id="pm-77",
        auto_thread_created=True,
        auto_thread_initial_name="new thread",
        bot_display_name="Hermes Bot",
        delivered_via_upstream_relay=True,  # must NOT survive the snapshot
        profile_route_rejected=True,  # must NOT survive the snapshot
    )
    session_key = runner._session_key_for_source(source)
    event = _event("metadata please", source=source)
    event.metadata = {
        "whatsapp_from_owner": True,
        "gateway_session_key": "agent:main:whatsapp:dm:meta-chat",
        "slack_team_id": "T123",
        "hermes_plugin_id": "p-1",
    }

    record = serialize_drain_event(session_key, event)
    assert record["event"]["metadata"] == event.metadata
    assert record["source"]["prospective_thread_id"] == "pm-77"
    assert record["source"]["auto_thread_created"] is True
    assert record["source"]["bot_display_name"] == "Hermes Bot"
    assert "delivered_via_upstream_relay" not in record["source"]
    assert "profile_route_rejected" not in record["source"]

    _key, rebuilt = deserialize_drain_event(record)
    assert rebuilt.metadata == event.metadata
    assert rebuilt.source.prospective_thread_id == "pm-77"
    assert rebuilt.source.auto_thread_created is True
    assert rebuilt.source.auto_thread_initial_name == "new thread"
    assert rebuilt.source.bot_display_name == "Hermes Bot"
    assert rebuilt.source.delivered_via_upstream_relay is False
    assert rebuilt.source.profile_route_rejected is False


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_quarantined_not_overwritten():
    """The unreadable bytes still hold the only copy of what the previous
    process queued — the next append must move them aside, not pave over
    them."""
    drain_queue_path().parent.mkdir(parents=True, exist_ok=True)
    drain_queue_path().write_text("{corrupt beyond repair", encoding="utf-8")
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    source = make_restart_source(chat_id="quarantine-chat")
    session_key = runner._session_key_for_source(source)

    ack = queue_drain_refused_message(
        runner, _event("queued after the corruption", source=source), session_key
    )

    assert ack is not None and QUEUED_MARKER in ack
    siblings = list(drain_queue_path().parent.glob("drain_message_queue.json.bad-*"))
    assert len(siblings) == 1
    assert siblings[0].read_text(encoding="utf-8") == "{corrupt beyond repair"
    assert [e["event"]["text"] for e in _snapshot_events()] == [
        "queued after the corruption"
    ]


@pytest.mark.asyncio
async def test_dispatch_failure_releases_the_preclaimed_slot(monkeypatch, caplog):
    """If the replay turn's task cannot even be created, the sentinel
    pre-claim must be released — otherwise the session reads as running
    until the process dies."""
    source = make_restart_source(chat_id="dispatch-fail-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("never dispatched", source=source), session_key
    )
    adapter.handle_message = AsyncMock()

    def _no_task(coro):
        coro.close()
        raise RuntimeError("event loop refused the task")

    monkeypatch.setattr(asyncio, "create_task", _no_task)
    with caplog.at_level("WARNING", logger="gateway.run_drain_queue"):
        assert replay_drain_queue(runner) == 0

    assert any(
        "dispatch failed" in record.message.lower() for record in caplog.records
    )
    assert not runner._is_session_running(session_key)
    adapter.handle_message.assert_not_called()
    # The event was injected but never dispatched: its record stays queued in
    # the LIVE snapshot, so the next boot/reconnect retries it instead of
    # stranding the message in a FIFO no turn will drain. Nothing claims the
    # session is busy in the meantime.
    assert [e["event"]["text"] for e in _snapshot_events()] == ["never dispatched"]


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


@pytest.mark.asyncio
async def test_captionless_photo_roundtrips_and_replays():
    """A photo with no caption is media with ``text=""`` — the record must
    carry the empty string, because ``MessageEvent`` takes ``text`` as a
    required positional (a record without the key could never be rebuilt)."""
    source = make_restart_source(chat_id="silent-photo-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    event = _event(
        "",
        source=source,
        message_type=MessageType.PHOTO,
        media=["/tmp/hermes/media/silent.jpg"],
        message_id="m-silent",
    )

    record = serialize_drain_event(session_key, event)
    assert record["event"]["text"] == ""  # the key itself, not a dropped field

    key, rebuilt = deserialize_drain_event(record)
    assert key == session_key
    assert rebuilt.text == ""
    assert rebuilt.message_type == MessageType.PHOTO
    assert rebuilt.media_urls == ["/tmp/hermes/media/silent.jpg"]

    # End to end: the draining process queues it, the fresh one replays it.
    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    _queue_in_draining_process(event, session_key)
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.message_type == MessageType.PHOTO
    assert replayed.text == ""
    assert replayed.media_urls == ["/tmp/hermes/media/silent.jpg"]


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
async def test_cooperative_session_the_scheduler_would_skip_still_gets_its_turn():
    """Allowlist presence alone must not leave the message unconsumed: the
    allowlist names the session, but the scheduler refuses suspended
    entries — so replay owns the turn."""
    source = make_restart_source(chat_id="suspended-coop-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("queued while parked", source=source), session_key
    )
    assert write_resume_allowlist([session_key])
    entry = _resume_entry(source, session_key)
    entry.suspended = True  # the scheduler's candidate filter skips this
    runner.session_store._entries = {session_key: entry}
    adapter.handle_message = AsyncMock()

    assert replay_drain_queue(runner) == 1
    await _settle(runner)

    assert adapter.handle_message.await_count == 1
    assert adapter.handle_message.await_args.args[0].text == "queued while parked"
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_cooperative_session_with_stale_marker_still_gets_its_turn(
    monkeypatch,
):
    """Same rule for the freshness gate: a resume marker older than the
    auto-continue window makes the scheduler skip the session, so the drain
    queue — not a resume that will never come — answers the user."""
    from datetime import timedelta

    monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
    source = make_restart_source(chat_id="stale-coop-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("queued before the long pause", source=source), session_key
    )
    assert write_resume_allowlist([session_key])
    entry = _resume_entry(source, session_key)
    entry.last_resume_marked_at = datetime.now() - timedelta(hours=6)
    runner.session_store._entries = {session_key: entry}
    adapter.handle_message = AsyncMock()

    assert replay_drain_queue(runner) == 1
    await _settle(runner)

    assert adapter.handle_message.await_count == 1
    assert (
        adapter.handle_message.await_args.args[0].text == "queued before the long pause"
    )
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_missing_snapshot_is_a_silent_no_op():
    runner, adapter = make_restart_runner()
    adapter.handle_message = AsyncMock()

    assert replay_drain_queue(runner) == 0

    adapter.handle_message.assert_not_called()
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_missing_adapter_retains_records_until_the_platform_returns():
    """A platform that is down at boot must not cost its users their queued
    messages: the record survives in the live snapshot and replays once the
    adapter exists (the reconnect watcher retries scoped to the platform)."""
    source = make_restart_source(chat_id="offline-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("queued while platform down", source=source, message_id="m-off"), session_key
    )

    offline, _offline_adapter = make_restart_runner()
    offline.adapters = {}  # the platform never came up this boot
    assert replay_drain_queue(offline) == 0

    # Retained, not dropped: the record is back in the LIVE snapshot and the
    # claim is closed (at-most-once window ended).
    events = _snapshot_events()
    assert [e["event"]["text"] for e in events] == ["queued while platform down"]
    assert not claimed_drain_queue_path().exists()

    # The platform returns (next boot or reconnect): the retained record
    # finally replays, and the snapshot is gone for real.
    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)
    assert (
        fresh_adapter.handle_message.await_args.args[0].text
        == "queued while platform down"
    )
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_reconnect_scoped_retry_replays_only_that_platform():
    """``replay_drain_queue(platform=...)`` is the reconnect retry: it takes
    its platform's records and leaves every other platform's untouched."""
    from gateway.config import Platform, PlatformConfig

    telegram_src = make_restart_source(chat_id="tg-chat")
    discord_src = make_restart_source(
        chat_id="dc-chat", platform=Platform.DISCORD, thread_id="dc-thread"
    )
    runner, telegram_adapter = make_restart_runner()
    runner.config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True, token="***")
    discord_adapter = type(telegram_adapter)(Platform.DISCORD)
    discord_adapter.set_message_handler(AsyncMock(return_value=None))
    runner.adapters[Platform.DISCORD] = discord_adapter
    tg_key = runner._session_key_for_source(telegram_src)
    dc_key = runner._session_key_for_source(discord_src)
    _queue_in_draining_process(_event("telegram one", source=telegram_src), tg_key, runner)
    _queue_in_draining_process(
        _event("discord one", source=discord_src, message_id="m-dc"), dc_key, runner
    )

    telegram_adapter.handle_message = AsyncMock()
    discord_adapter.handle_message = AsyncMock()
    # Discord reconnects first: only its record replays; Telegram's waits.
    assert replay_drain_queue(runner, platform=Platform.DISCORD) == 1
    await _settle(runner)
    discord_adapter.handle_message.assert_awaited_once()
    assert discord_adapter.handle_message.await_args.args[0].text == "discord one"
    telegram_adapter.handle_message.assert_not_called()
    assert [e["event"]["text"] for e in _snapshot_events()] == ["telegram one"]

    # Telegram comes back: the retained record finally replays.
    assert replay_drain_queue(runner, platform=Platform.TELEGRAM) == 1
    await _settle(runner)
    assert telegram_adapter.handle_message.await_args.args[0].text == "telegram one"
    assert not drain_queue_path().exists()


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
async def test_one_corrupt_record_does_not_discard_the_good_ones(caplog):
    """A single torn row costs only itself: the good records in the same
    snapshot still replay (the whole-file drop used to discard them all)."""
    source = make_restart_source(chat_id="mixed-snapshot-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    good = serialize_drain_event(
        session_key, _event("the good one", source=source, message_id="m-ok")
    )
    # The corrupt row is hand-written on purpose: production never writes
    # one, so only a test can put a torn record beside a real one.
    drain_queue_path().parent.mkdir(parents=True, exist_ok=True)
    drain_queue_path().write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {"session_key": "", "source": "not-an-object", "event": None},
                    good,
                ],
            }
        ),
        encoding="utf-8",
    )
    adapter.handle_message = AsyncMock()

    with caplog.at_level("WARNING", logger="gateway.run_drain_queue"):
        assert replay_drain_queue(runner) == 1
    await _settle(runner)

    replayed = adapter.handle_message.await_args.args[0]
    assert replayed.text == "the good one"
    assert replayed.message_id == "m-ok"
    assert any(
        "record unreadable" in record.message.lower() for record in caplog.records
    )
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_crash_after_claim_before_dispatch_loses_nothing():
    """Crash mid-replay, the exact window the claim exists for: the gateway
    died after the atomic claim but before any event was dispatched. Those
    messages were ACKNOWLEDGED ("queued for the next turn") — the next boot
    resumes the claim and delivers them; deleting it would lose every one."""
    source = make_restart_source(chat_id="crash-resume-chat")
    fresh, fresh_adapter = make_restart_runner()
    session_key = fresh._session_key_for_source(source)
    _queue_in_draining_process(
        _event("acknowledged before the crash", source=source, message_id="m-1"),
        session_key,
    )
    _queue_in_draining_process(
        _event("also acknowledged", source=source, message_id="m-2"), session_key
    )
    # The replay claimed the snapshot (atomic rename)… and the process died
    # before dispatching anything.
    os.replace(drain_queue_path(), claimed_drain_queue_path())

    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    # The head event runs the turn; the tail parks for its post-turn drain.
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.text == "acknowledged before the crash"
    state = fresh._peek_session_state(session_key)
    assert [e.text for e in state.conversation.queued_events] == ["also acknowledged"]
    assert not claimed_drain_queue_path().exists()
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_leftover_claim_plus_later_drain_snapshot_both_replay():
    """A crashed replay's claim can coexist with a LIVE snapshot written by a
    later drain window. The claim is resumed AND the never-claimed live
    records replay in the same pass — neither generation is dropped."""
    source = make_restart_source(chat_id="fold-chat")
    fresh, fresh_adapter = make_restart_runner()
    session_key = fresh._session_key_for_source(source)
    # Process 1 queued a message, claimed it for replay, and crashed.
    _queue_in_draining_process(
        _event("claimed by the crashed replay", source=source, message_id="m-1"),
        session_key,
    )
    os.replace(drain_queue_path(), claimed_drain_queue_path())
    # Process 2 later ran its own drain window and queued a new message.
    _queue_in_draining_process(
        _event("queued after the crash", source=source, message_id="m-2"), session_key
    )

    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    # Claimed records queued first, so they run the turn; the later live
    # record parks as the FIFO tail.
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.text == "claimed by the crashed replay"
    state = fresh._peek_session_state(session_key)
    assert [e.text for e in state.conversation.queued_events] == ["queued after the crash"]
    assert not claimed_drain_queue_path().exists()
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_dispatched_records_leave_the_claim_ledger_retained_ones_stay():
    """Per-record dispatch state: a session whose turn was dispatched is
    rewritten OUT of the claim ledger, while a retained session's records
    survive to the live snapshot — a crash at this point re-replays only the
    retained session, never the delivered one."""
    live_src = make_restart_source(chat_id="ledger-live-chat")
    offline_src = make_restart_source(chat_id="ledger-offline-chat")
    runner, adapter = make_restart_runner()
    live_key = runner._session_key_for_source(live_src)
    offline_key = runner._session_key_for_source(offline_src)
    _queue_in_draining_process(
        _event("will be dispatched", source=live_src), live_key, runner
    )
    _queue_in_draining_process(
        _event("will be retained", source=offline_src, message_id="m-off"),
        offline_key,
        runner,
    )

    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    real_adapter_for = fresh._adapter_for_source

    def _adapter_except_offline(source):
        return None if source.chat_id == "ledger-offline-chat" else real_adapter_for(source)

    fresh._adapter_for_source = _adapter_except_offline

    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    fresh_adapter.handle_message.assert_awaited_once()
    assert fresh_adapter.handle_message.await_args.args[0].text == "will be dispatched"
    # The ledger's leftovers are exactly the retained session's records.
    assert [e["session_key"] for e in _snapshot_events()] == [offline_key]
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_write_back_failure_keeps_the_claim_as_the_sole_copy(
    monkeypatch, caplog
):
    """When the retained retry-write fails (disk exhaustion, an interrupted
    atomic write), the claim file is the ONLY remaining copy of those
    messages — clearing it would delete them. It stays, and the next boot
    resumes it once storage recovers."""
    import gateway.run_drain_queue as drain_module

    source = make_restart_source(chat_id="writeback-fail-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("waiting for the disk", source=source), session_key
    )

    offline, _offline_adapter = make_restart_runner()
    offline.adapters = {}  # adapter down → the records take the write-back path

    real_write = drain_module.atomic_json_write
    disk_full = {"yes": True}

    def _maybe_no_disk(*args, **kwargs):
        if disk_full["yes"]:
            raise OSError(28, "No space left on device")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(drain_module, "atomic_json_write", _maybe_no_disk)
    with caplog.at_level("ERROR", logger="gateway.run_drain_queue"):
        assert replay_drain_queue(offline) == 0

    assert any("write-back failed" in r.message.lower() for r in caplog.records)
    # The claim survives as the sole copy; no live snapshot was created.
    assert claimed_drain_queue_path().exists()
    assert not drain_queue_path().exists()

    # Storage recovers: the next boot resumes the claim and delivers.
    disk_full["yes"] = False
    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)
    assert (
        fresh_adapter.handle_message.await_args.args[0].text == "waiting for the disk"
    )
    assert not claimed_drain_queue_path().exists()
    assert not drain_queue_path().exists()


@pytest.mark.asyncio
async def test_drain_cap_counts_the_mirrored_backlog_once_not_twice():
    """Once drain queueing starts, every accepted event is mirrored in the
    in-memory FIFO AND the durable snapshot — the cap counts the UNION, so
    with an empty pre-drain backlog the Nth message is still accepted and
    only the N+1th is refused (the sum counted every drain-window event
    twice and refused the message that hit cap/2)."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    runner._BUSY_QUEUE_MAX_PENDING = 2
    source = make_restart_source(chat_id="union-cap-chat")
    session_key = runner._session_key_for_source(source)

    assert await runner._handle_active_session_busy_message(
        _event("one", source=source, message_id="m-1"), session_key
    )
    assert await runner._handle_active_session_busy_message(
        _event("two", source=source, message_id="m-2"), session_key
    )
    # The third is the first over the cap (union depth 2 == cap): refused.
    await runner._handle_active_session_busy_message(
        _event("three", source=source, message_id="m-3"), session_key
    )

    assert any(REFUSAL_MARKER in msg for msg in adapter.sent[-1:])
    # Both mirrors hold exactly the two accepted events.
    assert len(_snapshot_events()) == 2
    assert [e["event"]["text"] for e in _snapshot_events()] == ["one", "two"]
    assert session_key in adapter._pending_messages


@pytest.mark.asyncio
async def test_channel_prompt_and_context_survive_the_roundtrip():
    """Channel-scoped instructions — the per-channel system prompt and the
    history backfilled under require_mention — must reach the replayed turn;
    the serialization whitelist used to drop them, so a drain silently
    stripped the channel's instructions from the queued message."""
    source = make_restart_source(chat_id="channel-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    event = _event("channel instructions please", source=source)
    event.channel_prompt = "You are the #medicina triage assistant; keep answers clinical."
    event.channel_context = "earlier messages backfilled under require_mention"

    record = serialize_drain_event(session_key, event)
    assert record["event"]["channel_prompt"] == event.channel_prompt
    assert record["event"]["channel_context"] == event.channel_context

    _key, rebuilt = deserialize_drain_event(record)
    assert rebuilt.channel_prompt == event.channel_prompt
    assert rebuilt.channel_context == event.channel_context

    # End to end: the replayed turn sees the channel fields.
    _queue_in_draining_process(event, session_key)
    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.channel_prompt == event.channel_prompt
    assert replayed.channel_context == event.channel_context


@pytest.mark.asyncio
async def test_multiplex_gateway_replays_every_served_profiles_queue():
    """Under ``multiplex_profiles`` a secondary profile's handler queues
    under ITS home (it runs inside the profile scope), so startup must claim
    every served profile's file — a single ambient replay would leave the
    secondary queues unclaimed until some other gateway happens to boot."""
    launch_src = make_restart_source(chat_id="launch-chat")
    profile_src = make_restart_source(chat_id="profile-chat")
    runner, adapter = make_restart_runner()
    launch_key = runner._session_key_for_source(launch_src)
    profile_key = runner._session_key_for_source(profile_src)

    # The launch (default) home queues one message, ambient scope.
    _queue_in_draining_process(
        _event("launch home message", source=launch_src, message_id="m-launch"),
        launch_key,
    )
    # A secondary profile's handler queues under its own home scope — the
    # same way its inbound turns run.
    profile_home = get_hermes_home() / "profiles" / "medicina"
    token = set_hermes_home_override(str(profile_home))
    try:
        _queue_in_draining_process(
            _event("profile home message", source=profile_src, message_id="m-profile"),
            profile_key,
            runner,
        )
    finally:
        reset_hermes_home_override(token)
    assert drain_queue_path(profile_home).exists()

    fresh, fresh_adapter = make_restart_runner()
    fresh.config.multiplex_profiles = True
    fresh_adapter.handle_message = AsyncMock()

    assert replay_drain_queues_for_profiles(fresh) == 2
    await _settle(fresh)

    delivered = sorted(
        call.args[0].text for call in fresh_adapter.handle_message.await_args_list
    )
    assert delivered == ["launch home message", "profile home message"]
    # Both homes' queues are fully consumed.
    assert not drain_queue_path().exists()
    assert not drain_queue_path(profile_home).exists()
    assert not claimed_drain_queue_path(profile_home).exists()


@pytest.mark.asyncio
async def test_single_profile_gateway_leaves_other_homes_queues_alone():
    """Control: without multiplex the gateway serves one home, so a queue
    sitting under another profile's home waits there until a gateway that
    serves that profile boots — it is not this gateway's to claim."""
    launch_src = make_restart_source(chat_id="solo-launch-chat")
    profile_src = make_restart_source(chat_id="solo-profile-chat")
    runner, adapter = make_restart_runner()
    launch_key = runner._session_key_for_source(launch_src)
    profile_key = runner._session_key_for_source(profile_src)
    _queue_in_draining_process(
        _event("solo launch message", source=launch_src), launch_key
    )
    profile_home = get_hermes_home() / "profiles" / "medicina"
    token = set_hermes_home_override(str(profile_home))
    try:
        _queue_in_draining_process(
            _event("solo profile message", source=profile_src), profile_key, runner
        )
    finally:
        reset_hermes_home_override(token)

    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()

    assert replay_drain_queues_for_profiles(fresh) == 1
    await _settle(fresh)

    assert (
        fresh_adapter.handle_message.await_args.args[0].text == "solo launch message"
    )
    assert not drain_queue_path().exists()
    # The profile home's queue is untouched, waiting for its own gateway.
    assert drain_queue_path(profile_home).exists()
