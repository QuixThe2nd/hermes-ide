"""Discord streaming finalization must reply only on the turn-final answer.

Interim streaming previews, commentary, and tool progress stay standalone
(no MessageReference ping). The completed answer is delivered as a fresh
reply because Discord cannot attach a reference via message.edit.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
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


# Snowflake-shaped fixture IDs — distinct 17–19-digit values in the
# 2026-era Discord range.  All are synthetic; no live entity is modeled.
_ROOT_MESSAGE_ID = 1544728193026457601  # auto-thread signature: thread id == root
_PARENT_CHANNEL_ID = 1544720986135749120
_IN_THREAD_REPLY_ID = 1544728355012983264  # ordinary in-thread reply target
_ROOT_AUTHOR_ID = 271190857634097154  # root author (user snowflake)
_FETCHED_AUTHOR_ID = 462391875021346816  # author served by parent.fetch_message
_FAKE_MESSAGE_ID_BASE = 1544728400000000000

_ROOT_MENTION = f"<@{_ROOT_AUTHOR_ID}>"  # inline ping carried by chunk 0

# ``truncate_message``'s " (1/2)" continuation markers — stripped per chunk
# so content-preservation assertions compare the delivered body exactly.
_CHUNK_INDICATOR_RE = re.compile(r" \(\d+/\d+\)$")


def _recover_body(sent_messages, mention: str = _ROOT_MENTION) -> str:
    """Body as delivered: drop the one mention prefix and chunk indicators.

    Byte-exact — no whitespace normalization: the point of the recovery is
    proving the splitter's own chunk bytes (interior spaces included) are
    untouched by the mention prefix.  The mention either rides the first
    chunk (remove it plus its one separating space) or ships as its own
    first message (drop that message entirely).
    """
    parts = []
    for index, msg in enumerate(sent_messages):
        content = _CHUNK_INDICATOR_RE.sub("", msg["content"] or "")
        if index == 0:
            if content in (mention, mention + " "):
                continue  # mention-only first message
            if content.startswith(mention + " "):
                content = content[len(mention) + 1 :]
        parts.append(content)
    return "".join(parts)


def _expected_body_chunks(adapter, content) -> list:
    """Chunks the splitter itself produces for the mention-free body."""
    return adapter._cap_split_chunks(
        adapter.truncate_message(
            adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH
        )
    )


def _assert_delivered_body_chunks_exact(sent_messages, mention, expected_chunks):
    """Delivered chunks ARE the splitter's body chunks, byte-for-byte.

    The mention must not move any split boundary: either it rides chunk 0
    (chunk 0 = mention + one space + the original first chunk) or it ships
    as its own first message — in both shapes the remaining delivered
    chunks equal ``expected_chunks`` exactly, indicators and interior
    whitespace included.
    """
    delivered = [msg["content"] or "" for msg in sent_messages]
    first = delivered[0]
    if first in (mention, mention + " "):
        assert delivered[1:] == expected_chunks
    else:
        assert first.startswith(mention + " ")
        assert [first[len(mention) + 1 :]] + delivered[1:] == expected_chunks


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
    forum_parent=False,
):
    """Adapter + fakes for root-turn mention-fallback tests.

    The parent-channel @mention spawned a thread whose id equals the root
    message id, and the root message itself lives in the parent channel —
    the exact shape that makes a MessageReference unattachable.
    ``forum_parent=True`` flips the parent to a forum channel (type 15),
    where the starter message instead lives in the thread itself.

    The thread's ``send`` models Discord's real message_reference
    validation, not mere object construction: any reference whose
    ``channel_id`` differs from the send channel is rejected with the
    same 50035 the 2026-09-02 incident produced, so a cross-channel
    anchor (e.g. re-anchoring a root final to the parent channel) fails
    the send instead of silently "constructing fine".  Every attempt —
    accepted or rejected — is recorded on ``thread.send_attempts``.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", reply_to_mode=reply_to_mode)
    )
    sent_messages = []
    send_attempts = []

    async def _send(content, reference=None):
        send_attempts.append({"content": content, "reference": reference})
        if reference is not None and int(reference.channel_id) != _ROOT_MESSAGE_ID:
            raise RuntimeError(
                "400 Bad Request (error code: 50035): "
                "In message_reference: Cannot reply to a message in a "
                "different channel"
            )
        msg = SimpleNamespace(
            id=_FAKE_MESSAGE_ID_BASE + len(sent_messages) + 1,
            content=content,
            reference=reference,
        )
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    if fetch_message is None:
        fetch_message = AsyncMock(
            return_value=SimpleNamespace(
                id=_ROOT_MESSAGE_ID, author=SimpleNamespace(id=_FETCHED_AUTHOR_ID)
            )
        )
    parent = SimpleNamespace(id=_PARENT_CHANNEL_ID, fetch_message=fetch_message)
    if forum_parent:
        parent.type = 15  # forum channel — `_is_forum_parent` reads this attr
    thread = SimpleNamespace(
        id=_ROOT_MESSAGE_ID,
        parent_id=_PARENT_CHANNEL_ID,
        send=AsyncMock(side_effect=_send),
        send_attempts=send_attempts,
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
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
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
        assert sent_messages[0]["content"].startswith(_ROOT_MENTION + " ")
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1
        # Later chunks mention NO user at all — not merely "not again".
        for msg in sent_messages[1:]:
            assert "<@" not in msg["content"]

    @pytest.mark.asyncio
    async def test_author_from_recovery_db_skips_fetch_message(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert parent.fetch_message.await_count == 0
        assert sent_messages[0]["content"].startswith(_ROOT_MENTION + " ")

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
        assert sent_messages[0]["content"].startswith(
            f"<@{_FETCHED_AUTHOR_ID}> "
        )

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
            reply_to=str(_IN_THREAD_REPLY_ID),
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is not None
        assert sent_messages[0]["reference"].message_id == _IN_THREAD_REPLY_ID
        assert "<@" not in sent_messages[0]["content"]
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_forum_starter_final_references_thread_not_mention_fallback(
        self, monkeypatch, tmp_path
    ):
        """Forum post starter: reference attaches in the thread — no mention.

        The starter shares the id signature of an auto-thread root (its id
        equals the post thread id), but it lives in the thread itself, so
        the mention fallback must not fire and the reference anchors to
        the thread channel.
        """
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID), forum_parent=True
            )
        )

        result = await adapter.send(
            parent_id,
            "Final answer",
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert sent_messages[0]["reference"] is not None
        assert sent_messages[0]["reference"].message_id == _ROOT_MESSAGE_ID
        assert sent_messages[0]["reference"].channel_id == _ROOT_MESSAGE_ID
        assert "<@" not in sent_messages[0]["content"]
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_reply_to_mode_off_means_no_reference_and_no_mention(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="off", db_author_id=str(_ROOT_AUTHOR_ID)
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
        adapter, thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )

        result = await adapter.send(
            parent_id,
            "Working on it...",
            reply_to=root_id,
            metadata={"notify": True, "_interim_send": True, "thread_id": root_id},
        )

        assert result.success is True
        # Exactly one send — no second ping-carrying message hiding behind
        # the first clean one — and every attempt is mention/reference-free.
        assert len(sent_messages) == 1
        assert len(thread.send_attempts) == 1
        for attempt in thread.send_attempts:
            assert attempt["reference"] is None
            assert "<@" not in (attempt["content"] or "")
        assert sent_messages[0]["content"] == "Working on it..."
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_exact_body_preserved_at_mention_induced_boundary(
        self, monkeypatch, tmp_path
    ):
        """The exact 2000-character boundary probe (round-2 blocker 1).

        ``"A" * 1980 + " " + "B" * 19`` is exactly one unsplit message on
        its own; prefixing the 8-character mention before the split moved
        the split boundary so ``truncate_message``'s whitespace advance
        (``.lstrip()``) ate the body's interior space — logical chunk
        lengths ``[7, 1980, 19]``, recovered body 1999 chars, first
        mismatch at offset 1980.  The body is now split FIRST and the
        mention ships without moving any boundary, so the delivered body
        is byte-for-byte the original, interior space included.
        """
        adapter, thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )
        body = "A" * 1980 + " " + "B" * 19
        assert len(body) == adapter.MAX_MESSAGE_LENGTH

        result = await adapter.send(
            parent_id,
            body,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        # The mention cannot ride the single at-cap body chunk, so it
        # ships as its own first message and the body chunk is untouched.
        assert [msg["content"] for msg in sent_messages] == [
            _ROOT_MENTION + " ",
            body,
        ]
        assert _recover_body(sent_messages) == body
        # The interior space at the old loss point is intact.
        assert _recover_body(sent_messages)[1980] == " "
        _assert_delivered_body_chunks_exact(
            sent_messages, _ROOT_MENTION, _expected_body_chunks(adapter, body)
        )
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1

    @pytest.mark.asyncio
    async def test_mention_rides_short_body_byte_exact(self, monkeypatch, tmp_path):
        """A mention that fits rides chunk 0 with zero body disturbance."""
        adapter, thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )
        # 8-char prefix + body == exactly the cap: the tightest inline fit.
        body = "z" * (adapter.MAX_MESSAGE_LENGTH - len(_ROOT_MENTION) - 1)

        result = await adapter.send(
            parent_id,
            body,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert [msg["content"] for msg in sent_messages] == [
            _ROOT_MENTION + " " + body
        ]
        assert _recover_body(sent_messages) == body

    @pytest.mark.asyncio
    async def test_mention_prefix_at_cap_body_chunks_byte_for_byte(
        self, monkeypatch, tmp_path
    ):
        """An at-cap body keeps every original byte when the mention ships.

        The at-cap body is a single unsplit chunk; the mention cannot ride
        it, so it goes out as its own first message and the body chunk is
        byte-for-byte the splitter's output — no re-split, no clipped or
        whitespace-eaten tail (the earlier round lost the space after the
        mention and any displaced suffix).
        """
        adapter, thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )
        at_cap = "x" * adapter.MAX_MESSAGE_LENGTH

        result = await adapter.send(
            parent_id,
            at_cap,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        for msg in sent_messages:
            assert len(msg["content"]) <= adapter.MAX_MESSAGE_LENGTH
            assert msg["reference"] is None
        _assert_delivered_body_chunks_exact(
            sent_messages, _ROOT_MENTION, _expected_body_chunks(adapter, at_cap)
        )
        # Space-free body: recovery is byte-exact, no normalization.
        assert _recover_body(sent_messages) == at_cap
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1
        for msg in sent_messages[1:]:
            assert "<@" not in msg["content"]

    @pytest.mark.asyncio
    async def test_mention_prefix_multi_cap_content_exact_across_chunks(
        self, monkeypatch, tmp_path
    ):
        """A multi-cap root final keeps every body chunk byte-for-byte."""
        adapter, thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )
        body = "y" * (adapter.MAX_MESSAGE_LENGTH * 2 + 7)

        result = await adapter.send(
            parent_id,
            body,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) >= 3
        for msg in sent_messages:
            assert len(msg["content"]) <= adapter.MAX_MESSAGE_LENGTH
            assert msg["reference"] is None
        # The snowflake-width mention does not fit beside chunk 0's
        # indicator reserve, so it ships as its own first message —
        # without displacing so much as one byte of any body chunk.
        _assert_delivered_body_chunks_exact(
            sent_messages, _ROOT_MENTION, _expected_body_chunks(adapter, body)
        )
        # Space-free body: full recovery, byte-for-byte, indicators gone.
        assert _recover_body(sent_messages) == body
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1

    @pytest.mark.asyncio
    async def test_mention_prefix_wordy_multi_chunk_preserves_every_word(
        self, monkeypatch, tmp_path
    ):
        """Word-boundary splits keep the splitter's exact chunk bytes.

        The body chunks are compared byte-for-byte against what the
        splitter itself produces for the mention-free body — the property
        the mention prefix must not disturb — and every word of the final
        answer still ships, in order, with the mention prepended exactly
        once.
        """
        adapter, _thread, _parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(monkeypatch, tmp_path, db_author_id=str(_ROOT_AUTHOR_ID))
        )
        words = [f"word{idx:05d}" for idx in range(600)]
        content = " ".join(words)

        result = await adapter.send(
            parent_id,
            content,
            reply_to=root_id,
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        _assert_delivered_body_chunks_exact(
            sent_messages, _ROOT_MENTION, _expected_body_chunks(adapter, content)
        )
        # Every word ships, in order — recovered by pattern, because the
        # splitter drops the whitespace it splits on (adjacent words
        # across a boundary concatenate) yet never loses a word or any of
        # its letters.
        assert re.findall(r"word\d{5}", _recover_body(sent_messages)) == words
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1


class TestDiscordRootTurnAllModeReplyReference:
    """reply_to_mode=all root turns: exactly one mention, never a cross-channel
    reference.

    A text-channel auto-thread holds no user-authored message to reply to —
    the root lives in the parent channel — so anchoring the 'all'-mode
    reference there made Discord reject the entire final (50035 "Cannot
    reply to a message in a different channel"; the 2026-09-02 incident).
    The fake thread ``send`` in the harness enforces that validation, so
    the pre-fix parent-anchored reference fails these tests outright.
    """

    @pytest.mark.asyncio
    async def test_root_turn_final_mentions_once_and_never_builds_reference(
        self, monkeypatch, tmp_path
    ):
        adapter, thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="all", db_author_id=str(_ROOT_AUTHOR_ID)
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
        # Discord's validation never fired: no send attempt — successful or
        # rejected — ever carried a reference anchored outside the thread.
        assert thread.send_attempts
        for attempt in thread.send_attempts:
            assert attempt["reference"] is None
        for msg in sent_messages:
            assert msg["reference"] is None
        # Exactly one inline mention of the root author, on the first chunk;
        # later chunks carry neither mention nor reference.
        assert sent_messages[0]["content"].startswith(_ROOT_MENTION + " ")
        assert sum(msg["content"].count(_ROOT_MENTION) for msg in sent_messages) == 1
        for msg in sent_messages[1:]:
            assert "<@" not in msg["content"]
        # The author came from the recovery ledger — no API round trip.
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_in_thread_final_all_mode_references_every_chunk(
        self, monkeypatch, tmp_path
    ):
        """Ordinary in-thread finals keep real reply chips in 'all' mode.

        A reply_to that is NOT the root-turn signature (its id differs from
        the thread id) is a normal in-thread message: every chunk of the
        final is a real reply anchored to the send channel, with no inline
        mention.
        """
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="all", db_author_id=str(_ROOT_AUTHOR_ID)
            )
        )
        long_answer = ("answer sentence. " * 300).strip()

        result = await adapter.send(
            parent_id,
            long_answer,
            reply_to=str(_IN_THREAD_REPLY_ID),
            metadata={"notify": True, "thread_id": root_id},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for msg in sent_messages:
            reference = msg["reference"]
            assert reference is not None
            assert reference.message_id == _IN_THREAD_REPLY_ID
            # Anchored to the send channel — the fake send would have
            # rejected anything else.
            assert reference.channel_id == _ROOT_MESSAGE_ID
            assert reference.fail_if_not_exists is False
            assert "<@" not in msg["content"]
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_forum_starter_final_stays_anchored_to_post_thread(
        self, monkeypatch, tmp_path
    ):
        """Forum post starter in all mode: reference stays in the thread.

        The starter matches the auto-thread signature (id == thread id)
        with a parent to re-anchor to, but re-anchoring would point the
        reference at a forum channel that does not contain the starter
        message — every chunk must reference the thread itself.
        """
        adapter, _thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch,
                tmp_path,
                reply_to_mode="all",
                db_author_id=str(_ROOT_AUTHOR_ID),
                forum_parent=True,
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
            assert reference.channel_id == _ROOT_MESSAGE_ID
            assert reference.fail_if_not_exists is False
            assert "<@" not in msg["content"]
        assert parent.fetch_message.await_count == 0

    @pytest.mark.asyncio
    async def test_interim_send_stays_standalone_in_all_mode(
        self, monkeypatch, tmp_path
    ):
        adapter, thread, parent, sent_messages, root_id, parent_id = (
            _make_root_turn_harness(
                monkeypatch, tmp_path, reply_to_mode="all", db_author_id=str(_ROOT_AUTHOR_ID)
            )
        )

        result = await adapter.send(
            parent_id,
            "Working on it...",
            reply_to=root_id,
            metadata={"notify": True, "_interim_send": True, "thread_id": root_id},
        )

        assert result.success is True
        # Exactly one send — no second ping-carrying message hiding behind
        # the first clean one — and every attempt is mention/reference-free
        # even in 'all' mode.
        assert len(sent_messages) == 1
        assert len(thread.send_attempts) == 1
        for attempt in thread.send_attempts:
            assert attempt["reference"] is None
            assert "<@" not in (attempt["content"] or "")
        assert parent.fetch_message.await_count == 0


# ---------------------------------------------------------------------------
# Cold-cache parent resolution — fetched threads have no cached parent
# ---------------------------------------------------------------------------


def _make_cold_cache_harness(
    monkeypatch,
    tmp_path,
    *,
    reply_to_mode,
    forum_parent,
    fail_parent_fetch=0,
    parent_fetch_message=None,
):
    """Root-turn fakes modeling a thread fetched cold from the API.

    ``send`` resolves its thread through ``fetch_channel`` when the client
    cache misses, and in discord.py 2.7.1 a fetched thread resolves
    ``parent`` through its guild's channel cache — empty for the guild
    the ``fetch_channel`` factory builds.  Modeled here by a thread with
    NO ``parent`` attribute and a ``get_channel`` that misses the parent
    id, so the forum classification can only come from the ``fetch_channel``
    fallback.  That fetch returns a ``discord.ForumChannel`` instance —
    the exact class production's ``_is_forum_parent`` isinstance-checks —
    rather than a mock attribute that merely looks forum-shaped.

    ``fail_parent_fetch`` makes the first N parent lookups raise a
    transient error (the parent stays unresolvable → unknown tri-state).
    ``parent_fetch_message`` attaches a ``fetch_message`` to a text parent
    so the root-author API fallback can be exercised cold.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", reply_to_mode=reply_to_mode)
    )
    sent_messages = []
    send_attempts = []

    async def _send(content=None, reference=None, **_):
        # Same Discord-side validation as the root-turn harness: a
        # message_reference anchored outside the send channel is a 50035.
        send_attempts.append({"content": content, "reference": reference})
        if reference is not None and int(reference.channel_id) != _ROOT_MESSAGE_ID:
            raise RuntimeError(
                "400 Bad Request (error code: 50035): "
                "In message_reference: Cannot reply to a message in a "
                "different channel"
            )
        msg = SimpleNamespace(
            id=_FAKE_MESSAGE_ID_BASE + len(sent_messages) + 1,
            content=content,
            reference=reference,
        )
        sent_messages.append({"content": content, "reference": reference, "id": msg.id})
        return msg

    if forum_parent:
        parent = discord.ForumChannel()
        parent.id = _PARENT_CHANNEL_ID
    else:
        # ChannelType.text (0) — a plain parent channel, never a forum.
        parent = SimpleNamespace(id=_PARENT_CHANNEL_ID, type=0)
        if parent_fetch_message is not None:
            parent.fetch_message = parent_fetch_message

    # The cold thread: no `parent` attribute at all, like a Thread whose
    # guild channel cache is empty.
    thread = SimpleNamespace(
        id=_ROOT_MESSAGE_ID,
        parent_id=_PARENT_CHANNEL_ID,
        send=AsyncMock(side_effect=_send),
        send_attempts=send_attempts,
    )

    def get_channel(channel_id):
        # Cold client cache — every lookup misses.
        return None

    parent_fetch_state = {"failed": 0}

    async def fetch_channel(channel_id):
        channel_id = int(channel_id)
        if channel_id == _ROOT_MESSAGE_ID:
            return thread
        if channel_id == _PARENT_CHANNEL_ID:
            if parent_fetch_state["failed"] < fail_parent_fetch:
                parent_fetch_state["failed"] += 1
                raise RuntimeError("503 Service Unavailable (transient)")
            return parent
        return None

    adapter._client = SimpleNamespace(
        get_channel=MagicMock(side_effect=get_channel),
        fetch_channel=AsyncMock(side_effect=fetch_channel),
    )
    adapter.format_message = lambda content: content
    return adapter, thread, sent_messages


def _parent_fetch_count(adapter):
    """How many times the parent channel was fetched over the API."""
    return sum(
        1
        for call in adapter._client.fetch_channel.await_args_list
        if call.args == (int(_PARENT_CHANNEL_ID),)
    )


class TestDiscordRootTurnColdCacheParentResolution:
    """Forum starters must stay thread-referenced even from a cold cache.

    ``send`` may resolve its thread through ``fetch_channel``; the fetched
    thread has no cached parent, and ``client.get_channel(parent_id)``
    misses when the parent channel is uncached too.  The forum-parent
    check used to answer "not forum" there, treating a forum starter like
    an auto-thread root and re-anchoring its reply to the forum channel —
    where the starter message does not live, so Discord silently dropped
    the reply ping.
    """

    @pytest.mark.asyncio
    async def test_forum_starter_cold_cache_references_thread_on_every_chunk_all_mode(
        self, monkeypatch, tmp_path
    ):
        adapter, thread, sent_messages = _make_cold_cache_harness(
            monkeypatch, tmp_path, reply_to_mode="all", forum_parent=True
        )
        # The cold-cache shape the fix exists for.
        assert not hasattr(thread, "parent")
        assert adapter._client.get_channel(int(_PARENT_CHANNEL_ID)) is None

        long_answer = ("answer sentence. " * 300).strip()
        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            long_answer,
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for msg in sent_messages:
            reference = msg["reference"]
            assert reference is not None
            assert reference.message_id == _ROOT_MESSAGE_ID
            # The post thread holds the starter — never the forum channel.
            assert reference.channel_id == _ROOT_MESSAGE_ID
            assert reference.fail_if_not_exists is False
            assert "<@" not in (msg["content"] or "")
        # The parent was resolved over the API exactly once for the send
        # (thread fetch + one parent fetch, memoized).
        assert _parent_fetch_count(adapter) == 1

    @pytest.mark.asyncio
    async def test_forum_starter_cold_cache_first_mode_references_thread_without_mention(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, sent_messages = _make_cold_cache_harness(
            monkeypatch, tmp_path, reply_to_mode="first", forum_parent=True
        )

        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            ("answer sentence. " * 300).strip(),
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        first_reference = sent_messages[0]["reference"]
        assert first_reference is not None
        assert first_reference.message_id == _ROOT_MESSAGE_ID
        assert first_reference.channel_id == _ROOT_MESSAGE_ID
        for msg in sent_messages[1:]:
            assert msg["reference"] is None
        # The reference attaches — the inline-mention fallback must not fire.
        assert all("<@" not in (m["content"] or "") for m in sent_messages)
        assert _parent_fetch_count(adapter) == 1

    @pytest.mark.asyncio
    async def test_cold_cache_text_parent_all_mode_mentions_once_without_reference(
        self, monkeypatch, tmp_path
    ):
        """Cold text-channel parents must stay classified as root turns.

        The API-resolved parent is a plain text channel here — the lookup
        that recovers forum starters must not over-classify and swallow
        the mention fallback for ordinary auto-threads.  The final still
        carries no reference in 'all' mode: re-anchoring to the parent is
        the cross-channel 50035 the fake send rejects.
        """
        adapter, thread, sent_messages = _make_cold_cache_harness(
            monkeypatch, tmp_path, reply_to_mode="all", forum_parent=False
        )
        _seed_recovery_author(adapter, _ROOT_MESSAGE_ID, _ROOT_AUTHOR_ID)

        long_answer = ("answer sentence. " * 300).strip()
        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            long_answer,
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for attempt in thread.send_attempts:
            assert attempt["reference"] is None
        for msg in sent_messages:
            assert msg["reference"] is None
        assert sent_messages[0]["content"].startswith(_ROOT_MENTION + " ")
        assert sum(_ROOT_MENTION in (m["content"] or "") for m in sent_messages) == 1
        for msg in sent_messages[1:]:
            assert "<@" not in (msg["content"] or "")
        assert _parent_fetch_count(adapter) == 1

    @pytest.mark.asyncio
    async def test_cold_cache_text_parent_first_mode_keeps_mention_fallback(
        self, monkeypatch, tmp_path
    ):
        adapter, _thread, sent_messages = _make_cold_cache_harness(
            monkeypatch, tmp_path, reply_to_mode="first", forum_parent=False
        )
        _seed_recovery_author(adapter, _ROOT_MESSAGE_ID, _ROOT_AUTHOR_ID)

        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            ("answer sentence. " * 300).strip(),
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for msg in sent_messages:
            assert msg["reference"] is None
        assert sent_messages[0]["content"].startswith(_ROOT_MENTION + " ")
        assert sum(_ROOT_MENTION in (m["content"] or "") for m in sent_messages) == 1
        # Later chunks mention NO user at all — not merely "not again".
        for msg in sent_messages[1:]:
            assert "<@" not in (msg["content"] or "")
        # One parent fetch served both the reference build and the mention
        # fallback's re-check — the memoized answer, not a second API call.
        assert _parent_fetch_count(adapter) == 1

    @pytest.mark.asyncio
    async def test_cold_text_root_no_db_author_fetches_parent_message_once_for_mention(
        self, monkeypatch, tmp_path
    ):
        """Cold caches + no ledger author: the fetched parent is REUSED.

        The forum classification resolves the parent over the API; the
        author lookup used to only ``get_channel()`` — missing exactly in
        this shape — so a cold text-root final shipped with no ping at
        all.  The shared parent resolution means ``parent.fetch_message``
        runs exactly once and the final carries exactly one first-chunk
        root-author mention.
        """
        fetch_message = AsyncMock(
            return_value=SimpleNamespace(
                id=_ROOT_MESSAGE_ID, author=SimpleNamespace(id=_FETCHED_AUTHOR_ID)
            )
        )
        adapter, thread, sent_messages = _make_cold_cache_harness(
            monkeypatch,
            tmp_path,
            reply_to_mode="first",
            forum_parent=False,
            parent_fetch_message=fetch_message,
        )

        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            ("answer sentence. " * 300).strip(),
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        fetch_message.assert_awaited_once_with(_ROOT_MESSAGE_ID)
        # The classification's parent fetch is the ONLY fetch — the author
        # lookup reused the resolved parent instead of re-resolving it.
        assert _parent_fetch_count(adapter) == 1
        for attempt in thread.send_attempts:
            assert attempt["reference"] is None
        assert sent_messages[0]["content"].startswith(
            f"<@{_FETCHED_AUTHOR_ID}> "
        )
        assert sum(f"<@{_FETCHED_AUTHOR_ID}>" in (m["content"] or "") for m in sent_messages) == 1
        for msg in sent_messages[1:]:
            assert "<@" not in (msg["content"] or "")

    @pytest.mark.asyncio
    async def test_transient_parent_fetch_failure_unknown_forum_keeps_thread_reference(
        self, monkeypatch, tmp_path
    ):
        """A failed parent lookup must not masquerade as a text root.

        ``fetch_channel(parent_id)`` can fail transiently; swallowing that
        as "not forum" used to strip a valid forum starter's reply chip
        and risked inline-mentioning it.  The chosen unknown fallback is
        the weakest safe classification — NOT a text root — so the
        reference stays anchored to the send channel (for a forum starter
        that is the correct, attaching chip) and the mention stays
        suppressed.  The failure is never cached.
        """
        adapter, thread, sent_messages = _make_cold_cache_harness(
            monkeypatch,
            tmp_path,
            reply_to_mode="all",
            forum_parent=True,
            fail_parent_fetch=1,
        )

        long_answer = ("answer sentence. " * 300).strip()
        result = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            long_answer,
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert result.success is True
        assert len(sent_messages) >= 2
        for msg in sent_messages:
            reference = msg["reference"]
            assert reference is not None
            assert reference.message_id == _ROOT_MESSAGE_ID
            assert reference.channel_id == _ROOT_MESSAGE_ID
            assert "<@" not in (msg["content"] or "")
        # Unknown is not a cached answer: neither cache holds the parent.
        assert _PARENT_CHANNEL_ID not in adapter._root_turn_parent_forum_cache
        assert _PARENT_CHANNEL_ID not in adapter._root_turn_parent_channel_cache
        assert _parent_fetch_count(adapter) == 1

    @pytest.mark.asyncio
    async def test_transient_parent_fetch_failure_unknown_text_retries_next_send(
        self, monkeypatch, tmp_path
    ):
        """Unknown text parent: safe standalone-reference fallback, then retry.

        For a genuinely text root whose parent is unresolvable, the chosen
        unknown fallback keeps the thread-anchored reference — Discord
        either silently drops it (``fail_if_not_exists=False``) or rejects
        it, and the send-side retry delivers standalone; either way the
        final ships with no cross-channel 50035 and no mention.  Because
        the failure is uncached, the next send re-resolves the parent and
        the mention fallback fires properly.
        """
        adapter, _thread, sent_messages = _make_cold_cache_harness(
            monkeypatch,
            tmp_path,
            reply_to_mode="first",
            forum_parent=False,
            fail_parent_fetch=1,
        )
        _seed_recovery_author(adapter, _ROOT_MESSAGE_ID, _ROOT_AUTHOR_ID)

        first = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            "First final",
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert first.success is True
        assert len(sent_messages) == 1
        # Weakest safe classification: thread-anchored reference kept (the
        # fake send accepts same-channel references), mention suppressed.
        reference = sent_messages[0]["reference"]
        assert reference is not None
        assert reference.channel_id == _ROOT_MESSAGE_ID
        assert "<@" not in (sent_messages[0]["content"] or "")
        assert _parent_fetch_count(adapter) == 1

        second = await adapter.send(
            str(_PARENT_CHANNEL_ID),
            "Second final",
            reply_to=str(_ROOT_MESSAGE_ID),
            metadata={"notify": True, "thread_id": str(_ROOT_MESSAGE_ID)},
        )

        assert second.success is True
        # The transient failure was not cached: this send re-fetched the
        # parent, classified it as text, and pinged the root author.
        assert _parent_fetch_count(adapter) == 2
        assert sent_messages[1]["reference"] is None
        assert sent_messages[1]["content"].startswith(_ROOT_MENTION + " ")
        assert "<@" not in (sent_messages[0]["content"] or "")
