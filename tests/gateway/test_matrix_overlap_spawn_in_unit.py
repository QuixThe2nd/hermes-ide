"""Under systemd the Matrix hub client spawns the successor in the gateway unit, like Discord."""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.matrix.hub_client import MatrixHubClientAdapter


def _client():
    client = MatrixHubClientAdapter.__new__(MatrixHubClientAdapter)
    client._call = AsyncMock(return_value={"pid": 999})
    client.wait_for_overlap_successor = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_systemd_spawn_stays_in_gateway_unit(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "unit")
    spawn = MagicMock(return_value=types.SimpleNamespace(pid=4321))
    monkeypatch.setattr("plugins.platforms.discord.overlap_spawn.spawn_gateway_successor", spawn)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 77)
    client = _client()

    assert await client.spawn_overlap_successor(via_service=True, predecessor_pid=10) == 4321
    spawn.assert_called_once()
    methods = [call.args[0] for call in client._call.await_args_list]
    assert "spawn_gateway" not in methods
    assert "register_spawned_gateway" in methods
    assert client._last_overlap_successor_service_managed is True
    assert client._last_overlap_successor_start_time == 77


@pytest.mark.asyncio
async def test_unsupervised_spawn_still_uses_hub(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1)
    client = _client()
    assert await client.spawn_overlap_successor(via_service=False, predecessor_pid=10) == 999
    assert client._call.await_args_list[0].args[0] == "spawn_gateway"
    assert client._last_overlap_successor_service_managed is False
