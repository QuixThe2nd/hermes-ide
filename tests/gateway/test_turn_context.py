"""Unit tests for the TurnContext/TurnRunner seam extracted from
``GatewayRunner._run_agent_inner`` (gateway/turn_context.py + gateway/run.py).

The extraction contract: the closure bodies moved onto ``TurnRunner`` methods
byte-identically (modulo local -> ctx.field rewrites), with every closed-over
local carried as a ``TurnContext`` field. These tests pin the seam's wiring —
shared mutable containers, no-queue early returns — not the progress behavior
itself (that's covered by test_run_progress_topics.py et al.).
"""

import asyncio
import queue as queue_mod
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _make_runner(ctx, adapter=None):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return adapter

    return TurnRunner(_StubGatewayRunner(), ctx)


# ---------------------------------------------------------------------------
# Branded agent-viewer status ordering (delegate_claude_agent /
# delegate_cursor_agent live-viewer notices).
#
# The notice is emitted from inside the tool call, i.e. from the same worker
# thread that already enqueued the ``tool.started`` row. Routing it through the
# same FIFO queue makes "tool row first, viewer notice second" an ordering fact
# instead of a race between the queue consumer and an independently scheduled
# status coro. These tests pin that contract without any network timing.
# ---------------------------------------------------------------------------

_CLAUDE_NOTICE = "Claude Code Agent: http://192.168.30.20:8787/#20260829-024525-1532951"
_CURSOR_NOTICE = "Cursor Cloud Agent: https://cursor.com/agents/bc-abc123"


class _CaptureCore:
    """Records sends in delivery order and wakes per-send waiters.

    Tests await an exact send count instead of sleeping, so nothing here
    depends on the consumer's poll cadence.
    """

    def __init__(self):
        self.sent = []
        self.edits = []
        self.typing = []
        self._waiters = []
        self.MAX_MESSAGE_LENGTH = 4000

    def _wake(self):
        waiters, self._waiters = self._waiters, []
        for event in waiters:
            event.set()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        message_id = f"msg-{len(self.sent) + 1}"
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
                "message_id": message_id,
            }
        )
        self._wake()
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.typing.append(chat_id)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    def format_tool_preview(self, preview, **kwargs):
        # Same as BasePlatformAdapter's default (plain compact text).
        return getattr(preview, "text", preview)


