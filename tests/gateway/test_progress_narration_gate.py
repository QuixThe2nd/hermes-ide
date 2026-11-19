"""Narration gate: ONE tool-progress box per narration, BELOW the sentence.

Live incident (2026-11-13, Discord — ``display.platforms.discord.streaming:
false``, ``interim_assistant_messages: true``): interim commentary was queued,
tools ran and enqueued progress rows immediately, but the bubble reset only
reached the progress queue when the commentary SEND finished
(``_send_commentary`` -> ``on_new_message`` -> ``('__reset__',)``).  The rows
were already ahead of the reset: the pump painted them into the box above the
sentence, the late reset closed that box, and the next tool round opened a
SECOND box — same rows, same order, two boxes.

The contract these tests pin:

* a narration (interim commentary, or a streamed segment's first send) closes
  the box ABOVE the sentence; every tool row until the next narration shares
  ONE box BELOW the sentence, first sent only after the sentence is on screen;
* an empty-text assistant step neither resets nor splits;
* a failed commentary send still releases the hold (rows must appear);
* ``TurnRunner._setup_stream_consumer`` actually returns (a dangling name from
  the god-file extraction raised NameError before the narration-gate wiring
  could ever run).
"""

import asyncio
import inspect
import queue as queue_mod
import threading

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.turn_context import TurnContext


class _NarrationAdapter:
    """Editable adapter shared by the progress pump and the stream consumer
    (production shares one transport too).

    Records every send/edit in one ordered event list; ``park_send`` holds a
    matching send in flight so tests can queue tool rows while the narration
    is still unresolved — the exact race from the live incident.
    """

    MAX_MESSAGE_LENGTH = 4000
    name = "discord"
    platform = Platform.DISCORD

    def __init__(self):
        self.events = []  # ("send"|"edit", content) in delivery order
        self.bubbles = {}  # message_id -> current text
        self._next_id = 0
        self._parked = {}  # marker -> (entered, release) asyncio events
        self._fail_markers = set()

    def park_send(self, marker):
        entered, release = asyncio.Event(), asyncio.Event()
        self._parked[marker] = (entered, release)
        return entered, release

    def fail_sends_matching(self, marker):
        self._fail_markers.add(marker)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        for marker, (entered, release) in self._parked.items():
            if marker in content:
                entered.set()
                await release.wait()
                break
        self._next_id += 1
        if any(marker in content for marker in self._fail_markers):
            self.events.append(("send-failed", content))
            return SendResult(success=False, error="narration send failed")
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
    q = queue_mod.Queue()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="all",
        progress_grouping="accumulate",
        tool_progress_enabled=True,
        progress_queue=q,
    )
    return TurnRunner(_StubGatewayRunner(adapter), ctx), ctx, q


def _consumer(adapter, progress_q):
    """Consumer wired like ``TurnRunner._setup_stream_consumer`` in production.

    ``on_progress_gate`` only exists once the narration gate lands; wiring it
    when present keeps the pre-fix run failing on the SPLIT (two boxes), not
    on a TypeError.
    """
    kwargs = dict(
        adapter=adapter,
        chat_id="c1",
        on_new_message=lambda: progress_q.put(("__reset__",)),
    )
    if "on_progress_gate" in inspect.signature(GatewayStreamConsumer.__init__).parameters:
        kwargs["on_progress_gate"] = lambda evt: progress_q.put(("__gate__", evt))
    return GatewayStreamConsumer(**kwargs)


async def _wait_until(pred, timeout=8.0):
    async def _wait():
        while not pred():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_wait(), timeout)


def _sent_contents(adapter):
    return [c for kind, c in adapter.events if kind == "send"]


