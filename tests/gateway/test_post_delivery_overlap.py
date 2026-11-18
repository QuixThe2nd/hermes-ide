"""Overlapping-turn post-delivery isolation on a reused session guard.

``_process_message_background``'s queued-follow-up path spawns the drain
task (run 2, reusing the SAME interrupt Event guard) BEFORE the first
task's ``finally`` runs. Run 2's handler stamps a newer
``_hermes_run_generation`` and registers a generation-2 post-delivery
callback while run 1's finally is still inside its (second)
``_stop_typing_refresh`` await.

Contract: after run 1's ``_process_message_background`` returns while run
2 is still blocked in its handler, run 1's finally must NOT have fired
run 2's callback and must NOT have marked the shared guard's post-delivery
boundary fired — each turn's finally is bound to its own frozen run
identity, never to whatever the shared Event carries after an await.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.platforms import base as base_mod
from tests.gateway.test_jev_goal_notices import SESSION_KEY, _RegistryAdapter, _event


@pytest.mark.asyncio
async def test_overlapping_turn_post_delivery_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    adapter = _RegistryAdapter()

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()
    fired: list = []

    # Stall run 1's SECOND _stop_typing_refresh (the first finally-bound
    # call in _process_message_background's finally) until run 2's handler
    # has stamped generation 2, registered its callback, and parked on the
    # release event.
    original_stop = base_mod.BasePlatformAdapter._stop_typing_refresh
    stop_calls = {"count": 0}

    async def stalling_stop(self, chat_id, typing_task=None, *, metadata=None,
                            timeout=0.5, stop_attempts=2):
        if asyncio.current_task() is first_task:
            stop_calls["count"] += 1
            if stop_calls["count"] == 2:
                assert await asyncio.wait_for(second_started.wait(), 10), (
                    "run 2 never registered its callback"
                )
        await original_stop(self, chat_id, typing_task, metadata=metadata,
                            timeout=timeout, stop_attempts=stop_attempts)

    monkeypatch.setattr(base_mod.BasePlatformAdapter, "_stop_typing_refresh", stalling_stop)

    async def handler(event):
        guard = adapter._active_sessions[SESSION_KEY]
        if event.text == "first":
            # Run 1: original generation stamp, empty turn.
            guard._hermes_run_generation = 1
            first_started.set()
            return None
        # Run 2 (drain task, same shared interrupt_event): stamp generation 2,
        # register the run-2 post-delivery callback, park until released.
        guard._hermes_run_generation = 2
        registered = adapter.register_post_delivery_callback(
            SESSION_KEY, lambda: fired.append("run2"), generation=2
        )
        assert registered, "run 2 registration was refused (no live run boundary)"
        second_started.set()
        await release.wait()
        return None

    adapter.set_message_handler(handler)

    # Run 1 finds the second event already queued, so it takes the
    # spawn-drain-before-finally path.
    adapter._pending_messages[SESSION_KEY] = _event("second")
    first_task = asyncio.create_task(
        adapter._process_message_background(_event("first"), SESSION_KEY)
    )
    assert await asyncio.wait_for(first_started.wait(), 10), "run 1 handler never ran"

    # Run 1 completes its whole lifecycle — including the finally that calls
    # _fire_post_delivery_callback — while run 2 is parked in its handler.
    await asyncio.wait_for(first_task, 10)

    guard = adapter._active_sessions.get(SESSION_KEY)
    assert guard is not None, "shared session guard vanished while run 2 is live"
    assert fired == [], (
        "run 1's finally fired run 2's generation-2 post-delivery callback"
    )
    assert not getattr(guard, "_hermes_post_delivery_fired", False), (
        "run 1's finally marked the shared guard fired for run 2"
    )

    # Let run 2 finish and drain every background task.
    release.set()
    tasks = {t for t in adapter._session_tasks.values() if not t.done()}
    tasks |= {t for t in adapter._background_tasks if not t.done()}
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
    # Run 2's own boundary is the one that fires its callback — exactly once.
    assert fired == ["run2"]
