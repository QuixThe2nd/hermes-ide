"""Discord streaming finalization must reply only on the turn-final answer.

Interim streaming previews, commentary, and tool progress stay standalone
(no MessageReference ping). The completed answer is delivered as a fresh
reply because Discord cannot attach a reference via message.edit.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _make_channel():
    sent_messages = []
    delete_calls = []

    async def _send(content, reference=None):
        msg = SimpleNamespace(id=len(sent_messages) + 100, content=content, reference=reference)
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    partial_messages = {}

    def get_partial_message(mid):
        mid = int(mid)
        if mid not in partial_messages:
            async def _delete():
                delete_calls.append(mid)

            partial_messages[mid] = SimpleNamespace(id=mid, delete=AsyncMock(side_effect=_delete))
        return partial_messages[mid]

    channel = SimpleNamespace(
        id=555,
        send=AsyncMock(side_effect=_send),
        get_partial_message=get_partial_message,
    )
    return channel, sent_messages, delete_calls


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", reply_to_mode="first")
    adapter = DiscordAdapter(config)
    channel, sent_messages, delete_calls = _make_channel()
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    adapter.truncate_message = lambda content, max_len, **kw: [content]
    adapter.format_message = lambda content: content
    return adapter, channel, sent_messages, delete_calls


def _make_split_capable_adapter():
    """Discord adapter harness that preserves real chunking for overflow splits."""
    config = PlatformConfig(enabled=True, token="test-token", reply_to_mode="first")
    adapter = DiscordAdapter(config)
    sent_messages = []
    delete_calls = []
    edit_calls = []

    async def _send(content, reference=None):
        msg = SimpleNamespace(id=len(sent_messages) + 100, content=content, reference=reference)
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    partial_messages = {}

    def get_partial_message(mid):
        mid = int(mid)

        async def _edit(*, content):
            edit_calls.append({"id": mid, "content": content})

        async def _delete():
            delete_calls.append(mid)

        if mid not in partial_messages:
            partial_messages[mid] = SimpleNamespace(
                id=mid,
                edit=AsyncMock(side_effect=_edit),
                delete=AsyncMock(side_effect=_delete),
            )
        return partial_messages[mid]

    channel = SimpleNamespace(
        id=555,
        send=AsyncMock(side_effect=_send),
        get_partial_message=get_partial_message,
    )
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    adapter.format_message = lambda content: content
    return adapter, channel, sent_messages, delete_calls, edit_calls


class TestDiscordReplyReferenceGating:
    @pytest.mark.asyncio
    async def test_streaming_preview_send_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "partial answer",
            reply_to="999",
            metadata={"expect_edits": True},
        )

        assert result.success is True
        assert len(sent_messages) == 1
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_interim_commentary_send_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "Using browser tool...",
            reply_to="999",
            metadata={"_interim_send": True},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_turn_final_notify_send_has_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()

        result = await adapter.send(
            "555",
            "Final answer",
            reply_to="999",
            metadata={"notify": True},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is not None
        assert sent_messages[0]["reference"].message_id == 999


class TestDiscordStreamConsumerFreshFinal:
    @pytest.mark.asyncio
    async def test_finalize_deletes_preview_and_sends_reply(self):
        adapter, channel, sent_messages, delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="▉",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        consumer.on_delta("Preview text")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Final answer text")
        await task

        assert len(sent_messages) == 2
        assert sent_messages[0]["reference"] is None
        assert sent_messages[1]["reference"] is not None
        assert sent_messages[1]["reference"].message_id == 999
        assert sent_messages[1]["content"] == "Final answer text"
        assert delete_calls == [sent_messages[0]["id"]]
        assert consumer.final_response_sent is True

    @pytest.mark.asyncio
    async def test_commentary_before_tools_has_no_reply_reference(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        await consumer._send_commentary("I'll check that for you.")
        consumer.on_delta("Done.")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Done.")
        await task

        assert len(sent_messages) == 3
        assert all(msg["reference"] is None for msg in sent_messages[:2])
        assert sent_messages[2]["reference"] is not None
        assert sent_messages[2]["reference"].message_id == 999

    @pytest.mark.asyncio
    async def test_tool_boundary_preamble_has_no_reply_before_turn_final(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=500,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        # Short preamble cut off by a tool call before any interval flush.
        consumer.on_delta("I'll look that up")
        consumer.on_segment_break()
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.on_delta("The answer is 42.")
        consumer.finish("The answer is 42.")
        await task

        assert len(sent_messages) >= 2
        assert sent_messages[0]["reference"] is None
        assert sent_messages[0]["content"] == "I'll look that up"
        assert sent_messages[-1]["reference"] is not None
        assert sent_messages[-1]["reference"].message_id == 999
        assert sent_messages[-1]["content"] == "The answer is 42."

    @pytest.mark.asyncio
    async def test_failed_fresh_final_does_not_suppress_gateway_reply(self):
        adapter, _channel, sent_messages, _delete_calls = _make_adapter()
        from gateway.platforms.base import SendResult

        send_results = [
            SendResult(success=True, message_id="100"),
            SendResult(success=False, error="fresh final failed"),
        ]

        async def flaky_send(*, chat_id, content, reply_to=None, metadata=None):
            result = send_results.pop(0)
            if result.success:
                ref = None
                if metadata and metadata.get("notify") and reply_to:
                    import discord
                    ref = discord.MessageReference(
                        message_id=int(reply_to),
                        channel_id=555,
                        fail_if_not_exists=False,
                    )
                msg = SimpleNamespace(
                    id=int(result.message_id),
                    content=content,
                    reference=ref,
                )
                sent_messages.append(
                    {"content": content, "reference": ref, "id": msg.id}
                )
            return result

        adapter.send = AsyncMock(side_effect=flaky_send)
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        consumer.on_delta("Final answer")
        task = __import__("asyncio").create_task(consumer.run())
        await __import__("asyncio").sleep(0.05)
        consumer.finish("Final answer")
        await task

        assert consumer.final_response_sent is False
        assert consumer.final_content_delivered is False
        assert len(sent_messages) == 1
        assert sent_messages[0]["reference"] is None

    @pytest.mark.asyncio
    async def test_split_turn_final_sends_single_fresh_reply_with_reference(self):
        adapter, _channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        notify_send_attempts = []
        real_send = adapter.send

        async def tracked_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                notify_send_attempts.append(kwargs)
            return await real_send(*args, **kwargs)

        adapter.send = tracked_send
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        # Past Discord's ~1900-char streaming safe limit → sealed head + tail.
        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        pre_finish_heads = list(sent_messages[:-1])
        tail_id = sent_messages[-1]["id"]
        head_ids = {msg["id"] for msg in pre_finish_heads}
        pre_finish_delete_count = len(delete_calls)
        pre_finish_edit_count = len(edit_calls)
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is True
        assert consumer.final_content_delivered is True
        assert len(notify_send_attempts) == 1
        assert notify_send_attempts[0]["reply_to"] == "999"
        assert notify_send_attempts[0]["content"] == long_text
        notify_sends = [msg for msg in sent_messages if msg["reference"] is not None]
        assert len(notify_sends) == 1
        assert notify_sends[0]["reference"].message_id == 999
        post_finish_deletes = delete_calls[pre_finish_delete_count:]
        assert head_ids.issubset(post_finish_deletes)
        assert tail_id in post_finish_deletes
        assert edit_calls[pre_finish_edit_count:] == []

    @pytest.mark.asyncio
    async def test_split_turn_final_fresh_failure_leaves_gateway_fallback(self):
        adapter, _channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        from gateway.platforms.base import SendResult

        real_send = adapter.send

        async def flaky_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                return SendResult(success=False, error="fresh final failed")
            return await real_send(*args, **kwargs)

        adapter.send = flaky_send
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        head_ids = {msg["id"] for msg in sent_messages[:-1]}
        pre_finish_delete_count = len(delete_calls)
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is False
        assert head_ids.isdisjoint(delete_calls[pre_finish_delete_count:])

    @pytest.mark.asyncio
    async def test_split_turn_final_cleanup_failure_still_delivered(self):
        adapter, channel, sent_messages, delete_calls, edit_calls = (
            _make_split_capable_adapter()
        )
        notify_send_attempts = []
        real_send = adapter.send
        real_get_partial = channel.get_partial_message
        failing_head_id = None

        async def tracked_send(*args, **kwargs):
            metadata = kwargs.get("metadata") or {}
            if metadata.get("notify"):
                notify_send_attempts.append(kwargs)
            return await real_send(*args, **kwargs)

        def get_partial_message(mid):
            nonlocal failing_head_id
            partial = real_get_partial(mid)
            if failing_head_id is not None and int(mid) == failing_head_id:
                async def _delete():
                    delete_calls.append(mid)
                    raise RuntimeError("delete failed")

                partial.delete = AsyncMock(side_effect=_delete)
            return partial

        adapter.send = tracked_send
        channel.get_partial_message = get_partial_message
        cfg = StreamConsumerConfig(
            transport="auto",
            chat_type="dm",
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
            fresh_final_after_seconds=0.0,
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "555",
            cfg,
            initial_reply_to_id="999",
        )

        long_text = ("paragraph " * 250).strip()
        assert len(long_text) > 1900

        task = __import__("asyncio").create_task(consumer.run())
        for offset in range(0, len(long_text), 200):
            consumer.on_delta(long_text[offset : offset + 200])
            await __import__("asyncio").sleep(0.005)
        await __import__("asyncio").sleep(0.1)
        assert consumer._turn_split_delivery is True
        failing_head_id = sent_messages[0]["id"]
        consumer.finish(long_text)
        await task

        assert consumer.final_response_sent is True
        assert consumer.final_content_delivered is True
        assert len(notify_send_attempts) == 1


# ---------------------------------------------------------------------------
# Root-turn mention fallback — auto-threaded finals can't attach a reference
# ---------------------------------------------------------------------------


_ROOT_MESSAGE_ID = 9001  # auto-thread signature: thread id == root message id
_PARENT_CHANNEL_ID = 777


def _seed_recovery_author(adapter, message_id, author_id):
    """Insert a root-message row so the recovery-ledger read path hits."""

    def _op(conn):
        conn.execute(
            "INSERT OR REPLACE INTO discord_messages "
            "(message_id, author_id, status, replied, updated_at) "
            "VALUES (?, ?, 'processing', 0, '2026-08-28T00:00:00+00:00')",
            (str(message_id), str(author_id)),
        )

    adapter._with_discord_recovery_db(_op)


def _make_root_turn_harness(
    monkeypatch,
    tmp_path,
    *,
    reply_to_mode="first",
    db_author_id=None,
    fetch_message=None,
):
    """Adapter + fakes for root-turn mention-fallback tests.

    The parent-channel @mention spawned a thread whose id equals the root
    message id, and the root message itself lives in the parent channel —
    the exact shape that makes a MessageReference unattachable.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", reply_to_mode=reply_to_mode)
    )
    sent_messages = []

    async def _send(content, reference=None):
        msg = SimpleNamespace(id=len(sent_messages) + 100, content=content, reference=reference)
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    if fetch_message is None:
        fetch_message = AsyncMock(
            return_value=SimpleNamespace(
                id=_ROOT_MESSAGE_ID, author=SimpleNamespace(id=5150)
            )
        )
    parent = SimpleNamespace(id=_PARENT_CHANNEL_ID, fetch_message=fetch_message)
    thread = SimpleNamespace(
        id=_ROOT_MESSAGE_ID,
        parent_id=_PARENT_CHANNEL_ID,
        send=AsyncMock(side_effect=_send),
    )

    def get_channel(channel_id):
        channel_id = int(channel_id)
        if channel_id == _PARENT_CHANNEL_ID:
            return parent
        if channel_id == _ROOT_MESSAGE_ID:
            return thread
        return None

    adapter._client = SimpleNamespace(
        get_channel=MagicMock(side_effect=get_channel),
        fetch_channel=AsyncMock(),
    )
    adapter.format_message = lambda content: content
    if db_author_id is not None:
        _seed_recovery_author(adapter, _ROOT_MESSAGE_ID, db_author_id)
    return (
        adapter,
        thread,
        parent,
        sent_messages,
        str(_ROOT_MESSAGE_ID),
        str(_PARENT_CHANNEL_ID),
    )


