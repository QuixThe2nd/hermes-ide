"""Narration hold vs context-card ordering gate: the two gates must never wait on each other.

Live incident: a turn's first visible output was a narration sentence while
context cards were still queued on the progress queue. ``on_commentary`` had
already armed the narration gate, so the pump (``TurnRunner.send_progress_messages``)
parked inside ``_handle_hold_marker`` waiting for the narration send to finish —
but ``_deliver_commentary`` awaits the context-card gate BEFORE the send, and
that gate's ``("__flush__", event)`` marker sat BEHIND the hold marker in the
FIFO queue. The pump never reached the flush; the narration never sent; only
the 60s "progress hold marker never released" timeout broke the cycle, so the
sentence landed a minute late and BELOW the tool box it narrated.

Pinned contract (real pump + real consumer + fake adapter, cards queued):

- the narration is sent without waiting for the hold timeout, and the visible
  order is: context-card box, narration, then the next tool box;
- a flush marker parked behind a hold is serviced while the hold waits,
  without reordering any queued row;
- an unreleased hold still falls back to the timeout guard.
"""

import asyncio
import logging
import queue as queue_mod

import pytest

from agent.display import describe_tool_call, get_tool_emoji
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import TurnContext

ARGS = {"path": "/tmp/a.md", "content": "a", "description": "write a"}
CARD_HEADING = "🗺️ Injecting session context"
NARRATION = "Narration sentence."
HOLD_WARNING = "progress hold marker never released"


def _row():
    return f"{get_tool_emoji('write_file', default='⚙️')} {describe_tool_call('write_file', ARGS)}"


class _Adapter:
    """Editable Discord-shaped adapter recording every delivery in order."""

    MAX_MESSAGE_LENGTH = 4000
    name = "discord"
    platform = Platform.DISCORD

    def __init__(self):
        self.events = []  # ("send"|"edit", content) in delivery order
        self.bubbles = {}  # message_id -> current text
        self._next_id = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._next_id += 1
        mid = f"msg-{self._next_id}"
        self.bubbles[mid] = content
        self.events.append(("send", content))
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.bubbles[message_id] = content
        self.events.append(("edit", content))
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        pass

    def format_tool_preview(self, preview, **kwargs):
        return getattr(preview, "text", preview)


class _StubGatewayRunner:
    def __init__(self, adapter):
        self._adapter = adapter
        self.config = None

    def _delivery_adapter_for(self, source):
        return self._adapter


def _make(adapter):
    """Runner/consumer wired like ``TurnRunner._setup_stream_consumer`` in production:
    narration gate armed before tool rows, context-card gate on visible output."""
    q = queue_mod.Queue()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="describe",
        progress_grouping="accumulate",
        tool_progress_enabled=True,
        progress_queue=q,
        user_config={"display": {"context_injection_content": False}},
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "c1",
        StreamConsumerConfig(edit_interval=0.01),
        on_new_message=lambda: q.put(("__reset__",)),
        on_progress_gate=lambda evt: q.put(("__gate__", evt)),
        context_gate=ctx.wait_context_cards_delivered,
        run_still_current=ctx._run_still_current,
    )
    runner = TurnRunner(_StubGatewayRunner(adapter), ctx)
    return runner, ctx, consumer, q


def _queue_context_card(runner):
    runner.progress_callback(
        "context.injected", "context", None,
        {"content": "session bytes", "injected_chars": 13,
         "sources": ["gateway"], "kinds": ["session"]},
    )


async def _wait_until(pred, timeout=8.0):
    async def _wait():
        while not pred():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_wait(), timeout)


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _delivered(adapter, needle):
    return any(needle in content for _kind, content in adapter.events)


def _first_index(adapter, needle):
    for idx, (_kind, content) in enumerate(adapter.events):
        if needle in content:
            return idx
    raise AssertionError(f"never delivered: {needle!r}\nevents={adapter.events}")


