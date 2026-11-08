"""Retried identical tool-progress rows after an interim content-bubble reset.

Live Discord incident: a parallel ``[write_file, skill_manage]`` batch failed
validation, an interim assistant text bubble landed in the channel (queueing
``('__reset__',)`` on the progress queue), and the model retried
``skill_manage`` alone ~11s later.  With ``tool_progress=describe`` the retry
renders the byte-identical row, so — depending on drain ordering — the channel
showed the same plain row TWICE (the reset cleared the dedup memory) or an
empty `````\\n\\n````` codebox bubble plus a lost row (a ``__dedup__`` absorb
against the just-cleared buffer returned the row without absorbing it).

Pinned end to end against the live ``TurnRunner`` class: a retried call whose
row text matches the previous row must update that row with a ``(×2)`` counter,
and the drain must never render an empty bubble.
"""

import asyncio
import queue as queue_mod

import pytest

from agent.display import describe_tool_call, get_tool_emoji
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

WRITE_ARGS = {
    "path": "/root/.hermes/data/backlog/BACKLOG.md",
    "content": "# Backlog\n",
    "description": "seed the shared backlog file",
}
SKILL_ARGS = {
    "description": "save shared backlog operating procedure",
    "operations": [
        {
            "action": "create",
            "name": "backlog",
            "content": "---\nname: backlog\n---\n",
        }
    ],
}


def _row(tool_name, args):
    """The describe-mode row, computed exactly as ``_progress_build_message``."""
    return f"{get_tool_emoji(tool_name, default='⚙️')} {describe_tool_call(tool_name, args)}"


class FakeAdapter:
    """Plain duck-typed adapter: records every delivered bubble text."""

    MAX_MESSAGE_LENGTH = 2000
    name = "discord"

    def __init__(self):
        self.bubbles = {}   # message_id -> current text
        self.contents = []  # every delivered text (sends + edits), in order
        self._next = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
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

    def format_tool_preview(self, preview, **kwargs):
        return getattr(preview, "text", preview)


class StubRunner:
    def __init__(self, adapter):
        self._adapter = adapter
        self.adapters = {}

    def _adapter_for_source(self, source):
        return self._adapter

    def _delivery_adapter_for(self, source):
        return self._adapter


def _make(runner_cls):
    adapter = FakeAdapter()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="describe",
        progress_grouping="accumulate",
        progress_codebox=True,
        tool_progress_enabled=True,
        progress_queue=queue_mod.Queue(),
    )
    return runner_cls(StubRunner(adapter), ctx), ctx, adapter


def _live_runner_cls():
    return TurnRunner


async def _wait_queue_empty(ctx, timeout=10.0):
    async def _wait():
        while not ctx.progress_queue.empty():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_wait(), timeout)


async def _run_drain_until_quiet(runner, ctx, settle=1.8):
    """Drain as a live task; wait for the queue to empty plus one edit
    throttle interval (1.5s) so buffered rows are actually delivered."""
    task = asyncio.get_running_loop().create_task(runner.send_progress_messages())
    await _wait_queue_empty(ctx)
    await asyncio.sleep(settle)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _bubble_rows(text):
    """Non-fence lines of a (possibly codeboxed) bubble text."""
    return [ln for ln in text.splitlines() if ln.strip() and ln.strip() != "```"]


def _all_rows(adapter):
    rows = []
    for text in adapter.bubbles.values():
        rows.extend(_bubble_rows(text))
    return rows


def _assert_no_empty_bubble(adapter):
    for text in adapter.contents:
        assert _bubble_rows(text), f"empty progress bubble delivered: {text!r}"


@pytest.mark.asyncio
async def test_incident_burst_retry_collapses_into_counter_row():
    """T1 — the live incident ordering: both rows, the interim reset and the
    retry all queued before the drain runs."""
    runner, ctx, adapter = _make(TurnRunner)
    plain_skill = _row("skill_manage", SKILL_ARGS)

    runner.progress_callback("tool.started", "write_file", None, WRITE_ARGS)
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)
    # Interim assistant content bubble lands in the channel.
    ctx.progress_queue.put(("__reset__",))
    # ~11s later the model retries the identical skill_manage call.
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)

    await _run_drain_until_quiet(runner, ctx)

    rows = _all_rows(adapter)
    assert rows.count(plain_skill) <= 1, rows
    assert rows.count(f"{plain_skill} (×2)") == 1, rows
    _assert_no_empty_bubble(adapter)


@pytest.mark.asyncio
async def test_reset_processed_before_retry_keeps_dedup_memory():
    """T2 — the drain processes the reset first, then the retry arrives."""
    runner, ctx, adapter = _make(_live_runner_cls())
    plain_skill = _row("skill_manage", SKILL_ARGS)

    runner.progress_callback("tool.started", "write_file", None, WRITE_ARGS)
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)
    task = asyncio.get_running_loop().create_task(runner.send_progress_messages())
    await _wait_queue_empty(ctx)
    await asyncio.sleep(1.8)

    ctx.progress_queue.put(("__reset__",))
    await asyncio.sleep(0.6)  # let the drain's 0.3s poll process the reset
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)
    await _wait_queue_empty(ctx)
    await asyncio.sleep(1.8)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    rows = _all_rows(adapter)
    assert rows.count(plain_skill) <= 1, rows
    assert rows.count(f"{plain_skill} (×2)") == 1, rows
    _assert_no_empty_bubble(adapter)


@pytest.mark.asyncio
async def test_baseline_consecutive_repeats_without_reset():
    """T3 — no reset: the plain row renders once, the counted row once, one bubble."""
    runner, ctx, adapter = _make(_live_runner_cls())
    plain_write = _row("write_file", WRITE_ARGS)
    plain_skill = _row("skill_manage", SKILL_ARGS)

    runner.progress_callback("tool.started", "write_file", None, WRITE_ARGS)
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)
    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)

    await _run_drain_until_quiet(runner, ctx)

    assert len(adapter.bubbles) == 1, adapter.bubbles
    rows = _all_rows(adapter)
    # The counted row REPLACES the plain row in the editable bubble (the edit
    # throttle may defer the plain row's first delivery until after the repeat
    # arrives, so the plain text need never be delivered at all).
    assert rows.count(plain_skill) == 0, rows
    assert rows[-1] == f"{plain_skill} (×2)", rows
    assert plain_write in rows, rows
    _assert_no_empty_bubble(adapter)


@pytest.mark.asyncio
async def test_distinct_row_after_reset_starts_fresh_bubble():
    """T4 — a DIFFERENT row after a reset still opens a fresh bubble, un-counted."""
    runner, ctx, adapter = _make(_live_runner_cls())
    plain_write = _row("write_file", WRITE_ARGS)

    runner.progress_callback("tool.started", "skill_manage", None, SKILL_ARGS)
    task = asyncio.get_running_loop().create_task(runner.send_progress_messages())
    await _wait_queue_empty(ctx)
    await asyncio.sleep(1.8)

    ctx.progress_queue.put(("__reset__",))
    await asyncio.sleep(0.6)
    runner.progress_callback("tool.started", "write_file", None, WRITE_ARGS)
    await _wait_queue_empty(ctx)
    await asyncio.sleep(1.8)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(adapter.bubbles) == 2, adapter.bubbles
    second_rows = _bubble_rows(adapter.bubbles["msg-2"])
    assert second_rows == [plain_write], adapter.bubbles
    assert all("(×" not in ln for ln in second_rows), second_rows
    _assert_no_empty_bubble(adapter)
