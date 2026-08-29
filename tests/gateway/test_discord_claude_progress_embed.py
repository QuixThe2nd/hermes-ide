"""Behavior tests for Discord Claude Code Agent progress embeds."""

from __future__ import annotations

import pytest

from gateway.platforms.base import SendResult
from tests.gateway.test_discord_cursor_progress_embed import _make_discord_send_adapter


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Claude Code Agent: http://192.168.30.20:8787/",
        "Claude Code Agent: http://192.168.30.20:8787/#20260829-024525-1532951",
        "Claude Code Agent: http://100.109.12.0:8787/#20260829-080009-1779398",
        "Claude Code Agent: https://192.168.30.20:8787/  \n",
    ],
)
def test_claude_agent_status_url_accepts_valid(content):
    from plugins.platforms.discord.adapter import _claude_agent_status_url

    assert _claude_agent_status_url(content) is not None


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Claude Code Agent:",
        "Claude Code Agent: ",
        "prefix Claude Code Agent: http://192.168.30.20:8787/",
        "Claude Code Agent: http://192.168.30.20:8787/ suffix",
        "Claude Code Agent: http://127.0.0.1:8787/",
        "Claude Code Agent: https://cursor.com/agents/x",
        "Claude Code Agent: http://192.168.30.20:8787/watch live",
        "Claude Code Agent: http://192.168.30.20:8787/api/runs",
        "Claude Code Agent: http://192.168.30.20:8787/?x=1",
        "Claude Code Agent: http://user@192.168.30.20:8787/",
        "Claude Code Agent: http://192.168.30.20:8787/#",
        "Claude Code Agent: http://192.168.30.20:8787/#not-a-stem",
        "Claude Code Agent: javascript:alert(1)",
    ],
)
def test_claude_agent_status_url_rejects_invalid(content):
    from plugins.platforms.discord.adapter import _claude_agent_status_url

    assert _claude_agent_status_url(content) is None


def test_claude_agent_status_url_returns_original_string():
    from plugins.platforms.discord.adapter import _claude_agent_status_url

    url = "http://100.109.12.0:8787/#20260829-080009-1779398"
    assert _claude_agent_status_url(f"Claude Code Agent: {url}\n") == url


# ---------------------------------------------------------------------------
# Embed spec / builder
# ---------------------------------------------------------------------------


def test_claude_agent_embed_spec_fields():
    from plugins.platforms.discord.adapter import (
        _claude_agent_embed_spec,
        _format_discord_markdown_link,
    )

    watch_url = "http://192.168.30.20:8787/#20260829-024525-1532951"
    spec = _claude_agent_embed_spec(watch_url)

    assert spec["title"] == "Claude Code Agent"
    assert spec["url"] == watch_url
    assert spec["description"] == _format_discord_markdown_link(
        "Watch live session", watch_url
    )
    # URL must only appear inside the markdown link destination, not as bare text.
    desc_without_link_dest = spec["description"].replace(f"<{watch_url}>", "")
    assert watch_url not in desc_without_link_dest
    assert spec["author"]["name"] == "Claude Code"
    assert spec["author"]["icon_url"] == "https://claude.ai/images/claude_app_icon.png"
    assert spec["author"]["url"] == "https://claude.ai"
    assert spec["thumbnail"] == "https://claude.ai/images/claude_app_icon.png"
    assert spec["color"] == 0xD97757
    assert spec["footer"] == "Anthropic · Claude Code"


def test_build_claude_agent_embed_renders_discord_embed():
    from plugins.platforms.discord.adapter import _build_claude_agent_embed

    watch_url = "http://100.109.12.0:8787/"
    embed = _build_claude_agent_embed(watch_url)

    assert embed.title == "Claude Code Agent"
    assert embed.url == watch_url
    assert embed.author.name == "Claude Code"
    assert embed.author.icon_url == "https://claude.ai/images/claude_app_icon.png"
    assert embed.author.url == "https://claude.ai"
    assert embed.thumbnail.url == "https://claude.ai/images/claude_app_icon.png"
    assert embed.color == 0xD97757
    assert embed.footer["text"] == "Anthropic · Claude Code"
    assert "Watch live session" in embed.description


# ---------------------------------------------------------------------------
# DiscordAdapter.send()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_send_claude_progress_uses_embed_not_raw_url():
    watch_url = "http://192.168.30.20:8787/#20260829-024525-1532951"
    content = f"Claude Code Agent: {watch_url}"
    adapter, _channel, sent_calls = _make_discord_send_adapter()

    result = await adapter.send("555", content)

    assert result.success is True
    assert result.message_id == "9001"
    assert len(sent_calls) == 1
    call = sent_calls[0]
    assert "embed" in call
    embed = call["embed"]
    assert embed.title == "Claude Code Agent"
    assert embed.url == watch_url
    assert embed.author.name == "Claude Code"
    assert embed.author.icon_url == "https://claude.ai/images/claude_app_icon.png"
    assert embed.thumbnail.url == "https://claude.ai/images/claude_app_icon.png"
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
async def test_discord_send_claude_progress_forum_parent_uses_markdown_link():
    from plugins.platforms.discord.adapter import _format_discord_markdown_link

    watch_url = "http://100.109.12.0:8787/#20260829-080009-1779398"
    content = f"Claude Code Agent: {watch_url}"
    adapter, channel, sent_calls = _make_discord_send_adapter(forum_parent=True)

    result = await adapter.send("555", content)

    assert result.success is True
    expected = (
        f"Claude Code Agent: "
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
async def test_discord_send_claude_progress_embed_failure_falls_back_to_markdown():
    from plugins.platforms.discord.adapter import _format_discord_markdown_link

    watch_url = "http://192.168.30.20:8787/"
    content = f"Claude Code Agent: {watch_url}"
    adapter, _channel, sent_calls = _make_discord_send_adapter(
        send_side_effects=[RuntimeError("embed rejected"), None],
    )

    result = await adapter.send("555", content)

    assert result.success is True
    assert len(sent_calls) == 2
    assert "embed" in sent_calls[0]
    fallback = sent_calls[1]["content"]
    expected = (
        f"Claude Code Agent: "
        f"{_format_discord_markdown_link('Watch live session', watch_url)}"
    )
    assert fallback == expected
    assert watch_url not in fallback.replace(
        _format_discord_markdown_link("Watch live session", watch_url), ""
    )


@pytest.mark.asyncio
async def test_discord_send_claude_progress_disconnected_client():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = None

    result = await adapter.send(
        "555",
        "Claude Code Agent: http://192.168.30.20:8787/",
    )

    assert result == SendResult(success=False, error="Not connected")