def _order_index(adapter, content, *, prefix=False):
    for idx, (_kind, c) in enumerate(adapter.events):
        if (c.startswith(content) if prefix else c == content):
            return idx
    raise AssertionError(f"never delivered: {content!r}\nevents={adapter.events}")


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── the live incident ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commentary_in_flight_rows_queue_into_one_box_below_sentence():
    """Commentary send still in flight, 4 rows already queued, send completes,
    3 more rows with no new commentary: ONE box with all 7 rows, and the
    commentary send happens BEFORE that box's first send."""
    adapter = _NarrationAdapter()
    runner, _ctx, q = _make(adapter)
    consumer = _consumer(adapter, q)
    entered, release = adapter.park_send("First narration sentence.")

    loop = asyncio.get_running_loop()
    pump = loop.create_task(runner.send_progress_messages())
    stream = loop.create_task(consumer.run())
    try:
        consumer.on_commentary("First narration sentence.")
        await asyncio.wait_for(entered.wait(), 5)  # commentary send in flight

        for i in range(1, 5):  # this step's tool rows, already ahead of the reset
            q.put(f"tool-{i}")
        release.set()  # sentence lands

        for i in range(5, 8):  # next tool round, no new commentary
            q.put(f"tool-{i}")

        await _wait_until(lambda: any("tool-7" in c for _k, c in adapter.events))
    finally:
        await _cancel(pump)
        await _cancel(stream)

    box_sends = [c for c in _sent_contents(adapter) if "tool-" in c]
    assert len(box_sends) == 1, f"one chain split into {len(box_sends)} boxes: {adapter.events}"
    assert all(f"tool-{i}" in box_sends[0] for i in range(1, 8))
    assert (
        _order_index(adapter, "First narration sentence.")
        < _order_index(adapter, box_sends[0])
    ), "box was first-sent before the narration sentence landed"


@pytest.mark.asyncio
async def test_empty_step_text_keeps_the_same_box():
    """An assistant step with empty text must NOT reset or split: rows keep
    editing the open box."""
    adapter = _NarrationAdapter()
    runner, _ctx, q = _make(adapter)
    consumer = _consumer(adapter, q)

    loop = asyncio.get_running_loop()
    pump = loop.create_task(runner.send_progress_messages())
    stream = loop.create_task(consumer.run())
    try:
        for i in range(1, 4):
            q.put(f"tool-{i}")
        await _wait_until(lambda: any("tool-3" in c for _k, c in adapter.events))

        consumer.on_commentary("   ")  # empty-text step: no narration lands

        for i in range(4, 7):
            q.put(f"tool-{i}")
        await _wait_until(lambda: any("tool-6" in c for _k, c in adapter.events))
    finally:
        await _cancel(pump)
        await _cancel(stream)

    box_ids = [mid for mid, text in adapter.bubbles.items() if "tool-1" in text]
    assert len(box_ids) == 1, adapter.bubbles
    box_sends = [c for c in _sent_contents(adapter) if "tool-" in c]
    assert len(box_sends) == 1, adapter.events
    assert all(f"tool-{i}" in adapter.bubbles[box_ids[0]] for i in range(1, 7))


@pytest.mark.asyncio
async def test_second_narration_splits_exactly_once_below_new_sentence():
    """A later real narration splits ONCE: the new sentence closes the first
    box, and the following rows open exactly one new box below the sentence."""
    adapter = _NarrationAdapter()
    runner, _ctx, q = _make(adapter)
    consumer = _consumer(adapter, q)
    entered_a, release_a = adapter.park_send("Narration A.")
    entered_b, release_b = adapter.park_send("Narration B.")

    loop = asyncio.get_running_loop()
    pump = loop.create_task(runner.send_progress_messages())
    stream = loop.create_task(consumer.run())
    try:
        consumer.on_commentary("Narration A.")
        await asyncio.wait_for(entered_a.wait(), 5)
        for i in range(1, 4):
            q.put(f"tool-a{i}")
        release_a.set()
        await _wait_until(lambda: any("tool-a3" in c for _k, c in adapter.events))

        consumer.on_commentary("Narration B.")
        await asyncio.wait_for(entered_b.wait(), 5)
        for i in range(1, 4):
            q.put(f"tool-b{i}")
        release_b.set()
        await _wait_until(lambda: any("tool-b3" in c for _k, c in adapter.events))
    finally:
        await _cancel(pump)
        await _cancel(stream)

    box_sends = [c for c in _sent_contents(adapter) if "tool-" in c]
    assert len(box_sends) == 2, adapter.events  # splits once, not twice
    first_box, second_box = box_sends
    assert "tool-a1" in first_box and "tool-b1" not in first_box
    assert "tool-b1" in second_box and "tool-a1" not in second_box
    a, b = _order_index(adapter, "Narration A."), _order_index(adapter, "Narration B.")
    assert a < _order_index(adapter, first_box) < b < _order_index(adapter, second_box), (
        adapter.events
    )


