"""Mid-turn adapter reconnect: in-flight progress pump and stream consumer must follow the live transport."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import queue
import time
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import SendResult
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.turn_context import TurnContext


class _RecordingAdapter:
    """Minimal editable-progress adapter that records transport calls."""

    name = "test"
    MAX_MESSAGE_LENGTH = 100_000

    def __init__(self, label: str):
        self.label = label
        self.calls: list[tuple] = []

    async def send(self, chat_id=None, content=None, reply_to=None, metadata=None, **_kw):
        self.calls.append(("send", self.label, content))
        return SendResult(success=True, message_id=f"msg-{self.label}-{len(self.calls)}")

    async def edit_message(
        self, chat_id=None, message_id=None, content=None, metadata=None, **_kw
    ):
        self.calls.append(("edit", self.label, message_id, content))
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.calls.append(("typing", self.label, chat_id))


class _StubRunner:
    def __init__(self, adapter):
        self.current_adapter = adapter

    def _adapter_for_source(self, _source):
        return self.current_adapter

    def _delivery_adapter_for(self, _source):
        return self.current_adapter


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def _make_turn_context(progress_q: queue.Queue, still: list[bool]) -> TurnContext:
    source = MagicMock()
    source.chat_id = "chat-1"
    return TurnContext(
        source=source,
        _run_still_current=lambda: still[0],
        progress_queue=progress_q,
        progress_grouping="accumulate",
        progress_codebox=False,
        _progress_metadata=None,
        _progress_reply_to=None,
        _native_slack_task_cards=False,
        _cleanup_progress=False,
        agent_holder=[None],
    )


@pytest.mark.parametrize(
    "turn_runner_path",
    [
        pytest.param("gateway.run", id="live"),
        pytest.param("gateway.run_turn_runner", id="shadow"),
    ],
)
@pytest.mark.asyncio
async def test_progress_pump_re_resolves_adapter_after_mid_turn_swap(turn_runner_path):
    mod = importlib.import_module(turn_runner_path)
    TurnRunner = mod.TurnRunner

    progress_q: queue.Queue = queue.Queue()
    still = [True]
    ctx = _make_turn_context(progress_q, still)

    adapter_a = _RecordingAdapter("A")
    adapter_b = _RecordingAdapter("B")
    runner = _StubRunner(adapter_a)
    turn = TurnRunner(runner, ctx)

    task = asyncio.create_task(turn.send_progress_messages())
    try:
        progress_q.put("first tool line")
        await _wait_until(lambda: any(c[0] == "send" for c in adapter_a.calls))
        # Progress edits are throttled (1.5s); let the first bubble settle before the
        # post-swap row so the next queue item reaches the edit path in one iteration.
        await asyncio.sleep(1.6)

        calls_on_a_at_swap = len(adapter_a.calls)
        runner.current_adapter = adapter_b

        progress_q.put("second tool line")
        await _wait_until(
            lambda: any(
                c[0] in ("send", "edit") and c[1] == "B" for c in adapter_b.calls
            )
        )

        assert len(adapter_a.calls) == calls_on_a_at_swap, (
            "adapter A must receive no further transport calls after the swap"
        )
        post_swap_b = [
            c
            for c in adapter_b.calls
            if c[0] in ("send", "edit", "typing")
        ]
        assert post_swap_b, "adapter B must handle post-swap progress delivery"
        assert all(c[1] == "B" for c in post_swap_b)
    finally:
        still[0] = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_stream_consumer_adapter_provider_follows_live_adapter():
    adapter_a = MagicMock(name="adapter_a")
    adapter_b = MagicMock(name="adapter_b")
    slot = {"live": adapter_a}

    def provider():
        return slot["live"]

    consumer = GatewayStreamConsumer(
        adapter=adapter_a,
        chat_id="chat-1",
        adapter_provider=provider,
    )
    assert consumer.adapter is adapter_a

    slot["live"] = adapter_b
    assert consumer.adapter is adapter_b

    slot["live"] = None
    assert consumer.adapter is adapter_a
