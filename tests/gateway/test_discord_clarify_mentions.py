"""Discord clarify prompts can @mention the requesting user."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from plugins.platforms.discord.adapter import DiscordAdapter, _apply_yaml_config


def _make_adapter(*, extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter


class _FakeAllowedMentions:
    def __init__(self, *, users=False, roles=False, everyone=False, replied_user=False):
        self.users = users
        self.roles = roles
        self.everyone = everyone
        self.replied_user = replied_user


class _FakeDiscordObject:
    def __init__(self, *, id):
        self.id = id


@pytest.fixture(autouse=True)
def _mention_value_types(monkeypatch):
    """Inspect Discord's allowlist values rather than MagicMock implementation details."""
    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.discord.AllowedMentions", _FakeAllowedMentions)
    monkeypatch.setattr("plugins.platforms.discord.adapter.discord.Object", _FakeDiscordObject)


class _RecordingClarifyAdapter:
    def __init__(self):
        self.kwargs = None

    def pause_typing_for_chat(self, chat_id):
        return None

    def resume_typing_for_chat(self, chat_id):
        return None

    async def send_clarify(self, **kwargs):
        self.kwargs = kwargs
        return SendResult(success=True, message_id="1.2")


@pytest.mark.asyncio
async def test_clarify_mentions_requester_by_default(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color <@999888777666555444> @everyone",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
        metadata={"mention_user_id": "111222333444555666"},
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"].startswith("<@111222333444555666>\n")
    allowed = kwargs["allowed_mentions"]
    assert isinstance(allowed.users, list)
    assert [user.id for user in allowed.users] == [111222333444555666]
    assert allowed.roles is False
    assert allowed.everyone is False
    assert allowed.replied_user is False


@pytest.mark.asyncio
async def test_clarify_no_metadata_omits_mention(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert not kwargs["content"].startswith("<@")
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None
    assert allowed.users == []


@pytest.mark.asyncio
async def test_clarify_env_opt_out_disables_mention(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    monkeypatch.setenv("DISCORD_CLARIFY_MENTIONS", "false")
    adapter = _make_adapter()
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
        metadata={"mention_user_id": "111222333444555666"},
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert not kwargs["content"].startswith("<@")
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None
    assert allowed.users == []


@pytest.mark.asyncio
async def test_clarify_config_extra_opt_out_disables_mention(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter(extra={"clarify_mentions": False})
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
        metadata={"mention_user_id": "111222333444555666"},
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert not kwargs["content"].startswith("<@")
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None
    assert allowed.users == []


@pytest.mark.asyncio
async def test_clarify_non_numeric_user_id_omits_mention(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channel = MagicMock()
    sent_msg = MagicMock()
    sent_msg.id = 123456
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send_clarify(
        chat_id="9001",
        question="Pick a color",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
        metadata={"mention_user_id": "alice"},
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert not kwargs["content"].startswith("<@")
    allowed = kwargs.get("allowed_mentions")
    assert allowed is not None
    assert allowed.users == []


def test_yaml_config_seeds_clarify_mentions(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)

    _apply_yaml_config({}, {"clarify_mentions": False})

    assert os.environ["DISCORD_CLARIFY_MENTIONS"] == "false"


def test_turn_runner_propagates_requester_without_mutating_thread_metadata(monkeypatch):
    """The real owning caller carries the authenticated source identity to the adapter."""
    from gateway.run_turn_runner import TurnRunner
    from tools import clarify_gateway as cm

    requester_id = "111222333444555666"
    thread_metadata = {"thread_id": "4242", "reply_to_message_id": "6789"}
    original_metadata = dict(thread_metadata)
    adapter = _RecordingClarifyAdapter()
    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="9001", user_id=requester_id, thread_id="4242"),
        session_key="discord:9001:4242",
        _status_adapter_snapshot=adapter,
        _status_chat_id="9001",
        _status_thread_metadata=thread_metadata,
    )
    runner = TurnRunner(MagicMock(), ctx)
    runner._close_native_stream_boundary = lambda *args, **kwargs: None

    class _ImmediateFuture:
        def __init__(self, result):
            self._result = result

        def result(self, timeout=None):
            return self._result

    runner._schedule = lambda coro, _label: _ImmediateFuture(asyncio.run(coro))
    real_register = cm.register

    def _register_and_answer(**kwargs):
        entry = real_register(**kwargs)
        cm.resolve_gateway_clarify(kwargs["clarify_id"], "red")
        return entry

    monkeypatch.setattr(cm, "register", _register_and_answer)
    response, answered = runner._ask_clarify_question(
        "Pick a color", ["red", "green"], False, rearm=False)

    assert (response, answered) == ("red", True)
    assert adapter.kwargs["metadata"] == {
        **original_metadata, "mention_user_id": requester_id}
    assert adapter.kwargs["metadata"] is not thread_metadata
    assert thread_metadata == original_metadata


@pytest.mark.asyncio
async def test_concurrent_clarifies_keep_requester_mentions_isolated(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)
    adapter = _make_adapter()
    channels = {}
    started = {}
    release = {}
    for chat_id in ("9001", "9002"):
        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.id = 123456
        channel.send = AsyncMock(return_value=sent_msg)
        channels[chat_id] = channel
        started[chat_id] = asyncio.Event()
        release[chat_id] = asyncio.Event()

    async def _fetch_channel(chat_id):
        key = str(chat_id)
        started[key].set()
        await release[key].wait()
        return channels[key]

    adapter._client.get_channel = MagicMock(return_value=None)
    adapter._client.fetch_channel = _fetch_channel

    async def _send(chat_id, requester_id):
        return await adapter.send_clarify(
            chat_id=chat_id,
            question="Pick a color",
            choices=None,
            clarify_id=f"cid-{chat_id}",
            session_key=f"sk-{chat_id}",
            metadata={"mention_user_id": requester_id},
        )

    first = asyncio.create_task(_send("9001", "111222333444555666"))
    await asyncio.wait_for(started["9001"].wait(), timeout=1)
    second = asyncio.create_task(_send("9002", "999888777666555444"))
    await asyncio.wait_for(started["9002"].wait(), timeout=1)
    release["9002"].set()
    await asyncio.wait_for(second, timeout=1)
    release["9001"].set()
    await asyncio.wait_for(first, timeout=1)

    first_content = channels["9001"].send.call_args.kwargs["content"]
    second_content = channels["9002"].send.call_args.kwargs["content"]
    assert first_content.startswith("<@111222333444555666>\n")
    assert second_content.startswith("<@999888777666555444>\n")
    assert [u.id for u in channels["9001"].send.call_args.kwargs["allowed_mentions"].users] == [
        111222333444555666]
    assert [u.id for u in channels["9002"].send.call_args.kwargs["allowed_mentions"].users] == [
        999888777666555444]