@pytest.mark.asyncio
async def test_failed_commentary_send_still_releases_the_rows():
    """A failed commentary send must release the hold — no deadlock, the tool
    rows still appear (as one box)."""
    adapter = _NarrationAdapter()
    runner, _ctx, q = _make(adapter)
    consumer = _consumer(adapter, q)
    adapter.fail_sends_matching("Failing narration.")

    loop = asyncio.get_running_loop()
    pump = loop.create_task(runner.send_progress_messages())
    stream = loop.create_task(consumer.run())
    try:
        consumer.on_commentary("Failing narration.")
        for i in range(1, 5):
            q.put(f"tool-{i}")
        await _wait_until(lambda: any("tool-4" in c for _k, c in adapter.events))
    finally:
        await _cancel(pump)
        await _cancel(stream)

    box_sends = [c for c in _sent_contents(adapter) if "tool-" in c]
    assert len(box_sends) == 1, adapter.events
    assert all(f"tool-{i}" in box_sends[0] for i in range(1, 5))
    assert any(kind == "send-failed" for kind, _c in adapter.events)


# ── streaming sibling: the segment's first send is a narration too ────────


@pytest.mark.asyncio
async def test_streamed_first_send_keeps_one_box_below_sentence():
    """Sibling of the commentary race on the streaming rail: the segment's
    first send lands its text, and that send's ``on_new_message`` reset must
    not overtake the same step's tool rows either."""
    adapter = _NarrationAdapter()
    runner, _ctx, q = _make(adapter)
    consumer = _consumer(adapter, q)
    entered, release = adapter.park_send("Let me look into that.")

    loop = asyncio.get_running_loop()
    pump = loop.create_task(runner.send_progress_messages())
    stream = loop.create_task(consumer.run())
    try:
        consumer.on_delta("Let me look into that.")  # first delta of the segment
        await asyncio.wait_for(entered.wait(), 5)  # first send in flight

        for i in range(1, 5):  # tools of the same step start immediately
            q.put(f"tool-{i}")
        release.set()  # sentence lands

        for i in range(5, 8):
            q.put(f"tool-{i}")

        await _wait_until(lambda: any("tool-7" in c for _k, c in adapter.events))
    finally:
        await _cancel(pump)
        await _cancel(stream)

    box_sends = [c for c in _sent_contents(adapter) if "tool-" in c]
    assert len(box_sends) == 1, f"one chain split into {len(box_sends)} boxes: {adapter.events}"
    assert all(f"tool-{i}" in box_sends[0] for i in range(1, 8))
    assert (
        _order_index(adapter, "Let me look into that.", prefix=True)
        < _order_index(adapter, box_sends[0])
    ), "box was first-sent before the streamed sentence landed"


# ── production wiring ─────────────────────────────────────────────────────


class _WiringStubRunner(_StubGatewayRunner):
    def _build_stream_consumer_config(self, source, scfg, adapter, **kwargs):
        return None, None


def test_setup_stream_consumer_returns_wired_consumer():
    """``_setup_stream_consumer`` must return, and wire the narration gate onto
    the progress queue BEFORE any tool row of the narrated step can enqueue.

    Regression: the god-file extraction left ``stts`` unresolved, so every
    call raised NameError before returning — no turn ever got a stream
    consumer, interim commentary included.
    """
    adapter = _NarrationAdapter()
    _runner, ctx, q = _make(adapter)
    ctx.resolve_display_setting = lambda cfg, key, name: False  # discord streaming off
    ctx.interim_assistant_messages_enabled = True
    ctx.user_config = {}
    turn_runner = TurnRunner(_WiringStubRunner(adapter), ctx)

    stream_consumer, _delta_cb, interim_cb, want_interim = turn_runner._setup_stream_consumer(
        "discord"
    )
    assert stream_consumer is not None
    assert callable(interim_cb)
    assert want_interim is True

    # The narration gate rides the progress queue ahead of the step's rows.
    stream_consumer.on_commentary("Hello there.")
    raw = q.get_nowait()
    assert isinstance(raw, tuple) and len(raw) == 2 and raw[0] == "__gate__", raw
    assert isinstance(raw[1], threading.Event)
    assert q.empty()  # no late __reset__ behind it for this commentary