class _OrderCaptureAdapter(_CaptureCore):
    """Editable adapter: ``edit_message`` is a real method on the type."""

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        from gateway.platforms.base import SendResult

        self.edits.append({"message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)


class _NoEditCaptureAdapter(_CaptureCore):
    """Duck-typed adapter with no ``edit_message`` at all (can't edit)."""


def _status_ctx(
    progress_queue,
    *,
    tool_progress=True,
    adapter=None,
    grouping="grouped",
    agent=None,
    loop=None,
    status_metadata=None,
    cleanup_progress=False,
):
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1"),
        _run_still_current=lambda: True,
        progress_mode="all",
        progress_grouping=grouping,
        tool_progress_enabled=tool_progress,
        progress_queue=progress_queue,
        _status_adapter=adapter,
        _status_chat_id="c1",
        _status_thread_metadata=status_metadata,
        _loop_for_step=loop,
        _cleanup_progress=cleanup_progress,
    )
    if agent is not None:
        ctx.agent_holder[0] = agent
    return ctx


async def _await_send_count(adapter, count, timeout=5.0):
    """Wait until *adapter* has recorded *count* sends (event-based)."""

    async def _wait():
        while len(adapter.sent) < count:
            event = asyncio.Event()
            adapter._waiters.append(event)
            if len(adapter.sent) >= count:
                break
            await event.wait()

    await asyncio.wait_for(_wait(), timeout)


async def _run_consumer_until_sends(runner, adapter, count):
    task = asyncio.get_running_loop().create_task(runner.send_progress_messages())
    try:
        await _await_send_count(adapter, count)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize("notice", [_CLAUDE_NOTICE, _CURSOR_NOTICE])
def test_agent_viewer_notice_rides_queue_behind_tool_row(notice, monkeypatch):
    """The branded notice is queued after the tool row, not scheduled directly."""
    import gateway.run as gateway_run

    scheduled = []

    def _capture(coro, loop, **kwargs):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(gateway_run, "safe_schedule_threadsafe", _capture)

    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    # tool_executor fires tool.started before dispatching the tool…
    runner.progress_callback(
        "tool.started", "delegate_claude_agent", "refactor the parser", {}
    )
    # …and the tool's spawn callback emits the viewer notice from that thread.
    runner._status_callback_sync("lifecycle", notice)

    items = list(progress_queue.queue)
    assert len(items) == 2
    tool_row, marker = items
    assert isinstance(tool_row, str) and "delegate_claude_agent" in tool_row
    assert gateway_run._is_agent_status_queue_marker(marker)
    assert marker[2] == notice
    assert scheduled == [], "branded notice must not take the direct status rail"


@pytest.mark.parametrize("notice", [_CLAUDE_NOTICE, _CURSOR_NOTICE])
def test_agent_viewer_notice_falls_back_to_direct_rail(notice, monkeypatch):
    """Without a usable ordered queue the notice keeps the direct status path.

    That includes a non-editable adapter with tool progress on: its consumer
    drains the queue once and returns before the tool ever spawns the run, so
    a queued marker would be stranded.
    """
    import gateway.run as gateway_run

    scheduled = []

    def _capture(coro, loop, **kwargs):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(gateway_run, "safe_schedule_threadsafe", _capture)

    for tool_progress, progress_queue, adapter in (
        (False, None, _OrderCaptureAdapter()),  # tool progress off: no queue at all
        (False, queue_mod.Queue(), _OrderCaptureAdapter()),  # queue exists, rows off
        (True, queue_mod.Queue(), _NoEditCaptureAdapter()),  # drain-once consumer
    ):
        scheduled.clear()
        ctx = _status_ctx(
            progress_queue, tool_progress=tool_progress, adapter=adapter
        )
        runner = _make_runner(ctx, adapter)

        runner._status_callback_sync("lifecycle", notice)

        assert len(scheduled) == 1, (tool_progress, progress_queue, adapter)
        if progress_queue is not None:
            assert progress_queue.empty()


def test_ordinary_lifecycle_status_keeps_direct_rail(monkeypatch):
    """Non-branded lifecycle statuses never enter the progress queue."""
    import gateway.run as gateway_run

    scheduled = []

    def _capture(coro, loop, **kwargs):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(gateway_run, "safe_schedule_threadsafe", _capture)

    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    runner._status_callback_sync("lifecycle", "⏳ Compressing context")

    assert len(scheduled) == 1
    assert progress_queue.empty()


@pytest.mark.parametrize(
    "notice,recognizer_name",
    [
        (_CLAUDE_NOTICE, "_claude_agent_status_url"),
        (_CURSOR_NOTICE, "_cursor_cloud_agent_status_url"),
    ],
)
def test_progress_consumer_delivers_tool_row_before_agent_notice(
    notice, recognizer_name
):
    """The consumer finalizes the tool row, then sends the exact notice line.

    The notice must stay byte-exact so the platform adapter still recognizes
    it (Discord converts the line into the branded viewer embed).
    """
    import plugins.platforms.discord.adapter as discord_adapter

    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    runner.progress_callback(
        "tool.started", "delegate_claude_agent", "refactor the parser", {}
    )
    runner._status_callback_sync("lifecycle", notice)

    asyncio.run(_run_consumer_until_sends(runner, adapter, 2))

    contents = [call["content"] for call in adapter.sent]
    assert len(contents) == 2, contents
    assert "delegate_claude_agent" in contents[0]
    assert notice not in contents[0]
    assert contents[1] == notice
    # The exact line still resolves for the adapter's branded rendering.
    assert getattr(discord_adapter, recognizer_name)(contents[1]) is not None
    # The pending row was finalized into its own bubble (edited), never merged
    # with the notice.
    assert adapter.edits
    assert all(notice not in edit["content"] for edit in adapter.edits)


def test_tool_row_after_agent_notice_starts_new_bubble():
    """After the notice, later tool rows must not edit the bubble above it."""
    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    runner.progress_callback("tool.started", "web_search", "alpha-query", {})
    runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)
    runner.progress_callback("tool.started", "web_search", "beta-query", {})
    # A second post-notice row: the consumer batches the pending row into one
    # delivery on the next tick, so the fresh bubble below the notice is sent.
    runner.progress_callback("tool.started", "web_search", "gamma-query", {})

    asyncio.run(_run_consumer_until_sends(runner, adapter, 3))

    contents = [call["content"] for call in adapter.sent]
    assert len(contents) == 3, contents
    assert "alpha-query" in contents[0]
    assert contents[1] == _CLAUDE_NOTICE
    # The post-notice rows went out as a FRESH bubble below the notice — sent
    # as a new message that does not replay the pre-notice row. Later edits of
    # that batch are fine (the consumer keeps editing the new bubble); what is
    # forbidden is post-notice rows landing in any edit of a message sent
    # BEFORE the notice, i.e. the bubble sitting above it.
    assert "beta-query" in contents[2]
    assert "alpha-query" not in contents[2]
    post_notice_bubble = adapter.sent[2]["message_id"]
    assert all(
        edit["message_id"] == post_notice_bubble
        for edit in adapter.edits
        if "beta-query" in edit["content"] or "gamma-query" in edit["content"]
    ), adapter.edits