@pytest.mark.asyncio
async def test_first_narration_with_cards_queued_lands_between_the_boxes(caplog):
    """Cards queued, then a narration plus a tool row as the turn's first
    visible output: card box, then the narration, then the tool box — with no
    wait on the hold timeout and no 'never released' warning."""
    adapter = _Adapter()
    runner, ctx, consumer, _q = _make(adapter)
    row = _row()
    pump = asyncio.create_task(runner.send_progress_messages())
    # The ordering gate's shutdown signal is the owning drain task's lifecycle.
    ctx.bind_progress_task(pump)
    sc = asyncio.create_task(consumer.run())
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        _queue_context_card(runner)
        consumer.on_commentary(NARRATION)
        runner.progress_callback("tool.started", "write_file", None, ARGS)
        with caplog.at_level(logging.WARNING, logger="gateway.run"):
            await _wait_until(
                lambda: all(
                    _delivered(adapter, needle) for needle in (CARD_HEADING, NARRATION, row)
                )
            )
        elapsed = loop.time() - started
    finally:
        await _cancel(sc)
        await _cancel(pump)

    i_card = _first_index(adapter, CARD_HEADING)
    i_narration = _first_index(adapter, NARRATION)
    i_row = _first_index(adapter, row)
    assert i_card < i_narration < i_row, adapter.events
    assert elapsed < 30, f"narration waited on the hold timeout ({elapsed:.1f}s)"
    assert not any(HOLD_WARNING in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


@pytest.mark.asyncio
async def test_parked_flush_marker_is_serviced_during_the_hold_without_reordering_rows():
    """Pump-level contract, no consumer: a flush marker queued BEHIND a parked
    hold is serviced while the hold waits (its waiter's gate is what releases
    the hold), and the rows queued under the hold keep strict FIFO order."""
    adapter = _Adapter()
    runner, ctx, _consumer, q = _make(adapter)
    flush = asyncio.Event()
    hold = asyncio.Event()
    card = "context card row"
    q.put(card)
    q.put(("__hold__", hold))
    q.put("tool row A")
    q.put(("__flush__", flush))
    q.put("tool row B")
    pump = asyncio.create_task(runner.send_progress_messages())
    ctx.bind_progress_task(pump)
    try:
        # The flush waiter is released while the narration hold is still parked...
        await asyncio.wait_for(flush.wait(), timeout=8)
        # ...but the rows queued under the hold are NOT delivered early.
        assert not _delivered(adapter, "tool row A"), adapter.events
        assert not _delivered(adapter, "tool row B"), adapter.events
        hold.set()
        await _wait_until(lambda: _delivered(adapter, "tool row B"))
    finally:
        await _cancel(pump)

    i_card = _first_index(adapter, card)
    i_a = _first_index(adapter, "tool row A")
    i_b = _first_index(adapter, "tool row B")
    assert i_card < i_a <= i_b, adapter.events
    box = next(content for _kind, content in adapter.events if "tool row B" in content)
    assert box.index("tool row A") < box.index("tool row B"), box


@pytest.mark.asyncio
async def test_unreleased_hold_still_falls_back_to_the_timeout(caplog, monkeypatch):
    """A narration send that never releases (dead consumer) is still bounded by
    the last-resort hold timeout: queued rows land after the guard fires."""
    monkeypatch.setattr(TurnRunner, "_PROGRESS_HOLD_TIMEOUT_S", 0.3)
    adapter = _Adapter()
    runner, ctx, _consumer, q = _make(adapter)
    hold = asyncio.Event()  # never set: the narration send died
    q.put(("__hold__", hold))
    q.put("tool row C")
    pump = asyncio.create_task(runner.send_progress_messages())
    ctx.bind_progress_task(pump)
    try:
        with caplog.at_level(logging.WARNING, logger="gateway.run"):
            await _wait_until(lambda: _delivered(adapter, "tool row C"))
    finally:
        await _cancel(pump)
    assert any(HOLD_WARNING in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
