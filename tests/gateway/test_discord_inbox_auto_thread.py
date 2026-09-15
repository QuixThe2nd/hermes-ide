"""Adapter auto-threading for the Hermes Starts inbox channel.

The inbox contract: a bot-authored top-level message in the configured
Hermes Starts inbox channel always anchors a public thread, no matter which
send path delivered it (model ``send_message``, ``hermes send``, progress
embeds, cron). ``start_conversation`` already threads its own openings via
REST; the adapter's ``send()`` used to be the gap. These tests pin the
load-bearing behaviors: thread creation + participation marking on inbox
sends, no threading for other channels or existing threads, and the
already-threaded / no-state / home_server-fallback edges.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


# ---------------------------------------------------------------------------
# Discord mock setup — no-op when the conftest mock (or the real library) is
# already in place; keeps this file standalone-safe like its siblings.
# ---------------------------------------------------------------------------

def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


INBOX_CHANNEL_ID = "555000"


def _plugin():
    """hermes_starts resolved at test time.

    The plugin-discovery tests in tests/plugins/hermes_starts reload the
    plugin by deleting it from ``sys.modules``; a collection-time import
    would monkeypatch a stale instance while the adapter's lazy resolution
    re-imports a fresh (unpatched) one.
    """
    import plugins.hermes_starts as hermes_starts

    return hermes_starts


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def thread_calls(monkeypatch):
    """Spy on hermes_starts' REST thread primitive and the participation mark."""
    calls = {"create": [], "marked": []}
    hermes_starts = _plugin()

    def fake_create(token, channel_id, message_id, name):
        calls["create"].append(
            {
                "token": token,
                "channel_id": channel_id,
                "message_id": message_id,
                "name": name,
            }
        )
        return "thread-9001"

    def fake_mark(thread_id):
        calls["marked"].append(thread_id)

    monkeypatch.setattr(hermes_starts, "_create_thread_for_message", fake_create)
    monkeypatch.setattr(hermes_starts, "_mark_participated_thread", fake_mark)
    return calls


def _write_inbox_state(home, channel_id=INBOX_CHANNEL_ID):
    state_path = home / "hermes_starts" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channel_id": channel_id,
                "channel_name": "inbox",
                "welcome_message_id": "msg-0",
                "counter": 3,
            }
        ),
        encoding="utf-8",
    )


def _write_home_server_inbox(home, channel_id=INBOX_CHANNEL_ID):
    state_path = home / "home_server" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"guild_id": "guild-2", "channels": {"chat": {"inbox": channel_id}}}
        ),
        encoding="utf-8",
    )


def _make_adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))


def _make_channel(channel_id=INBOX_CHANNEL_ID, *, thread=False, forum=False):
    """Fake text channel handing out sequential message ids from 9001."""
    message_counter = iter(range(9001, 10000))

    async def _send(**_kwargs):
        return SimpleNamespace(id=next(message_counter))

    channel = SimpleNamespace(
        id=int(channel_id),
        send=AsyncMock(side_effect=_send),
        type=SimpleNamespace(value=15 if forum else 0),
    )
    if thread:
        channel.parent_id = 111  # thread-like: reports its parent channel
    return channel


def _attach(adapter, channel):
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=AsyncMock(),
    )
    return adapter


