"""Tool-progress bubble vs in-flight narration send ordering (Discord incident).

Live shape (Discord, streaming off, ``display.interim_assistant_messages`` on):
tools enqueue progress rows on the turn's progress queue while an interim
commentary ("narration") send is still in flight.  The old wiring only queued
``('__reset__',)`` via ``on_new_message`` AFTER the send landed — by then the
pump had already painted the queued tool rows into a bubble, so the late reset
closed the box the same step's tools just opened (rows split across boxes, box
above the narration).

Pinned contract (end to end: live ``TurnRunner.send_progress_messages`` pump +
live ``GatewayStreamConsumer`` wired exactly like ``_setup_stream_consumer``):

- while the narration send is in flight, tool rows already queued are NOT sent;
- when the send completes, those rows plus later rows with no new narration
  form ONE box, and the narration send happens BEFORE that box's first send;
- an empty assistant step does not split the box;
- a second narration splits exactly once, sentence before the new box;
- a FAILED narration send still releases the hold;
- the late ``__reset__`` from commentary completion must not close the box the
  same step's tools just opened.
"""

import asyncio
import queue as queue_mod

import pytest

from agent.display import describe_tool_call, get_tool_emoji
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import TurnContext

ARGS_A = {"path": "/tmp/a.md", "content": "a", "description": "write a"}
ARGS_B = {"path": "/tmp/b.md", "content": "b", "description": "write b"}


def _row(name, args):
    return f"{get_tool_emoji(name, default='⚙️')} {describe_tool_call(name, args)}"


class FakeAdapter:
    """Duck-typed Discord-shaped adapter: records every delivered text in order.

    ``hold_first_send`` parks the FIRST send until ``release_send`` is set,
    simulating a narration send still in flight.
    """

    MAX_MESSAGE_LENGTH = 2000
    name = "discord"

    def __init__(self):
        self.bubbles = {}   # message_id -> current text
        self.contents = []  # every delivered text (sends + edits), in order
        self._next = 0
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.hold_first_send = False
        self.fail_sends = False

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.send_started.set()
        if self.hold_first_send:
            self.hold_first_send = False
            await asyncio.wait_for(self.release_send.wait(), timeout=15)
        if self.fail_sends:
            self.fail_sends = False
            return SendResult(success=False, error="boom")
        self._next += 1
        mid = f"msg-{self._next}"
        self.bubbles[mid] = content
        self.contents.append(content)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.bubbles[message_id] = content
        self.contents.append(content)
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        pass


class StubRunner:
    def __init__(self, adapter):
        self._adapter = adapter
        self.adapters = {}

    def _delivery_adapter_for(self, source):
        return self._adapter


def _make():
    adapter = FakeAdapter()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="describe",
        progress_grouping="accumulate",
        # Assertions match the raw row string (``r1 in contents``), not a
        # fenced codebox wrapper. Codebox wrapping is covered elsewhere.
        progress_codebox=False,
        tool_progress_enabled=True,
        progress_queue=queue_mod.Queue(),
    )

    # Wiring mirrors _setup_stream_consumer: on_new_message resets the bubble,
    # progress_box_gate queues the in-flight-hold marker before tool rows.
    def _gate():
        if not ctx._run_still_current():
            return None
        evt = asyncio.Event()
        ctx.progress_queue.put(("__hold__", evt))
        return evt.set

    consumer = GatewayStreamConsumer(
        adapter, "c1", StreamConsumerConfig(edit_interval=0.01),
        on_new_message=lambda: ctx.progress_queue.put(("__reset__",)),
        progress_box_gate=_gate,
        run_still_current=ctx._run_still_current,
    )
    runner = TurnRunner(StubRunner(adapter), ctx)
    return runner, ctx, adapter, consumer


async def _quiet(ctx, settle=1.8):
    async def _wait():
        while not ctx.progress_queue.empty():
            await asyncio.sleep(0.05)
    await asyncio.wait_for(_wait(), timeout=15)
    await asyncio.sleep(settle)


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _rows_of(text):
    return [ln for ln in text.splitlines() if ln.strip() and ln.strip() != "```"]


def _progress_bubbles(adapter, narration_texts):
    """Bubbles that are tool-progress boxes (not narration messages)."""
    return {mid: txt for mid, txt in adapter.bubbles.items()
            if txt not in narration_texts}


@pytest.mark.asyncio
async def test_narration_hold_keeps_box_below_inflight_send():
    """4 rows queued during an in-flight narration send are held; send lands,
    then those 4 + 3 later rows (no new narration) form ONE box below it."""
    runner, ctx, adapter, consumer = _make()
    r1, r2 = _row("write_file", ARGS_A), _row("write_file", ARGS_B)
    pump = asyncio.create_task(runner.send_progress_messages())
    sc = asyncio.create_task(consumer.run())

    # Narration send parks in flight; the hold marker is queued before tool rows.
    adapter.hold_first_send = True
    consumer.on_commentary("Narration one.")
    await asyncio.wait_for(adapter.send_started.wait(), timeout=5)

    for _ in range(4):
        runner.progress_callback("tool.started", "write_file", None, ARGS_A)
    # While the narration is in flight nothing tool-shaped is delivered.
    await asyncio.sleep(2.0)
    assert not any(r1 in txt for txt in adapter.contents), adapter.contents
    assert len(adapter.contents) == 0, adapter.contents

    # Send completes; three more rows arrive with no new narration.
    adapter.release_send.set()
    while "Narration one." not in adapter.contents:
        await asyncio.sleep(0.05)
    for _ in range(3):
        runner.progress_callback("tool.started", "write_file", None, ARGS_B)
    await _quiet(ctx)
    await _cancel(sc)
    await _cancel(pump)

    # The narration landed, exactly once.
    assert adapter.contents.count("Narration one.") == 1, adapter.contents
    boxes = _progress_bubbles(adapter, {"Narration one."})
    assert len(boxes) == 1, adapter.bubbles
    rows = _rows_of(next(iter(boxes.values())))
    # All 4 held rows + 3 later rows in ONE box (dedup collapses repeats).
    assert r1 in rows and r2 in rows, rows
    # The narration send happens BEFORE the box's first send.
    assert adapter.contents.index("Narration one.") < adapter.contents.index(
        next(iter(boxes.values())))


