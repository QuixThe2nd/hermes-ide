"""Regression tests for Discord persistent typing task ownership."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


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
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *args, **kwargs: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3, green=1, grey=2,
        blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4,
        purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class _BlockingTypingHTTP:
    def __init__(self):
        self.requests = []
        self.request_started = [asyncio.Event(), asyncio.Event()]
        self.release = asyncio.Event()

    async def request(self, route):
        request_index = len(self.requests)
        self.requests.append(route)
        if request_index < len(self.request_started):
            self.request_started[request_index].set()
        await self.release.wait()


def _make_typing_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    http = _BlockingTypingHTTP()
    adapter._client = SimpleNamespace(http=http)
    return adapter, http


async def _cancel_tasks(*tasks):
    live_tasks = [task for task in tasks if task is not None and not task.done()]
    for task in live_tasks:
        task.cancel()
    if live_tasks:
        await asyncio.gather(*live_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_typing_loop_cannot_unregister_its_successor():
    """A cancelled loop must not steal a replacement loop's registration."""
    adapter, http = _make_typing_adapter()
    task_a = None
    task_b = None
    try:
        await adapter.send_typing("chan")
        task_a = adapter._typing_tasks["chan"]
        await asyncio.wait_for(http.request_started[0].wait(), timeout=2)

        # Reproduce stop_typing's pop-and-cancel half, pausing before it awaits A.
        assert adapter._typing_tasks.pop("chan") is task_a
        task_a.cancel()
        assert task_a.cancelling()
        assert not task_a.done()

        await adapter.send_typing("chan")
        task_b = adapter._typing_tasks["chan"]
        assert task_b is not task_a

        # A's finally runs before this await returns; B then enters its request.
        await task_a
        await asyncio.wait_for(http.request_started[1].wait(), timeout=2)
        assert not task_b.done()
        assert len(http.requests) == 2
        assert adapter._typing_tasks.get("chan") is task_b

        await adapter.stop_typing("chan")
        assert task_b.done()
        assert "chan" not in adapter._typing_tasks
    finally:
        # On the unfixed code B is unregistered, so clean it up directly.
        await _cancel_tasks(task_a, task_b)


@pytest.mark.asyncio
async def test_stop_typing_cancels_registered_loop():
    adapter, http = _make_typing_adapter()
    task = None
    try:
        await adapter.send_typing("chan")
        task = adapter._typing_tasks["chan"]
        await asyncio.wait_for(http.request_started[0].wait(), timeout=2)

        await adapter.stop_typing("chan")

        assert task.done()
        assert "chan" not in adapter._typing_tasks
        assert len(http.requests) == 1
    finally:
        await _cancel_tasks(task)