class TestDiscordRootTurnMentionFallback:
    @pytest.mark.asyncio
    async def test_root_turn_final_mentions_once_with_no_reference(
        self, monkeypatch, tmp_path
    ):
        """Root-turn final: reference can't attach → inline mention, chunk 0 only."""
        adapter, _thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id="4242")
        )
        long_answer = ("answer sentence. " * 300).strip()

        result = await adapter.send(
            parent_id,
            long_answer,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        assert all(msg["reference"] is None for msg in sent_messages)
        assert sent_messages[0]["content"].startswith("<@4242> ")
        assert sum(msg["content"].count("<@4242>") for msg in sent_messages) == 1

    @pytest.mark.asyncio
    async def test_author_from_recovery_db_skips_fetch_message(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id="4242")
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert parent.fetch_message.await_count == 0
        assert sent_messages[0]["content"].startswith("<@4242> ")

    @pytest.mark.asyncio
    async def test_author_missing_from_db_falls_back_to_fetch_message(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path)
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        parent.fetch_message.assert_awaited_once_with(_ROOT_MESSAGE_ID)
        assert sent_messages[0]["content"].startswith("<@5150> ")

    @pytest.mark.asyncio
    async def test_both_author_lookups_fail_sends_clean(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch,
                tmp_path,
                fetch_message=AsyncMock(side_effect=RuntimeError("404 Unknown Message")),
            )
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert parent.fetch_message.await_count == 1
        assert sent_messages[0]["reference"] is None
        assert sent_messages[0]["content"] == "Final answer"

    @pytest.mark.asyncio
    async def test_in_thread_final_keeps_reference_and_no_mention(
        self, monkeypatch, tmp_path
    ):
        """A normal in-thread reply_to (≠ channel id) still reply-references."""
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path)
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to="8888",
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is not None
        assert sent_messages[0]["reference"].message_id == 8888
        assert "<@" not in sent_messages[0]["content"]
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_reply_to_mode_off_means_no_reference_and_no_mention(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="off", db_author_id="4242"
            )
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is None
        assert sent_messages[0]["content"] == "Final answer"
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_interim_send_never_mentions(self, monkeypatch, tmp_path):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id="4242")
        )

        result = await adapter.send(
            parent_id,
            "Working on it...",
            reply_to=root_id,
            metadata={"notify": True, "_interim_send": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is None
        assert sent_messages[0]["content"] == "Working on it..."
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_at_cap_chunk_stays_within_max_length_after_prefix(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id="4242")
        )
        at_cap = "x" * adapter.MAX_MESSAGE_LENGTH

        result = await adapter.send(
            parent_id,
            at_cap,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) == 1
        assert len(sent_messages[0]["content"]) <= adapter.MAX_MESSAGE_LENGTH
        assert sent_messages[0]["content"].startswith("<@4242> ")


class TestDiscordRootTurnAllModeReplyReference:
    """reply_to_mode=all: root-turn finals are real replies, not standalone pings.

    The inline-mention fallback is a 'first'-mode mechanism; 'all' anchors a
    MessageReference to the parent channel — where the root message actually
    lives — on every chunk of the final.
    """

    @pytest.mark.asyncio
    async def test_root_turn_final_references_parent_channel_on_every_chunk(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="all", db_author_id="4242"
            )
        )
        long_answer = ("answer sentence. " * 300).strip()

        result = await adapter.send(
            parent_id,
            long_answer,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for msg in sent_messages:
            reference = msg["reference"]
            assert reference is not None
            assert reference.message_id == _ROOT_MESSAGE_ID
            assert reference.channel_id == _PARENT_CHANNEL_ID
            assert reference.fail_if_not_exists is False
            assert "<@" not in msg["content"]
        # The reference is ids-built — no author lookup round trip.
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_interim_send_stays_standalone_in_all_mode(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="all", db_author_id="4242"
            )
        )

        result = await adapter.send(
            parent_id,
            "Working on it...",
            reply_to=root_id,
            metadata={"notify": True, "_interim_send": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is None
        assert "<@" not in sent_messages[0]["content"]
        assert parent.fetch_message.await_count == 0
