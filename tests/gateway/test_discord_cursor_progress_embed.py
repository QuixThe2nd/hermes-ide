"""Behavior tests for Discord Cursor Cloud Agent progress embeds."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult


def _make_discord_send_adapter(*, forum_parent: bool = False, send_side_effects=None):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent_calls = []

    async def _fake_send(**kwargs):
        sent_calls.append(kwargs)
        if send_side_effects:
            effect = send_side_effects[len(sent_calls) - 1]
            if isinstance(effect, Exception):
                raise effect
        return SimpleNamespace(id=9001)

    channel_type = SimpleNamespace(value=15) if forum_parent else SimpleNamespace(value=0)
    channel = SimpleNamespace(
        send=AsyncMock(side_effect=_fake_send),
        type=channel_type,
    )
    if forum_parent:
        async def _create_thread(**kwargs):
            thread_channel = SimpleNamespace(
                send=AsyncMock(return_value=SimpleNamespace(id=9002)),
                id=7777,
            )
            return SimpleNamespace(
                thread=thread_channel,
                message=SimpleNamespace(id=9001),
                id=7777,
            )
        channel.create_thread = AsyncMock(side_effect=_create_thread)

    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=AsyncMock(),
    )
    adapter._is_forum_parent = lambda ch: forum_parent
    return adapter, channel, sent_calls


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Cursor Cloud Agent: https://cursor.com/agents/bc-abc123",
        "Cursor Cloud Agent: https://www.cursor.com/agents/bc-abc123",
        "Cursor Cloud Agent: https://cursor.com/agents/bc-abc123  \n",
    ],
)
def test_cursor_cloud_agent_status_url_accepts_valid(content):
    from plugins.platforms.discord.adapter import _cursor_cloud_agent_status_url

    url = _cursor_cloud_agent_status_url(content)
    assert url is not None
    assert url.startswith("https://")
    assert "/agents/" in url


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Cursor Cloud Agent:",
        "Cursor Cloud Agent: ",
        "prefix Cursor Cloud Agent: https://cursor.com/agents/x",
        "Cursor Cloud Agent: https://cursor.com/agents/x suffix",
        "Cursor Cloud Agent: ftp://cursor.com/agents/x",
        "Cursor Cloud Agent: https://evil.com/agents/x",
        "Cursor Cloud Agent: https://cursor.com/not-agents/x",
        "Cursor Cloud Agent: https://user:pass@cursor.com/agents/x",
        "Cursor Cloud Agent: https://cursor.com@evil.com/agents/x",
    ],
)
def test_cursor_cloud_agent_status_url_rejects_invalid(content):
    from plugins.platforms.discord.adapter import _cursor_cloud_agent_status_url

    assert _cursor_cloud_agent_status_url(content) is None


# ---------------------------------------------------------------------------
# Embed spec / builder
# ---------------------------------------------------------------------------


def test_cursor_cloud_agent_embed_spec_fields():
    from plugins.platforms.discord.adapter import (
        _cursor_cloud_agent_embed_spec,
        _format_discord_markdown_link,
    )

    watch_url = "https://cursor.com/agents/bc-test"
    spec = _cursor_cloud_agent_embed_spec(watch_url)

    assert spec["title"] == "Cursor Cloud Agent"
    assert spec["url"] == watch_url
    assert "Watch live session" in spec["description"]
    assert spec["description"] == _format_discord_markdown_link(
        "Watch live session", watch_url
    )
  # URL must only appear inside the markdown link destination, not as bare text.
    bare = watch_url
    desc_without_link_dest = spec["description"].replace(f"<{watch_url}>", "")
    assert bare not in desc_without_link_dest
    assert spec["author"]["name"] == "Cursor"
    assert spec["author"]["icon_url"] == "https://cursor.com/apple-touch-icon.png"
    assert spec["author"]["url"] == "https://cursor.com"
    assert spec["thumbnail"] == "https://cursor.com/apple-touch-icon.png"
    assert spec["color"] == 0x000000
    assert spec["footer"] == "Cursor · Cloud Agent"


def test_build_cursor_cloud_agent_embed_renders_discord_embed():
    from plugins.platforms.discord.adapter import _build_cursor_cloud_agent_embed

    watch_url = "https://cursor.com/agents/bc-embed"
    embed = _build_cursor_cloud_agent_embed(watch_url)

    assert embed.title == "Cursor Cloud Agent"
    assert embed.url == watch_url
    assert embed.author.name == "Cursor"
    assert embed.author.icon_url == "https://cursor.com/apple-touch-icon.png"
    assert embed.author.url == "https://cursor.com"
    assert embed.thumbnail.url == "https://cursor.com/apple-touch-icon.png"
    assert embed.color == 0x000000
    assert embed.footer["text"] == "Cursor · Cloud Agent"
    assert "Watch live session" in embed.description


# ---------------------------------------------------------------------------
# DiscordAdapter.send()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_send_cursor_progress_uses_embed_not_raw_url():
    from plugins.platforms.discord.adapter import _format_discord_markdown_link

    watch_url = "https://cursor.com/agents/bc-live"
    content = f"Cursor Cloud Agent: {watch_url}"
    adapter, _channel, sent_calls = _make_discord_send_adapter()

    result = await adapter.send("555", content)

    assert result.success is True
    assert result.message_id == "9001"
    assert len(sent_calls) == 1
    call = sent_calls[0]
    assert "embed" in call
    embed = call["embed"]
    assert embed.title == "Cursor Cloud Agent"
    assert embed.url == watch_url
    assert embed.author.icon_url == "https://cursor.com/apple-touch-icon.png"
    assert embed.thumbnail.url == "https://cursor.com/apple-touch-icon.png"
    assert call.get("content") in (None, "")
    assert content not in str(call)


@pytest.mark.asyncio
async def test_discord_send_ordinary_text_unchanged():
    adapter, _channel, sent_calls = _make_discord_send_adapter()

    result = await adapter.send("555", "Hello from Hermes")

    assert result.success is True
    assert len(sent_calls) == 1
    assert sent_calls[0]["content"] == "Hello from Hermes"
    assert "embed" not in sent_calls[0]


@pytest.mark.asyncio
async def test_discord_send_cursor_progress_forum_parent_uses_markdown_link():
    from plugins.platforms.discord.adapter import _format_discord_markdown_link

    watch_url = "https://cursor.com/agents/bc-forum"
    content = f"Cursor Cloud Agent: {watch_url}"
    adapter, channel, sent_calls = _make_discord_send_adapter(forum_parent=True)

    result = await adapter.send("555", content)

    assert result.success is True
    expected = (
        f"Cursor Cloud Agent: "
        f"{_format_discord_markdown_link('Watch live session', watch_url)}"
    )
    channel.create_thread.assert_awaited_once()
    starter = channel.create_thread.await_args.kwargs["content"]
    assert starter == expected
    assert watch_url not in starter.replace(
        _format_discord_markdown_link("Watch live session", watch_url), ""
    )
    assert "embed" not in (sent_calls[0] if sent_calls else {})


@pytest.mark.asyncio
async def test_discord_send_cursor_progress_embed_failure_falls_back_to_markdown():
    from plugins.platforms.discord.adapter import _format_discord_markdown_link

    watch_url = "https://cursor.com/agents/bc-failsoft"
    content = f"Cursor Cloud Agent: {watch_url}"
    adapter, _channel, sent_calls = _make_discord_send_adapter(
        send_side_effects=[RuntimeError("embed rejected"), None],
    )

    result = await adapter.send("555", content)

    assert result.success is True
    assert len(sent_calls) == 2
    assert "embed" in sent_calls[0]
    fallback = sent_calls[1]["content"]
    expected = (
        f"Cursor Cloud Agent: "
        f"{_format_discord_markdown_link('Watch live session', watch_url)}"
    )
    assert fallback == expected
    assert watch_url not in fallback.replace(
        _format_discord_markdown_link("Watch live session", watch_url), ""
    )


@pytest.mark.asyncio
async def test_discord_send_cursor_progress_disconnected_client():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = None

    result = await adapter.send(
        "555",
        "Cursor Cloud Agent: https://cursor.com/agents/bc-offline",
    )

    assert result == SendResult(success=False, error="Not connected")