@pytest.mark.asyncio
async def test_empty_assistant_step_does_not_split():
    runner, ctx, adapter, consumer = _make()
    r1 = _row("write_file", ARGS_A)
    pump = asyncio.create_task(runner.send_progress_messages())
    sc = asyncio.create_task(consumer.run())

    runner.progress_callback("tool.started", "write_file", None, ARGS_A)
    await _quiet(ctx)

    # Empty assistant step: no narration, no split, nothing delivered.
    n_before = len(adapter.contents)
    consumer.on_commentary("")
    await asyncio.sleep(1.0)
    assert len(adapter.contents) == n_before, adapter.contents

    # A later row keeps editing the SAME box.
    runner.progress_callback("tool.started", "write_file", None, ARGS_B)
    await _quiet(ctx)
    await _cancel(sc)
    await _cancel(pump)
    assert r1 in adapter.contents
    assert len(adapter.bubbles) == 1, adapter.bubbles


@pytest.mark.asyncio
async def test_second_narration_splits_exactly_once():
    runner, ctx, adapter, consumer = _make()
    r1, r2 = _row("write_file", ARGS_A), _row("write_file", ARGS_B)
    pump = asyncio.create_task(runner.send_progress_messages())
    sc = asyncio.create_task(consumer.run())

    consumer.on_commentary("Narration one.")
    while "Narration one." not in adapter.contents:
        await asyncio.sleep(0.05)
    runner.progress_callback("tool.started", "write_file", None, ARGS_A)
    await _quiet(ctx)

    # Second narration: splits exactly once — sentence before the new box.
    consumer.on_commentary("Narration two.")
    while "Narration two." not in adapter.contents:
        await asyncio.sleep(0.05)
    runner.progress_callback("tool.started", "write_file", None, ARGS_B)
    await _quiet(ctx)
    await _cancel(sc)
    await _cancel(pump)

    assert adapter.contents.count("Narration two.") == 1, adapter.contents
    boxes = _progress_bubbles(adapter, {"Narration one.", "Narration two."})
    assert len(boxes) == 2, adapter.bubbles
    box_ids = sorted(boxes, key=lambda mid: adapter.contents.index(boxes[mid]))
    first_rows = _rows_of(boxes[box_ids[0]])
    second_rows = _rows_of(boxes[box_ids[1]])
    assert r1 in first_rows and r1 not in second_rows, (first_rows, second_rows)
    assert r2 in second_rows and r2 not in first_rows, (first_rows, second_rows)
    # Narration two lands BEFORE the second box's first send.
    i_n2 = adapter.contents.index("Narration two.")
    i_box2 = adapter.contents.index(boxes[box_ids[1]])
    assert i_n2 < i_box2


@pytest.mark.asyncio
async def test_failed_narration_send_releases_hold():
    runner, ctx, adapter, consumer = _make()
    r1 = _row("write_file", ARGS_A)
    pump = asyncio.create_task(runner.send_progress_messages())
    sc = asyncio.create_task(consumer.run())

    adapter.fail_sends = True
    consumer.on_commentary("Narration doomed.")
    await asyncio.wait_for(adapter.send_started.wait(), timeout=5)
    # The failed send must still release the hold: rows flow into a box.
    runner.progress_callback("tool.started", "write_file", None, ARGS_A)
    await _quiet(ctx)
    await _cancel(sc)
    await _cancel(pump)

    assert "Narration doomed." not in adapter.contents
    assert any(r1 in txt for txt in adapter.contents), adapter.contents


@pytest.mark.asyncio
async def test_pump_holds_at_marker_until_released():
    """Pump-level contract, no consumer: the gate marker holds queued rows in
    one undelivered buffer until released."""
    runner, ctx, adapter, _consumer = _make()
    r1 = _row("write_file", ARGS_A)
    evt = asyncio.Event()
    ctx.progress_queue.put(("__hold__", evt))
    for _ in range(4):
        runner.progress_callback("tool.started", "write_file", None, ARGS_A)
    pump = asyncio.create_task(runner.send_progress_messages())
    await asyncio.sleep(2.0)
    assert adapter.contents == [], adapter.contents  # held, not sent
    evt.set()
    await _quiet(ctx)
    await _cancel(pump)
    assert any(r1 in txt for txt in adapter.contents), adapter.contents
    assert len(adapter.bubbles) == 1, adapter.bubbles
