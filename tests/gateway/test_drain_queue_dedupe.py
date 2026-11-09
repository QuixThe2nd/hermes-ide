"""Dedupe for gateway queue re-delivery: ONE agent turn per inbound message id.

The invariant: no matter how a platform message reaches the gateway — FIFO
overflow promotion after a busy window, or busy-window rescue staging on the
idle path — it must never start more than one agent turn. Duplicate copies
are dropped idempotently (one log line each), and an event that survives
dedupe as a known re-delivery carries a visible marker so the model can tell
a redelivery from a fresh user message instead of answering each copy.

Observed on this host (2026-09-14): a restart replay re-injected one user
message seven times (~64 duplicate replies in one thread), and a session whose
FIFO overflow had grown ~24-29 copies of ONE message drained one duplicate
turn per promoted copy, re-appending as it went. (The durable drain-snapshot
replay itself was removed in Nov 2026 — restarts no longer snapshot other
chats' work — but the FIFO-level dedupe that incident motivated remains.)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)

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