def test_cancelled_consumer_drains_agent_notice_without_merging():
    """A cancelled consumer still delivers the queued notice standalone."""
    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    runner.progress_callback("tool.started", "web_search", "first", {})
    runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)

    async def _scenario():
        task = asyncio.get_running_loop().create_task(
            runner.send_progress_messages()
        )
        # Wait for the tool row to be delivered, then cancel while the notice
        # is still queued — the cancellation drain owns it from there.
        await _await_send_count(adapter, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())

    contents = [call["content"] for call in adapter.sent]
    assert contents, "the tool row must have been delivered before cancellation"
    assert contents[0] != _CLAUDE_NOTICE
    assert _CLAUDE_NOTICE in contents
    # Delivered as its own message: never merged into the editable progress
    # text nor stringified as the raw marker tuple.
    joined = "\n".join(contents + [edit["content"] for edit in adapter.edits])
    assert "__agent_status__" not in joined


def test_non_editable_adapter_notice_keeps_direct_rail_after_consumer_returns():
    """Non-editable adapters: the notice keeps the direct status rail.

    Production starts ``send_progress_messages`` BEFORE tool execution. On an
    adapter that cannot edit, the consumer drains the (still empty) queue once
    and returns — so a notice enqueued later, when the delegation tool finally
    spawns its run, would sit on the queue with no consumer left to deliver
    it. The real production order is reproduced here: the consumer runs to
    completion on an empty queue first, and only then does the status callback
    fire. The notice must still be sent, exactly once, and not be stranded.
    """
    progress_queue = queue_mod.Queue()
    adapter = _NoEditCaptureAdapter()

    async def _scenario():
        ctx = _status_ctx(
            progress_queue, adapter=adapter, loop=asyncio.get_running_loop()
        )
        runner = _make_runner(ctx, adapter)
        # A tool row queued before the consumer starts: dropped by design on
        # this adapter (every edit would be a separate bubble).
        runner.progress_callback("tool.started", "web_search", "suppressed-row", {})

        consumer = asyncio.get_running_loop().create_task(
            runner.send_progress_messages()
        )
        await consumer  # returns on its own: drain-once, nothing left running

        # The tool runs now and its spawn callback emits the viewer notice —
        # there is no progress consumer alive to drain a queued marker.
        runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)
        await _await_send_count(adapter, 1)

    asyncio.run(_scenario())

    assert [call["content"] for call in adapter.sent] == [_CLAUDE_NOTICE]
    assert progress_queue.empty(), "notice must not be left stranded on the queue"


def test_non_editable_adapter_backstop_delivers_stray_queued_marker():
    """Defense in depth: a marker that reaches this queue is still delivered.

    The producer keeps branded notices off this queue (see the test above);
    if one ever arrives anyway, the drain-once loop must send it rather than
    discard it alongside the suppressed tool rows.
    """
    import gateway.run as gateway_run

    progress_queue = queue_mod.Queue()
    adapter = _NoEditCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter)
    runner = _make_runner(ctx, adapter)

    ctx.progress_queue.put("🔍 web_search: suppressed-row")
    ctx.progress_queue.put(
        gateway_run._agent_status_queue_marker("lifecycle", _CLAUDE_NOTICE)
    )

    asyncio.run(_run_consumer_until_sends(runner, adapter, 1))

    assert [call["content"] for call in adapter.sent] == [_CLAUDE_NOTICE]
    assert progress_queue.empty()


