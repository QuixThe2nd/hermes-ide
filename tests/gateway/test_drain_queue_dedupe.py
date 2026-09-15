"""Dedupe for gateway queue re-delivery: ONE agent turn per inbound message id.

The invariant: no matter how a platform message reaches the gateway — durable
drain-snapshot replay at boot, FIFO overflow promotion after a busy window, or
busy-window rescue staging on the idle path — it must never start more than one
agent turn. Duplicate copies are dropped idempotently (one log line each), and
an event that survives dedupe as a known re-delivery carries a visible marker
so the model can tell a redelivery from a fresh user message instead of
answering each copy.

Observed on this host (2026-09-14): a restart replay re-injected one user
message seven times (~64 duplicate replies in one thread), and a session whose
FIFO overflow had grown ~24-29 copies of ONE message drained one duplicate
turn per promoted copy, re-appending as it went.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_drain_queue import (
    claimed_drain_queue_path,
    drain_queue_path,
    queue_drain_refused_message,
    record_drain_event,
    replay_drain_queue,
    serialize_drain_event,
)
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

QUEUED_MARKER = "queued for the next turn after it comes back"
REDELIVERY_MARKER = "re-delivered"


def _event(
    text: str = "what needs my attention the most rn?",
    *,
    source=None,
    message_id: str = "m-1",
    message_type: MessageType = MessageType.TEXT,
):
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source or make_restart_source(),
        message_id=message_id,
    )


def _snapshot_events() -> list:
    data = json.loads(drain_queue_path().read_text(encoding="utf-8"))
    return data["events"]


def _queue_in_draining_process(event: MessageEvent, session_key: str, runner=None):
    """Queue one event the way the draining gateway does (real write path)."""
    if runner is None:
        runner, adapter = make_restart_runner()
    else:
        adapter = runner._adapter_for_source(event.source)
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    return queue_drain_refused_message(runner, event, session_key)


async def _settle(runner) -> None:
    if runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))


class _StubAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        return SendResult(success=True, message_id="msg-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _text_event(text: str, msg_id: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=MagicMock(chat_id="123", platform=Platform.TELEGRAM, profile=None),
        message_id=msg_id,
    )


def _bare_runner() -> GatewayRunner:
    return GatewayRunner.__new__(GatewayRunner)


# ── (a) drain-snapshot replay ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_snapshot_repeated_copies_of_one_message_id_replay_as_one_turn():
    """The platform re-delivered one message id seven times inside the drain
    window (the boot-replay symptom). The durable snapshot and the in-memory
    FIFO must each hold exactly ONE copy, and the post-restart replay must
    dispatch exactly one turn with no duplicate tail parked behind it."""
    source = make_restart_source(chat_id="dup-replay-chat")
    runner, adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    event = _event("tell me a joke", source=source, message_id="m-dup")

    assert _queue_in_draining_process(event, session_key, runner) is not None
    for _ in range(6):  # same message id re-delivered during the drain window
        ack = _queue_in_draining_process(event, session_key, runner)
        assert ack is not None and QUEUED_MARKER in ack  # honest: it IS queued

    assert [e["event"]["message_id"] for e in _snapshot_events()] == ["m-dup"]
    assert runner._queue_depth(session_key, adapter=adapter) == 1

    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    fresh_adapter.handle_message.assert_awaited_once()
    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.message_id == "m-dup"
    # No parked duplicate tail: each parked copy would run as its own turn via
    # the post-turn drain (the amplification loop).
    state = fresh._peek_session_state(session_key)
    assert [e.message_id for e in state.conversation.queued_events] == []
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()


@pytest.mark.asyncio
async def test_replay_dedupes_snapshot_records_by_message_id():
    """A snapshot that already holds N copies of one id (written by an older
    process, or folded generations) replays as exactly one injected copy —
    dedupe must not depend on the append path alone."""
    source = make_restart_source(chat_id="foreign-snapshot-chat")
    fresh, fresh_adapter = make_restart_runner()
    session_key = fresh._session_key_for_source(source)
    record = serialize_drain_event(session_key, _event("ping me", source=source, message_id="m-f"))
    drain_queue_path().parent.mkdir(parents=True, exist_ok=True)
    drain_queue_path().write_text(
        json.dumps({"version": 1, "events": [record] * 5}, indent=None),
        encoding="utf-8",
    )

    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    fresh_adapter.handle_message.assert_awaited_once()
    state = fresh._peek_session_state(session_key)
    assert [e.message_id for e in state.conversation.queued_events] == []
    assert not drain_queue_path().exists()
    assert not claimed_drain_queue_path().exists()


# ── (b) FIFO overflow promotion ──────────────────────────────────────────────


def test_fifo_overflow_copies_of_one_id_promote_one_turn_and_drop_the_rest():
    """A pre-grown overflow (the ~24-29-copy session) must collapse at the
    promotion seam: one promoted event, the sibling copies dropped, never a
    second turn for the same id."""
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:dup-promote"
    runner._session_state(session_key).conversation.queued_events.extend(
        _text_event("tell me a joke", "m-dup") for _ in range(4)
    )

    promoted = runner._promote_queued_event(session_key, adapter, None)

    assert promoted is not None and promoted.message_id == "m-dup"
    assert promoted.redelivered is True  # the marker rides the surviving copy
    assert runner._session_state(session_key).conversation.queued_events == []
    # Nothing left to promote: no second turn for this message id.
    assert runner._promote_queued_event(session_key, adapter, None) is None


def test_enqueue_fifo_drops_redelivered_copy_and_marks_the_queued_one():
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:dup-enqueue"
    first = _text_event("original copy", "m-dup")
    runner._enqueue_fifo(session_key, first, adapter)

    runner._enqueue_fifo(session_key, _text_event("redelivered copy", "m-dup"), adapter)

    assert runner._queue_depth(session_key, adapter=adapter) == 1
    survivor = adapter._pending_messages[session_key]
    assert survivor is first  # FIFO order: the first copy keeps its turn
    assert survivor.redelivered is True


@pytest.mark.asyncio
async def test_busy_queue_during_drain_drops_second_copy_of_same_message_id():
    """The busy drain path (durable snapshot + FIFO mirror) admits a
    re-delivered id exactly once — both pools stay at one copy, and the ack
    stays honest because the message IS queued (its first copy)."""
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    source = make_restart_source(chat_id="busy-dup-chat")
    session_key = runner._session_key_for_source(source)
    event = _event("status ping please", source=source, message_id="m-busy-dup")

    assert await runner._handle_active_session_busy_message(event, session_key) is True
    assert await runner._handle_active_session_busy_message(event, session_key) is True

    assert [e["event"]["message_id"] for e in _snapshot_events()] == ["m-busy-dup"]
    assert runner._queue_depth(session_key, adapter=adapter) == 1
    assert adapter._pending_messages[session_key].redelivered is True
    # Both copies were acked honestly — the queue holds the first copy.
    assert sum(QUEUED_MARKER in msg for msg in adapter.sent) == 2


# ── (c) busy-window rescue staging ───────────────────────────────────────────


def test_rescue_of_duplicate_chain_stages_no_second_copy():
    """Rescue on an overflow holding copies of one id: the oldest runs as this
    turn, and neither the staged slot nor the remaining overflow may hold
    another copy of the same id (each would run as its own turn)."""
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:dup-rescue"
    runner._session_state(session_key).conversation.queued_events.extend(
        _text_event("orphan copy", "m-dup") for _ in range(3)
    )

    rescued = runner._rescue_orphaned_overflow(session_key, adapter)

    assert rescued is not None and rescued.message_id == "m-dup"
    assert rescued.redelivered is True
    staged = adapter._pending_messages.get(session_key)
    assert staged is None or staged.message_id != "m-dup"
    assert runner._session_state(session_key).conversation.queued_events == []


def test_rescue_park_incoming_redelivery_of_rescued_id_appends_nothing():
    """The idle-path rescue seam: an orphan with id X runs as THIS turn, and
    the platform re-delivers id X as the incoming event. Parking the incoming
    copy behind the chain would queue a second turn for one platform message —
    it must be dropped and the running (rescued) copy marked instead."""
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:dup-park"
    runner._session_state(session_key).conversation.queued_events.append(
        _text_event("orphan copy", "m-dup")
    )

    rescued = runner._rescue_orphaned_overflow(session_key, adapter)
    assert rescued is not None and rescued.message_id == "m-dup"
    # Exactly what the idle-path call site does with the incoming event.
    runner._rescue_park_incoming_event(
        session_key, _text_event("redelivered copy", "m-dup"), adapter, rescued
    )

    assert adapter._pending_messages.get(session_key) is None
    assert runner._session_state(session_key).conversation.queued_events == []
    assert rescued.redelivered is True


# ── (d) the model-visible re-delivery marker ─────────────────────────────────


@pytest.mark.asyncio
async def test_replayed_event_carries_the_redelivery_marker_to_the_model():
    """A drain-queue replay is a re-injection, not a fresh user message: the
    replayed event must carry the payload marker, and the marker must reach
    the model-facing turn text through the shared inbound preparation seam."""
    source = make_restart_source(chat_id="marker-replay-chat")
    runner, _adapter = make_restart_runner()
    session_key = runner._session_key_for_source(source)
    _queue_in_draining_process(
        _event("queued before the bounce", source=source, message_id="m-mark"),
        session_key,
        runner,
    )

    fresh, fresh_adapter = make_restart_runner()
    fresh_adapter.handle_message = AsyncMock()
    assert replay_drain_queue(fresh) == 1
    await _settle(fresh)

    replayed = fresh_adapter.handle_message.await_args.args[0]
    assert replayed.message_id == "m-mark"
    assert replayed.redelivered is True

    # The marker is visible to the model, not just gateway bookkeeping.
    turn_text = await fresh._prepare_inbound_message_text(
        event=replayed, source=replayed.source, history=[], session_key=session_key
    )
    assert turn_text is not None
    assert REDELIVERY_MARKER in turn_text.lower()
    assert "queued before the bounce" in turn_text


def test_marker_reaches_model_text_for_marked_queued_event():
    """Any event flagged as a redelivery (e.g. the queued copy a duplicate was
    dropped against) renders the marker on its model-facing turn text."""
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="marker-queue-chat")
    event = _event("still the same question", source=source, message_id="m-mark-2")
    event.redelivered = True

    turn_text = asyncio.get_event_loop().run_until_complete(
        runner._prepare_inbound_message_text(
            event=event, source=source, history=[], session_key="k"
        )
    )
    assert turn_text is not None
    assert REDELIVERY_MARKER in turn_text.lower()
    # Unmarked events stay clean — a fresh message must not carry the note.
    fresh_text = asyncio.get_event_loop().run_until_complete(
        runner._prepare_inbound_message_text(
            event=_event("brand new", source=source, message_id="m-fresh"),
            source=source,
            history=[],
            session_key="k",
        )
    )
    assert fresh_text is not None
    assert REDELIVERY_MARKER not in fresh_text.lower()


# ── (e) internal synthetic events exempt from message-id dedupe ──────────────


def _internal_text_event(text: str, msg_id: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=MagicMock(chat_id="123", platform=Platform.TELEGRAM, profile=None),
        message_id=msg_id,
        internal=True,
    )


def test_enqueue_fifo_keeps_distinct_internal_events_sharing_reply_anchor_id():
    """Two background completions in one turn share the spawning turn's
    reply-anchor message id; both must queue as distinct turns."""
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:internal-fifo"
    runner._enqueue_fifo(session_key, _text_event("holding the slot", "m-holder"), adapter)

    runner._enqueue_fifo(
        session_key, _internal_text_event("job A completed", "m-anchor"), adapter
    )
    runner._enqueue_fifo(
        session_key, _internal_text_event("job B completed", "m-anchor"), adapter
    )

    queued = runner._session_state(session_key).conversation.queued_events
    assert len(queued) == 2
    assert {e.text for e in queued} == {"job A completed", "job B completed"}


def test_enqueue_fifo_still_dedupes_platform_events_with_same_message_id():
    runner = _bare_runner()
    adapter = _StubAdapter()
    session_key = "telegram:user:platform-dedupe"
    first = _text_event("original copy", "m-dup")
    first.internal = False
    runner._enqueue_fifo(session_key, first, adapter)

    redelivery = _text_event("redelivered copy", "m-dup")
    redelivery.internal = False
    runner._enqueue_fifo(session_key, redelivery, adapter)

    assert runner._queue_depth(session_key, adapter=adapter) == 1
    survivor = adapter._pending_messages[session_key]
    assert survivor is first
    assert survivor.redelivered is True


def test_record_drain_event_keeps_distinct_internal_events_sharing_reply_anchor_id():
    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="internal-drain-chat")
    session_key = runner._session_key_for_source(source)
    event_a = _event("job A completed", source=source, message_id="m-anchor")
    event_a.internal = True
    event_b = _event("job B completed", source=source, message_id="m-anchor")
    event_b.internal = True

    assert record_drain_event(runner, session_key, event_a) is True
    assert record_drain_event(runner, session_key, event_b) is True

    records = _snapshot_events()
    assert len(records) == 2
    assert all(r["event"]["message_id"] == "m-anchor" for r in records)
