"""Discord clarify prompts can @mention the requesting user."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, _apply_yaml_config


def _make_adapter(*, extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter


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
        question="Pick a color",
        choices=["red", "green"],
        clarify_id="cidM",
        session_key="sk-M",
        metadata={"mention_user_id": "111222333444555666"},
    )

    assert result.success is True
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"].startswith("<@111222333444555666>\n")
    assert "allowed_mentions" in kwargs


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
    assert "allowed_mentions" not in kwargs


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
    assert "allowed_mentions" not in kwargs


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
    assert "allowed_mentions" not in kwargs


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
    assert "allowed_mentions" not in kwargs


def test_yaml_config_seeds_clarify_mentions(monkeypatch):
    monkeypatch.delenv("DISCORD_CLARIFY_MENTIONS", raising=False)

    _apply_yaml_config({}, {"clarify_mentions": False})

    assert os.environ["DISCORD_CLARIFY_MENTIONS"] == "false"