class TestInboxSendThreads:
    @pytest.mark.asyncio
    async def test_send_to_inbox_creates_thread_and_marks_participation(
        self, _hermes_home, thread_calls
    ):
        _write_inbox_state(_hermes_home)
        adapter = _attach(_make_adapter(), _make_channel())
        content = "Quick observation about the build"

        result = await adapter.send(INBOX_CHANNEL_ID, content)

        assert result.success is True
        assert result.message_id == "9001"
        assert len(thread_calls["create"]) == 1
        call = thread_calls["create"][0]
        assert call["channel_id"] == INBOX_CHANNEL_ID
        assert call["message_id"] == "9001"
        assert call["token"] == "test-token"
        # Short name derived from the sent content, no hardcoded labels.
        assert call["name"] == adapter._derive_auto_thread_name(content)
        assert call["name"] == content
        # The adapter's live participation tracker learns the thread (persisted
        # to discord_threads.json), so replies need no @mention.
        assert "thread-9001" in adapter._threads
        threads_file = _hermes_home / "discord_threads.json"
        assert "thread-9001" in json.loads(threads_file.read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_send_to_other_channel_creates_no_thread(
        self, _hermes_home, thread_calls
    ):
        _write_inbox_state(_hermes_home)
        adapter = _attach(_make_adapter(), _make_channel(channel_id="777999"))

        result = await adapter.send("777999", "hello there")

        assert result.success is True
        assert thread_calls["create"] == []

    @pytest.mark.asyncio
    async def test_send_into_existing_thread_creates_no_second_thread(
        self, _hermes_home, thread_calls
    ):
        _write_inbox_state(_hermes_home)
        thread_channel = _make_channel(channel_id="888000", thread=True)
        adapter = _attach(_make_adapter(), thread_channel)

        result = await adapter.send(
            "888000", "reply inside the thread", metadata={"thread_id": "888000"}
        )

        assert result.success is True
        assert thread_calls["create"] == []

    @pytest.mark.asyncio
    async def test_multi_chunk_send_threads_first_message(
        self, _hermes_home, thread_calls
    ):
        _write_inbox_state(_hermes_home)
        adapter = _attach(_make_adapter(), _make_channel())

        result = await adapter.send(INBOX_CHANNEL_ID, "x" * 4500)

        assert result.success is True
        # 4500 chars split into 3 chunks (9001/9002/9003); the opener anchors.
        assert len(thread_calls["create"]) == 1
        assert thread_calls["create"][0]["message_id"] == "9001"

    @pytest.mark.asyncio
    async def test_progress_embed_send_to_inbox_threads(self, _hermes_home, thread_calls):
        """The agent-progress embed early-return path threads too."""
        _write_inbox_state(_hermes_home)
        adapter = _attach(_make_adapter(), _make_channel())
        content = "Claude Code Agent: http://192.168.30.20:8787/#20260829-024525-1532951"

        result = await adapter.send(INBOX_CHANNEL_ID, content)

        assert result.success is True
        assert result.message_id == "9001"
        assert len(thread_calls["create"]) == 1
        embed_call = thread_calls["create"][0]
        assert embed_call["message_id"] == "9001"
        # Name comes from the status line content, not the embed's empty body.
        assert embed_call["name"] == adapter._derive_auto_thread_name(content)

    @pytest.mark.asyncio
    async def test_forum_parent_inbox_lookup_does_not_thread(
        self, _hermes_home, thread_calls
    ):
        """A forum parent never hosts a plain anchored thread."""
        _write_inbox_state(_hermes_home)
        channel = _make_channel(forum=True)

        async def _forum_create_thread(**_kwargs):
            thread_channel = SimpleNamespace(
                id=7777,
                send=AsyncMock(return_value=SimpleNamespace(id=9002)),
            )
            return SimpleNamespace(
                thread=thread_channel, message=SimpleNamespace(id=9001), id=7777
            )

        channel.create_thread = AsyncMock(side_effect=_forum_create_thread)
        adapter = _attach(_make_adapter(), channel)
        adapter._is_forum_parent = lambda _ch: True

        result = await adapter.send(INBOX_CHANNEL_ID, "forum-shaped content")

        assert result.success is True
        assert thread_calls["create"] == []


class TestInboxResolution:
    @pytest.mark.asyncio
    async def test_send_without_inbox_state_is_noop(self, _hermes_home, thread_calls):
        adapter = _attach(_make_adapter(), _make_channel())

        result = await adapter.send(INBOX_CHANNEL_ID, "hello")

        assert result.success is True
        assert thread_calls["create"] == []

    @pytest.mark.asyncio
    async def test_disabled_plugin_is_noop(self, _hermes_home, thread_calls):
        """A deny-listed hermes_starts must not thread, even with state on disk."""
        _write_inbox_state(_hermes_home)
        (_hermes_home / "config.yaml").write_text(
            json.dumps({"plugins": {"disabled": ["hermes_starts"]}}),
            encoding="utf-8",
        )
        adapter = _attach(_make_adapter(), _make_channel())

        result = await adapter.send(INBOX_CHANNEL_ID, "hello")

        assert result.success is True
        assert thread_calls["create"] == []

    @pytest.mark.asyncio
    async def test_home_server_inbox_is_the_fallback(
        self, _hermes_home, thread_calls
    ):
        """No hermes_starts state — the shared home_server inbox still threads."""
        _write_home_server_inbox(_hermes_home)
        adapter = _attach(_make_adapter(), _make_channel())

        result = await adapter.send(INBOX_CHANNEL_ID, "via the shared inbox")

        assert result.success is True
        assert len(thread_calls["create"]) == 1
        assert thread_calls["create"][0]["channel_id"] == INBOX_CHANNEL_ID


class TestAlreadyThreaded:
    @pytest.mark.asyncio
    async def test_already_threaded_message_is_success_not_error(
        self, _hermes_home, monkeypatch, caplog
    ):
        """Discord's 160004 (thread exists for this message) satisfies the invariant."""
        _write_inbox_state(_hermes_home)

        def fake_create(token, channel_id, message_id, name):
            raise urllib.error.HTTPError(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/threads",
                400,
                "Bad Request",
                None,
                io.BytesIO(
                    b'{"code": 160004, "message": "thread already created for this message"}'
                ),
            )

        monkeypatch.setattr(_plugin(), "_create_thread_for_message", fake_create)
        adapter = _attach(_make_adapter(), _make_channel())

        with caplog.at_level(logging.WARNING):
            result = await adapter.send(INBOX_CHANNEL_ID, "opening that already has a thread")

        assert result.success is True
        assert "Inbox thread creation" not in caplog.text

    @pytest.mark.asyncio
    async def test_thread_create_failure_degrades_to_warning(
        self, _hermes_home, monkeypatch, caplog
    ):
        """A failed send's message is never threaded; a failed THREAD on a sent
        message degrades to a warning — the delivered message stands."""
        _write_inbox_state(_hermes_home)

        def fake_create(token, channel_id, message_id, name):
            raise RuntimeError("discord REST unreachable")

        monkeypatch.setattr(_plugin(), "_create_thread_for_message", fake_create)
        adapter = _attach(_make_adapter(), _make_channel())

        with caplog.at_level(logging.WARNING):
            result = await adapter.send(INBOX_CHANNEL_ID, "delivered but unthreaded")

        assert result.success is True
        assert "Inbox thread creation for message 9001 failed" in caplog.text
