"""Code-box presentation covers every tool-progress lane, not just the editable bubble.

Regression: with ``tool_progress_codebox`` on, "separate" grouping (one message
per tool) and non-editable fallback sends shipped plain text. The TurnRunner
drain must box every row, leave self-fenced rows unboxed, and send plain text
when the flag is off.
"""

import asyncio
import importlib
import queue as queue_mod

import pytest

from gateway.config import Platform
from gateway.display_config import format_progress_row
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

ROW = "🔧 list the repository files"
FENCED_ROW = "💻 run script\n```python\nprint(1)\n```"


class FakeAdapter:
    MAX_MESSAGE_LENGTH = 2000
    name = "discord"

    def __init__(self):
        self.sent = []
        self._next = 0
        self.edited = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._next += 1
        self.sent.append(content)
        return SendResult(success=True, message_id=f"m{self._next}")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edited.append((message_id, content))
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        pass


class StubRunner:
    def __init__(self, adapter):
        self._adapter = adapter
        self.adapters = {}

    def _adapter_for_source(self, source):
        return self._adapter

    def _delivery_adapter_for(self, source):
        return self._adapter


def _progress_context(codebox=True, grouping="separate"):
    return TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="describe",
        progress_grouping=grouping,
        progress_codebox=codebox,
        tool_progress_enabled=True,
        progress_queue=queue_mod.Queue(),
    )


async def _deliver_separate(runner_module, rows, codebox):
    runner_cls = importlib.import_module(runner_module).TurnRunner
    adapter = FakeAdapter()
    ctx = _progress_context(codebox)
    for row in rows:
        ctx.progress_queue.put(row)
    task = asyncio.get_running_loop().create_task(runner_cls(StubRunner(adapter), ctx).send_progress_messages())
    while not ctx.progress_queue.empty():
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.8)  # one edit-throttle interval so the row is delivered
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return adapter.sent


MODULES = ["gateway.run_turn_runner"]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_row_is_boxed_when_codebox_on(runner_module):
    sent = await _deliver_separate(runner_module, [ROW], codebox=True)
    assert sent == [f"```\n{ROW}\n```"]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_row_is_plain_when_codebox_off(runner_module):
    sent = await _deliver_separate(runner_module, [ROW], codebox=False)
    assert sent == [ROW]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_fenced_row_stays_unboxed(runner_module):
    sent = await _deliver_separate(runner_module, [FENCED_ROW], codebox=True)
    assert sent == [FENCED_ROW]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_repeat_row_is_boxed_once(runner_module):
    # "separate" grouping never surfaced (×N) counters (same on main with
    # codebox off): a dedup event only rewrites the buffer, not the per-row
    # pending list. The contract here is one box per message, never doubled.
    sent = await _deliver_separate(runner_module, [ROW, ("__dedup__", ROW, 1)], codebox=True)
    assert sent
    for text in sent:
        assert text.startswith("```\n") and text.endswith("\n```")
        assert text.count("```") == 2, text  # never double-boxed
    assert sent == [f"```\n{ROW}\n```"]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_flush_does_not_resend_delivered_row(runner_module):
    runner_cls = importlib.import_module(runner_module).TurnRunner
    adapter = FakeAdapter()
    ctx = _progress_context(codebox=True)
    ctx.progress_queue.put(ROW)
    task = asyncio.get_running_loop().create_task(
        runner_cls(StubRunner(adapter), ctx).send_progress_messages()
    )
    try:
        for _ in range(40):
            if adapter.sent:
                break
            await asyncio.sleep(0.05)
        assert adapter.sent == [f"```\n{ROW}\n```"]

        flushed = asyncio.Event()
        ctx.progress_queue.put(("__flush__", flushed))
        await asyncio.wait_for(flushed.wait(), timeout=2)

        assert flushed.is_set()
        assert adapter.sent == [f"```\n{ROW}\n```"]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_flush_delivers_absorbed_row_once(runner_module):
    runner = importlib.import_module(runner_module).TurnRunner(StubRunner(FakeAdapter()), _progress_context())
    adapter = runner._runner._adapter
    st = runner._progress_edit_state(adapter)
    runner._progress_absorb(st, ROW)
    flushed = asyncio.Event()

    await runner._handle_flush_marker(st, ("__flush__", flushed))

    assert flushed.is_set()
    assert adapter.sent == [f"```\n{ROW}\n```"]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_separate_flush_retries_row_after_failed_send(runner_module):
    adapter = FakeAdapter()
    runner = importlib.import_module(runner_module).TurnRunner(StubRunner(adapter), _progress_context())
    st = runner._progress_edit_state(adapter)
    runner._progress_absorb(st, ROW)
    original_send = adapter.send

    async def fail_send(**kwargs):
        return SendResult(success=False, error="transient")

    adapter.send = fail_send
    await runner._progress_send_or_edit(st, ROW)
    adapter.send = original_send
    flushed = asyncio.Event()

    await runner._handle_flush_marker(st, ("__flush__", flushed))

    assert flushed.is_set()
    assert adapter.sent == [f"```\n{ROW}\n```"]


@pytest.mark.parametrize("runner_module", MODULES)
@pytest.mark.asyncio
async def test_accumulate_flush_edits_existing_bubble(runner_module):
    adapter = FakeAdapter()
    ctx = _progress_context(grouping="accumulate")
    runner = importlib.import_module(runner_module).TurnRunner(StubRunner(adapter), ctx)
    st = runner._progress_edit_state(adapter)
    runner._progress_absorb(st, ROW)
    await runner._progress_send_or_edit(st, ROW)
    flushed = asyncio.Event()

    await runner._handle_flush_marker(st, ("__flush__", flushed))

    assert flushed.is_set()
    assert adapter.sent == [f"```\n{ROW}\n```"]
    assert adapter.edited == [("m1", f"```\n{ROW}\n```")]


def test_format_progress_row_contract():
    assert format_progress_row(ROW, False, 2000) == [ROW]
    assert format_progress_row(ROW, True, 2000) == [f"```\n{ROW}\n```"]
    assert format_progress_row(FENCED_ROW, True, 2000) == [FENCED_ROW]


def test_oversized_row_ships_as_several_complete_boxes():
    row = "x" * 500
    texts = format_progress_row(row, True, 100, len)
    assert len(texts) > 1
    assert all(len(t) <= 100 and t.startswith("```\n") and t.endswith("\n```") for t in texts)
    assert "".join(t[4:-4] for t in texts) == row
