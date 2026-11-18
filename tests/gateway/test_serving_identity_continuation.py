"""Serving-identity delivery on transparent continuation turns.

A forced-interruption resume (drain-timeout restart) replays the
interrupted batch as an ordinary ``continue after tool results`` call: no
current user message, no synthetic rows, no recovery prose. But the
conversation JUST moved to a new gateway process — the exact moment the
per-turn serving-identity note matters most.

Contract under proof, through the REAL ``TurnRunner.run_sync`` resume path
(same harness shape as ``test_forced_resume_integration.py``):

- the constructed agent carries the continuation identity note
  (``_gateway_continuation_note``) on a forced-victim continuation turn —
  the gateway stages it even though the ordinary gateway-notes channel is
  deferred on continuations;
- the note names the serving pid;
- the restart-requester restore note (the sanctioned persisted-note path)
  carries the serving identity clause, so the user-visible resume note
  also says which process now serves the conversation.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

_INTERRUPTED_RESULT = '{"exit_code": 130, "output": "[Command interrupted]"}'


@pytest.fixture(autouse=True)
def _isolated_replay_reservations():
    """Keep the replay-execution fence out of neighboring tests (same shape
    as test_forced_resume_integration.py)."""
    import gateway.forced_resume_replay as _freplay

    with _freplay._FALLBACK_RESERVATION_LOCK:
        _freplay._FALLBACK_RESERVATIONS.clear()


class StubModelAgent:
    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.tools = []
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000)
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_reasoning_tokens = 0
        self.executions = []
        self.run_calls = []
        self._results = {}

    def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
        for call in assistant_message.tool_calls:
            self.executions.append((call.function.name, call.id))
            from agent.tool_dispatch_helpers import make_tool_result_message

            messages.append(
                make_tool_result_message(
                    call.function.name, self._results.get(call.id, "OK"), call.id))
        return "ok"

    def run_conversation(self, message, **kwargs):
        self.run_calls.append((message, kwargs))
        return {
            "final_response": "Deploy finished cleanly.", "messages": [],
            "completed": True, "failed": False, "interrupted": False,
            "api_calls": 1, "tools": [],
        }


class RecordingSessionStore:
    """Real-shaped minimal session store (same shape as
    test_forced_resume_integration.py)."""

    def __init__(self, entries=None):
        self._entries = entries or {}
        self.appended = []
        self.rewrites = []
        self.durable = []

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.appended.append((session_id, message))
        self.durable.append(dict(message))

    def append_to_transcript_checked(self, session_id, message):
        self.append_to_transcript(session_id, message)
        return (
            bool(self.durable)
            and self.durable[-1].get("role") == message.get("role")
            and self.durable[-1].get("tool_call_id") == message.get("tool_call_id")
        )

    def rewrite_transcript(
        self, session_id, messages, active_only=False, reject_active_turn_lease=False
    ):
        self.rewrites.append((session_id, [dict(m) for m in messages], active_only))
        self.durable = [dict(m) for m in messages]
        return True


def _gateway_runner(store):
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store = store
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "test-model", "runtime": {}}
    runner._agent_config_signature.return_value = ("test-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    # Real attribute reads for the serving-identity snapshot: this harness
    # runner is an ordinary serving process.
    runner._restart_requested = False
    runner._draining = False
    runner._overlap_successor_pending = False
    runner._overlap_predecessor_pid = None
    runner._overlap_restart_successor_pid = None
    runner._overlap_restart_successor_start_time = None
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._service_tier = None
    return runner


def _resume_entry(reason):
    return SimpleNamespace(
        resume_pending=True, resume_reason=reason, last_resume_marked_at=time.time())


def _interrupted_terminal_history():
    return [
        {"role": "user", "content": "deploy the service", "timestamp": time.time()},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_deploy",
                "function": {"name": "terminal",
                             "arguments": '{"command": "make deploy"}'},
            }],
            "timestamp": time.time(),
        },
        {
            "role": "tool", "tool_call_id": "call_deploy",
            "content": _INTERRUPTED_RESULT, "timestamp": time.time(),
        },
    ]


def _make_ctx(agent_cls, history, message, session_key, session_id="test-session"):
    source = SessionSource(
        platform=Platform.LOCAL, chat_id="test-chat", user_id="test-user")
    return TurnContext(
        source=source, message=message, history=history, session_id=session_id,
        session_key=session_key, user_config={}, AIAgent=agent_cls,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )


def _run_and_capture_agent(history, message, reason, results):
    constructed = []

    class _Agent(StubModelAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._results = results
            constructed.append(self)

    store = RecordingSessionStore({f"sk-{reason}": _resume_entry(reason)})
    runner = _gateway_runner(store)
    ctx = _make_ctx(_Agent, history, message, f"sk-{reason}")
    TurnRunner(runner, ctx).run_sync()
    assert len(constructed) == 1
    return constructed[0], store


def test_forced_continuation_agent_carries_identity_note():
    """The gateway must stage the serving-identity note even on the turn type
    whose ordinary notes channel is deferred — a restart just moved the
    conversation to a new process."""
    agent, _store = _run_and_capture_agent(
        _interrupted_terminal_history(), "", "restart_timeout", {})

    run_message, kwargs = agent.run_calls[0]
    assert kwargs.get("continue_interrupted_turn") is True
    assert run_message is None
    note = getattr(agent, "_gateway_continuation_note", "")
    assert isinstance(note, str) and note
    assert f"pid {os.getpid()}" in note
    assert note.startswith("[System note:")


def test_restart_requester_restore_note_carries_serving_identity():
    """The sanctioned persisted restore note must say WHICH process now
    serves the conversation — the restarted gateway, not the pid the
    supervisor files still show."""
    history = [
        {"role": "user", "content": "/restart", "timestamp": time.time()},
        {"role": "assistant", "content": "Restarting now.", "timestamp": time.time()},
    ]
    agent, _store = _run_and_capture_agent(history, "", "restart_requester", {})

    run_message, _kwargs = agent.run_calls[0]
    assert run_message.startswith("[System note: The previous turn")
    assert f"pid {os.getpid()}" in run_message
    assert "served by gateway pid" in run_message
