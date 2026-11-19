"""Overlap restart right after a hub bounce (adapters reconnecting)."""

import asyncio
from types import SimpleNamespace
from gateway.config import Platform
from unittest.mock import AsyncMock

from gateway import overlap_restart


class FakeHubAdapter:
    supports_overlap_restart = True

    def __init__(self):
        self.spawn_overlap_successor = AsyncMock(return_value=4242)
        self.handoff_event = AsyncMock(return_value=True)
        self.wait_for_overlap_successor = AsyncMock()
        self.ready = False

    def mark_overlap_successor_ready(self):
        self.ready = True

    def admits_owned_work_while_draining(self, source=None):
        return self.ready


def _runner(**kw):
    base = dict(adapters={}, _profile_adapters={}, _failed_platforms={})
    base.update(kw)
    return SimpleNamespace(**base)


def test_spawn_waits_for_hub_adapter_to_reconnect():
    async def scenario():
        runner = _runner(_failed_platforms={Platform.DISCORD: {}})
        adapter = FakeHubAdapter()

        async def reconnect():
            await asyncio.sleep(0.2)
            runner._failed_platforms.clear()
            runner.adapters["discord"] = adapter

        task = asyncio.create_task(reconnect())
        pid = await overlap_restart.spawn_successor(runner, via_service=False)
        await task
        assert pid == 4242
        assert runner._overlap_restart_successor_started is True
        adapter.spawn_overlap_successor.assert_awaited_once()

    asyncio.run(scenario())


def test_spawn_without_any_hub_adapter_returns_none_loudly(caplog):
    async def scenario():
        return await overlap_restart.spawn_successor(_runner(), via_service=False)

    assert asyncio.run(scenario()) is None
    assert "no connected hub adapter" in caplog.text


def test_successor_less_drain_keeps_events_local():
    async def scenario():
        adapter = FakeHubAdapter()
        runner = _runner(
            _draining=True, _restart_task_started=True,
            _overlap_restart_successor_started=False,
            _intake_adapter_for=lambda source: adapter,
        )
        event = SimpleNamespace(source=object())
        handed = await overlap_restart.handoff_overlap_event(
            runner, event, "agent:main:discord:thread:1", require_successor_ready=True,
        )
        assert handed is False
        adapter.handoff_event.assert_not_awaited()

    asyncio.run(scenario())


def test_drain_with_live_successor_still_hands_off():
    async def scenario():
        adapter = FakeHubAdapter()
        adapter.ready = True
        runner = _runner(
            _draining=True, _restart_task_started=True,
            _overlap_restart_successor_started=True,
            _intake_adapter_for=lambda source: adapter,
        )
        event = SimpleNamespace(source=object())
        assert await overlap_restart.handoff_overlap_event(
            runner, event, "agent:main:discord:thread:1", require_successor_ready=True,
        ) is True

    asyncio.run(scenario())
