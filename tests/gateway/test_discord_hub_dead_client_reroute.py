"""A delivery to a SIGKILLed gateway is rerouted, not parked until the next attach."""

import os
import subprocess
import sys

import pytest

from plugins.platforms.discord.hub import DiscordSessionRouter


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


async def _broken(session_key, record):
    raise ConnectionResetError("Connection lost")


@pytest.mark.asyncio
async def test_dead_owner_is_dropped_and_event_rerouted():
    router = DiscordSessionRouter()
    got = []

    async def live(session_key, record):
        got.append(session_key)

    router.attach("live", live, gateway_pid=os.getpid())
    router.attach("dead", _broken, gateway_pid=_dead_pid())
    router._owners["thread"] = "dead"

    assert await router.route("thread", {"session_key": "thread"}) == "live"
    assert got == ["thread"]
    assert router.owner_for("thread") == "live"
    assert router.undelivered == []
    assert router.current_generation == "live"


@pytest.mark.asyncio
async def test_live_owner_write_failure_is_still_retained():
    router = DiscordSessionRouter()
    other = []

    async def live(session_key, record):
        other.append(session_key)

    router.attach("other", live, gateway_pid=os.getpid())
    router.attach("flaky", _broken, gateway_pid=os.getpid())
    router._owners["thread"] = "flaky"

    assert await router.route("thread", {"session_key": "thread"}) is None
    assert other == []
    assert len(router.undelivered) == 1


@pytest.mark.asyncio
async def test_only_dead_generation_left_retains_event():
    router = DiscordSessionRouter()
    router.attach("dead", _broken, gateway_pid=_dead_pid())
    assert await router.route("thread", {"session_key": "thread"}) is None
    assert len(router.undelivered) == 1
    assert router.current_generation is None