def test_separate_grouping_pending_row_lands_before_agent_notice():
    """``separate`` grouping: a throttled pending row is sent, never dropped.

    Reproduces the verifier sequence: the first row's send opens the edit
    throttle window, the second row is buffered inside that window (it has no
    message of its own yet), and the delegation's viewer notice dequeues right
    behind it. The pending row must land first — once, and unmerged — because
    the spawned run's link would otherwise point at a tool row the user never
    saw.
    """
    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    ctx = _status_ctx(progress_queue, adapter=adapter, grouping="separate")
    runner = _make_runner(ctx, adapter)

    async def _scenario():
        runner.progress_callback("tool.started", "web_search", "first-row", {})
        task = asyncio.get_running_loop().create_task(
            runner.send_progress_messages()
        )
        try:
            await _await_send_count(adapter, 1)  # first row: its own message
            # Second row lands inside the throttle window opened by the first
            # send — buffered, not yet sent — immediately followed by the
            # viewer notice from inside the tool call.
            runner.progress_callback("tool.started", "web_search", "second-row", {})
            runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)
            await _await_send_count(adapter, 3)  # deferred row, then notice
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_scenario())

    contents = [call["content"] for call in adapter.sent]
    assert len(contents) == 3, contents
    assert "first-row" in contents[0]
    assert "second-row" in contents[1], contents
    assert contents[2] == _CLAUDE_NOTICE
    # Exactly once each: no dropped row, no duplicate row, no raw marker text.
    rendered = contents + [edit["content"] for edit in adapter.edits]
    assert sum("second-row" in line for line in rendered) == 1, rendered
    assert sum(line == _CLAUDE_NOTICE for line in rendered) == 1, rendered
    assert "__agent_status__" not in "\n".join(rendered)


def test_interrupted_agent_delivers_viewer_notice_and_suppresses_late_row():
    """Under interrupt the spawned-run notice survives; late tool rows do not.

    ``agent.is_interrupted`` used to discard every dequeued event, including
    the marker for a subprocess/cloud run that already spawned and keeps
    running after the stop. The marker is a durable status notice, so it is
    delivered exactly once — while an ordinary queued tool row stays
    suppressed, with no progress edit and no post-cancel edit at all.
    """
    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    agent = SimpleNamespace(is_interrupted=True)
    ctx = _status_ctx(
        progress_queue,
        adapter=adapter,
        agent=agent,
        cleanup_progress=True,
        status_metadata={"thread_id": "t-9"},
    )
    runner = _make_runner(ctx, adapter)

    # A row queued in the window between tool parse and interrupt processing
    # (progress_callback itself refuses to enqueue it this late).
    ctx.progress_queue.put("🔍 web_search: queued before stop landed")
    runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)

    asyncio.run(_run_consumer_until_sends(runner, adapter, 1))

    assert [call["content"] for call in adapter.sent] == [_CLAUDE_NOTICE]
    assert adapter.edits == [], "interrupted turns must not edit progress rows"
    # Same status wiring as the direct rail: thread metadata preserved and the
    # notice tracked for cleanup like any other status bubble.
    assert adapter.sent[0]["metadata"] == {"thread_id": "t-9"}
    assert ctx._cleanup_msg_ids == [adapter.sent[0]["message_id"]]


def test_interrupted_agent_cancel_drain_delivers_notice_without_late_rows():
    """Cancellation during an interrupted turn still delivers the marker once."""
    progress_queue = queue_mod.Queue()
    adapter = _OrderCaptureAdapter()
    agent = SimpleNamespace(is_interrupted=True)
    ctx = _status_ctx(progress_queue, adapter=adapter, agent=agent)
    runner = _make_runner(ctx, adapter)

    ctx.progress_queue.put("🔍 web_search: queued before stop landed")
    runner._status_callback_sync("lifecycle", _CLAUDE_NOTICE)

    async def _scenario():
        task = asyncio.get_running_loop().create_task(
            runner.send_progress_messages()
        )
        await asyncio.sleep(0)  # let the consumer enter its loop
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())

    contents = [call["content"] for call in adapter.sent]
    assert contents.count(_CLAUDE_NOTICE) == 1, contents
    assert all("queued before stop" not in line for line in contents), contents
    rendered = contents + [edit["content"] for edit in adapter.edits]
    assert "__agent_status__" not in "\n".join(rendered)


