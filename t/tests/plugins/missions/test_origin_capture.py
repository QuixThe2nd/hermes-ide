"""Production-shaped origin capture for assistant missions (uniform lifecycle).

``registry.dispatch`` — the real tool-executor entry point — forwards only
``task_id`` / ``session_id`` / ``tool_call_id`` / ``user_task``; it never
sends ``session_key``. These tests drive the missions plugin exactly that way
(scoped registration + the gateway's approval ContextVar) instead of passing
the trusted key as a kwarg the production path never delivers, so they fail
if origin capture ever regresses to reading the raw kwarg.

Also covers the durable half of the lifecycle change: a mission completion is
published through ``publish_terminal_event``, so it lands on the shared
completion rail AND as a claimable ``async_delegations`` row that replays
after a restart.
"""

from __future__ import annotations

import json
import queue

import pytest

from tools import async_delegation as ad
from tools.approval import reset_current_session_key, set_current_session_key
from tools.process_registry import (
    format_process_notification,
    process_registry,
)
from tools.registry import registry

ORIGIN_KEY = "agent:main:discord:thread:abc:abc"
CHAT = "61400000000@s.whatsapp.net"
# Scoped registration keeps the plugin's tools out of the global registry so
# this file cannot perturb any other test's tool resolution.
SCOPE = "test-missions-origin"


class _RegistrationCtx:
    """The slice of the plugin registration context the missions plugin uses."""

    def register_tool(self, **kw):
        registry.register(scope=SCOPE, **kw)


def _drain() -> None:
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


@pytest.fixture()
def missions_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    import plugins.missions as pm

    ad._reset_for_tests()
    _drain()
    yield pm
    ad._reset_for_tests()
    _drain()
    with registry._lock:  # private, but the scoped slot is test-only state
        registry._scoped_tools.pop(SCOPE, None)


def _dispatch_start(pm, *, session_id: str, tool_call_id: str) -> dict:
    """Start a mission exactly the way the tool executor would.

    ``background=true`` because the renamed tool's foreground default now
    blocks until the mission is terminal — these tests want the started
    mission and its completion rail, not a wait. The shared acceptance
    envelope carries the ``mission-<id>`` delegation id; the mission_id key
    is merged back in so legacy assertions keep naming the mission.
    """
    token = set_current_session_key(ORIGIN_KEY)
    try:
        out = json.loads(
            registry.dispatch(
                "dispatch_assistant",
                {"chat_id": CHAT, "goal": "Agree picnic time", "background": True},
                task_id=f"task-{tool_call_id}",
                session_id=session_id,
                tool_call_id=tool_call_id,
                user_task="plan the picnic",
                scope=SCOPE,
            )
        )
    finally:
        reset_current_session_key(token)
    mission = pm.find_active_mission_for_chat(CHAT)
    assert mission is not None, out
    assert out["delegation_id"] == f"mission-{mission['mission_id']}"
    out["mission_id"] = mission["mission_id"]
    return out


class TestOriginCapture:
    def test_registry_dispatch_populates_created_by_session(self, missions_env):
        missions_env.register(_RegistrationCtx())

        out = _dispatch_start(missions_env, session_id="sess-origin-1", tool_call_id="c1")
        # Background acceptance envelope, not a terminal result inline.
        assert out["status"] == "dispatched"
        assert out["mode"] == "background"
        assert out["tool"] == "delegate_assistant"
        assert out["result_kind"] == "mission"
        assert out["delegation_id"].startswith("mission-")

        mission = missions_env.find_active_mission_for_chat(CHAT)
        assert mission is not None
        assert out["delegation_id"] == f"mission-{mission['mission_id']}"
        # All three origin fields are populated WITHOUT a session_key kwarg —
        # the pre-fix code left created_by_session/reply_target empty here.
        assert mission["created_by_session"] == ORIGIN_KEY
        assert mission["reply_target"] == ORIGIN_KEY
        assert mission["origin_parent_session_id"] == "sess-origin-1"

    def test_explicit_reply_to_still_wins(self, missions_env):
        missions_env.register(_RegistrationCtx())
        token = set_current_session_key(ORIGIN_KEY)
        try:
            out = registry.dispatch(
                "dispatch_assistant",
                {
                    "chat_id": CHAT,
                    "goal": "g",
                    "reply_to": "discord:guild:1",
                    "background": True,
                },
                task_id="t",
                session_id="sess-origin-2",
                tool_call_id="c2",
                user_task="u",
                scope=SCOPE,
            )
        finally:
            reset_current_session_key(token)
        assert json.loads(out)["status"] == "dispatched"
        assert json.loads(out)["tool"] == "delegate_assistant"
        mission = missions_env.find_active_mission_for_chat(CHAT)
        assert mission["reply_target"] == "discord:guild:1"
        assert mission["created_by_session"] == ORIGIN_KEY


class TestDurableCompletion:
    def test_completion_rides_the_shared_rail_with_mission_provenance(
        self, missions_env
    ):
        missions_env.register(_RegistrationCtx())
        mid = _dispatch_start(
            missions_env, session_id="sess-origin-3", tool_call_id="c3"
        )["mission_id"]

        done = json.loads(
            missions_env.handle_dispatch_agent(
                {"action": "complete", "mission_id": mid, "outcome": "Saturday 11am"}
            )
        )
        assert done["ok"] is True
        assert done["notified"] is True

        evt = process_registry.completion_queue.get(timeout=5)
        assert evt["type"] == "async_delegation"
        assert evt["delegation_id"] == f"mission-{mid}"
        assert evt["session_key"] == ORIGIN_KEY
        assert evt["parent_session_id"] == "sess-origin-3"
        assert evt["status"] == "completed"
        assert evt["tool"] == missions_env.DELEGATE_ASSISTANT_TOOL
        assert evt["result_kind"] == "mission"
        assert evt["background"] is True
        assert "Saturday 11am" in evt["summary"]
        assert process_registry.completion_queue.empty()

        rendered = format_process_notification(evt)
        assert rendered is not None
        assert "ASSISTANT MISSION COMPLETE" in rendered

    def test_completion_is_claimable_once_and_replays_after_restart(
        self, missions_env
    ):
        missions_env.register(_RegistrationCtx())
        mid = _dispatch_start(
            missions_env, session_id="sess-origin-4", tool_call_id="c4"
        )["mission_id"]
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mid, "outcome": "done"}
        )
        evt = process_registry.completion_queue.get(timeout=5)
        delegation_id = evt["delegation_id"]

        # Durable row: one delivery claim wins, the second is refused.
        assert ad.claim_completion_delivery(delegation_id, "claim-a") is True
        assert ad.claim_completion_delivery(delegation_id, "claim-b") is False

        # And the outcome replays as a fresh turn after a "restart".
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        restored = replay.get_nowait()
        assert restored["delegation_id"] == delegation_id
        assert restored.get("restored") is True
        assert restored["summary"] == evt["summary"]

    def test_cancelled_mission_publishes_an_error_status(self, missions_env):
        missions_env.register(_RegistrationCtx())
        mid = _dispatch_start(
            missions_env, session_id="sess-origin-5", tool_call_id="c5"
        )["mission_id"]
        missions_env.handle_dispatch_agent({"action": "cancel", "mission_id": mid})

        evt = process_registry.completion_queue.get(timeout=5)
        assert evt["status"] == "error"
        assert evt["delegation_id"] == f"mission-{mid}"
