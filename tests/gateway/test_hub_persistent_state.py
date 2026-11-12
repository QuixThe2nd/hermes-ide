"""Hub state that must survive a hub restart."""

import asyncio
import json
import os

import pytest

from gateway.overlap_registry import OverlapSuccessorRegistry
from gateway.status import get_process_start_time
from plugins.platforms.discord.hub import DiscordSessionRouter


def test_successor_record_survives_registry_reload(tmp_path):
    state = tmp_path / "succ.json"
    first = OverlapSuccessorRegistry(state_path=state)
    first.record_spawn(os.getpid(), str(tmp_path), predecessor_pid=1)

    reloaded = OverlapSuccessorRegistry(state_path=state)
    reloaded.attach(
        pid=os.getpid(), start_time=get_process_start_time(os.getpid()),
        gateway_home=str(tmp_path), generation_id="gen-2", peer_pid=os.getpid(),
    )
    assert reloaded.snapshot()[0]["state"] == "attached"


def test_reloaded_record_with_wrong_fingerprint_is_dropped(tmp_path):
    state = tmp_path / "succ.json"
    state.write_text(json.dumps([{
        "pid": os.getpid(), "start_time": 1, "gateway_home": str(tmp_path),
        "predecessor_pid": 1, "spawned_wall": 0, "generations": [], "attached": False,
    }]))
    reloaded = OverlapSuccessorRegistry(state_path=state)
    with pytest.raises(RuntimeError, match="not spawned"):
        reloaded.attach(
            pid=os.getpid(), start_time=get_process_start_time(os.getpid()),
            gateway_home=str(tmp_path), generation_id="gen-2",
        )
    assert json.loads(state.read_text()) == []


def test_held_events_survive_router_restart_and_clear_after_replay(tmp_path):
    store = tmp_path / "held.json"

    async def scenario():
        first = DiscordSessionRouter()
        first.bind_store(store)
        await first.route("thread:1", {"session_key": "thread:1", "text": "hi"})
        assert store.exists()

        second = DiscordSessionRouter()
        second.bind_store(store)
        assert [r["text"] for r in second.undelivered] == ["hi"]
        got = []

        async def deliver(key, record):
            got.append(record["text"])

        second.attach("gen-new", deliver, gateway_pid=os.getpid())
        assert await second.replay_undelivered() == 1
        assert got == ["hi"]
        assert not store.exists()

    asyncio.run(scenario())