class TestTurnContext:
    def test_defaults_are_independent_containers(self):
        a, b = TurnContext(), TurnContext()
        a.last_progress_msg[0] = "x"
        a.repeat_count[0] = 3
        a._cleanup_msg_ids.append("1")
        assert b.last_progress_msg == [None]
        assert b.repeat_count == [0]
        assert b._cleanup_msg_ids == []

    def test_shared_containers_visible_to_outer_scope(self):
        # The outer body and the runner share the SAME list objects, so
        # mutation through the ctx is visible to locals captured elsewhere.
        last_progress_msg = [None]
        ctx = TurnContext(last_progress_msg=last_progress_msg)
        ctx.last_progress_msg[0] = "🔍 web_search"
        assert last_progress_msg[0] == "🔍 web_search"


class TestTurnRunner:
    def test_methods_exist_and_bind(self):
        from gateway.run import TurnRunner

        ctx = TurnContext()
        runner = _make_runner(ctx)
        assert callable(runner.progress_callback)
        assert asyncio.iscoroutinefunction(TurnRunner.send_progress_messages)
        assert runner._ctx is ctx

    def test_send_progress_messages_no_queue_returns(self):
        ctx = TurnContext(progress_queue=None)
        runner = _make_runner(ctx)
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_send_progress_messages_no_adapter_returns(self):
        ctx = TurnContext(progress_queue=queue_mod.Queue())
        runner = _make_runner(ctx)  # stub adapter resolver returns None
        assert asyncio.run(runner.send_progress_messages()) is None

    def test_context_injection_uses_tool_progress_queue_with_full_redacted_content(self):
        class _DiscordLikeAdapter:
            MAX_MESSAGE_LENGTH = 2000
            supports_code_blocks = True

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _DiscordLikeAdapter())
        secret = "ghp_" + ("a" * 36)
        content = (
            "\n\n<memory-context>\n"
            f"token={secret}\n"
            "quoted mention: <@1234567890>\n"
            + ("remembered observation\n" * 300)
            + "</memory-context>"
        )

        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {
                "content": content,
                "injected_chars": len(content) + 2,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1  # 5k-ish context is split for Discord
        assert all(len(message) <= 1936 for message in messages)  # 2000 - safety margin
        rendered = "\n".join(messages)
        assert rendered.startswith(f"🧠 memory context injected (+{len(content) + 2:,} chars)")
        assert "<memory-context>" in rendered
        assert "</memory-context>" in rendered
        assert secret not in rendered
        assert "«redacted:ghp_…»" in rendered
        assert "<@1234567890>" not in rendered
        assert "<@\u200b1234567890>" in rendered
        assert all(message.count("```") == 2 for message in messages)

        from agent.redact import redact_sensitive_text
        from gateway.stream_consumer import escape_code_fences_for_display

        displayed_chunks = []
        for message in messages:
            body = message.split("\n", 1)[1]
            displayed_chunks.append(body[4:-4])  # outer ```\n ... \n```
        expected_display = escape_code_fences_for_display(
            redact_sensitive_text(
                content,
                force=True,
                file_read=True,
                redact_url_credentials=True,
            ).replace("@", "@\u200b")
        )
        assert "".join(displayed_chunks) == expected_display

    def test_context_injection_respects_per_chat_utf16_limit(self):
        from gateway.platforms.base import BasePlatformAdapter, utf16_len

        class _Utf16Adapter(BasePlatformAdapter):
            supports_code_blocks = True
            MAX_MESSAGE_LENGTH = 4096

            def __init__(self):
                pass

            async def connect(self, *, is_reconnect=False):
                return True

            async def disconnect(self):
                return None

            async def send(self, *args, **kwargs):
                return None

            async def get_chat_info(self, chat_id):
                return {}

            def max_message_length_for_chat(self, chat_id):
                return 4096

            def message_len_fn_for_chat(self, chat_id):
                return utf16_len

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _Utf16Adapter())
        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {
                "content": "😀" * 5000,
                "injected_chars": 5000,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1
        assert all(utf16_len(message) <= 4032 for message in messages)
        displayed_chunks = []
        for message in messages:
            body = message.split("\n", 1)[1]
            displayed_chunks.append(body[4:-4])
        assert "".join(displayed_chunks) == "😀" * 5000

    def test_context_injection_respects_duck_typed_per_chat_utf16_limit(self):
        from gateway.platforms.base import utf16_len

        class _DuckUtf16Adapter:
            supports_code_blocks = True
            MAX_MESSAGE_LENGTH = 100

            def max_message_length_for_chat(self, chat_id):
                return 100

            def message_len_fn_for_chat(self, chat_id):
                return utf16_len

        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="all",
            tool_progress_enabled=True,
            progress_queue=progress_queue,
        )
        runner = _make_runner(ctx, _DuckUtf16Adapter())
        runner.progress_callback(
            "context.injected",
            "context",
            "",
            {
                "content": "😀" * 200,
                "injected_chars": 200,
                "sources": ["memory"],
            },
        )

        messages = []
        while not progress_queue.empty():
            messages.append(progress_queue.get_nowait())
        assert len(messages) > 1
        assert all(utf16_len(message) <= 100 for message in messages)

    def test_context_injection_suppresses_unrepresentable_unit_budget(self):
        from gateway.platforms.base import utf16_len
        from gateway.run import _split_context_progress_text

        assert _split_context_progress_text("😀", 1, utf16_len) == []

    @pytest.mark.parametrize("message_limit", [1, 8, 40, 64, 128])
    def test_context_injection_respects_even_tiny_adapter_caps(self, message_limit):
        from gateway.run import _format_context_injection_progress

        messages = _format_context_injection_progress(
            content="x" * 334,
            injected_chars=336,
            sources=["source-" + ("y" * 200)],
            message_limit=message_limit,
            supports_code_blocks=True,
        )
        assert messages
        assert all(len(message) <= message_limit for message in messages)

    def test_context_injection_follows_tool_progress_visibility(self):
        progress_queue = queue_mod.Queue()
        ctx = TurnContext(
            source=SimpleNamespace(chat_id="c1"),
            _run_still_current=lambda: True,
            progress_mode="off",
            tool_progress_enabled=False,
            progress_queue=progress_queue,
        )
        adapter = SimpleNamespace(MAX_MESSAGE_LENGTH=2000, supports_code_blocks=True)
        runner = _make_runner(ctx, adapter)
        runner.progress_callback(
            "context.injected",
            "context",
            None,
            {"content": "hidden", "injected_chars": 8, "sources": ["memory"]},
        )
        assert progress_queue.empty()
    def test_normal_response_preserves_compression_exhausted(self):
        """A non-empty exhaustion response must still reach auto-reset consumers."""

        class _ExhaustedAgent:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]
                self.session_id = kwargs["session_id"]
                self.tools = []
                self.context_compressor = SimpleNamespace(
                    last_prompt_tokens=0,
                    context_length=200_000,
                )
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0

            def run_conversation(self, _message, **_kwargs):
                return {
                    "final_response": "Context length exceeded. Cannot compress further.",
                    "failed": True,
                    "compression_exhausted": True,
                    "messages": [],
                }

        gateway_runner = MagicMock()
        gateway_runner.config = SimpleNamespace(streaming=None)
        gateway_runner._provider_routing = {}
        gateway_runner._agent_cache_lock = None
        gateway_runner._agent_cache = {}
        gateway_runner._session_db = None
        gateway_runner._prefill_messages = None
        gateway_runner._pending_model_notes = {}
        gateway_runner._pending_skills_reload_notes = {}
        gateway_runner.session_store._entries = {}
        gateway_runner._get_system_prompt_for_channel.return_value = None
        gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
        gateway_runner._resolve_session_reasoning_config.return_value = None
        gateway_runner._resolve_session_service_tier.return_value = None
        gateway_runner._resolve_turn_agent_config.return_value = {
            "model": "test-model",
            "runtime": {},
        }
        gateway_runner._agent_config_signature.return_value = ("test-signature",)
        gateway_runner._extract_cache_busting_config.return_value = {}
        gateway_runner._refresh_fallback_model.return_value = None
        gateway_runner._consume_pending_native_image_paths.return_value = []
        gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
        gateway_runner._is_telegram_topic_lane.return_value = False
        gateway_runner._is_discord_auto_thread_lane.return_value = False
        gateway_runner._is_relay_discord_channel_lane.return_value = False

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="test-chat",
            user_id="test-user",
        )
        ctx = TurnContext(
            source=source,
            message="continue",
            history=[],
            session_id="test-session",
            session_key="test-session-key",
            user_config={},
            AIAgent=_ExhaustedAgent,
            resolve_display_setting=lambda *_args: False,
            _run_still_current=lambda: True,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )

        from gateway.run import TurnRunner

        result = TurnRunner(gateway_runner, ctx).run_sync()

        assert result["final_response"] == (
            "Context length exceeded. Cannot compress further."
        )
        assert result["compression_exhausted"] is True
