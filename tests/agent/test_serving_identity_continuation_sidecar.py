"""Continuation-turn delivery of the serving-identity note via api_content.

``build_turn_context`` on a transparent continuation (``continue_interrupted_turn``)
keeps the request byte-equivalent to an ordinary in-loop "continue after
tool results" call: no new user row, no gateway-notes splice. The
serving-identity note for a just-restarted process rides the anchored user
row's ``api_content`` sidecar instead — append-only after any bytes that
row already sent, persisted so later turns replay the same bytes, and never
as prose in the clean transcript content.
"""

from __future__ import annotations

import types

import pytest

from agent.turn_context import build_turn_context


class _FakeTodoStore:
    def has_items(self):
        return True


class _FakeGuardrails:
    def reset_for_turn(self):
        pass


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches
    (mirrors tests/agent/test_gateway_turn_sidecar.py)."""

    def __init__(self):
        self.session_id = "sess-1"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-x"
        self.api_mode = "chat_completions"
        self.platform = "discord"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self._skip_mcp_refresh = True
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(protect_first_n=2, protect_last_n=2)
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, messages, _history=None):
        pass


def _build(agent, history, **overrides):
    kwargs = dict(
        agent=agent,
        user_message=None,
        system_message=None,
        conversation_history=history,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: str(s),
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
        continue_interrupted_turn=True,
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


IDENTITY_NOTE = (
    "[System note: This turn is served by Hermes gateway pid 4242. That pid — "
    "not any pid reported by systemctl, the pid file, or gateway status files — "
    "is the process running this conversation.]"
)


def _interrupted_history():
    return [
        {"role": "user", "content": "deploy the service"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_deploy",
             "function": {"name": "terminal", "arguments": '{"command": "make deploy"}'}}]},
        {"role": "tool", "tool_call_id": "call_deploy", "content": '{"exit_code": 130}'},
    ]


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    from unittest.mock import patch

    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def test_continuation_note_rides_anchored_row_api_content():
    agent = _FakeAgent()
    agent._gateway_continuation_note = IDENTITY_NOTE
    history = _interrupted_history()
    ctx = _build(agent, history)

    messages = ctx.messages
    assert [m.get("role") for m in messages] == ["user", "assistant", "tool"]
    anchored = messages[0]
    assert anchored["content"] == "deploy the service"  # clean transcript stays clean
    assert anchored.get("api_content") == f"deploy the service\n\n{IDENTITY_NOTE}"


def test_continuation_note_is_one_shot():
    agent = _FakeAgent()
    agent._gateway_continuation_note = IDENTITY_NOTE
    _build(agent, _interrupted_history())
    assert getattr(agent, "_gateway_continuation_note", "") == ""


def test_continuation_note_appends_after_existing_sidecar_bytes():
    """A row that already sent injected bytes keeps them: the note is
    appended after the existing api_content, never a replacement."""
    agent = _FakeAgent()
    agent._gateway_continuation_note = IDENTITY_NOTE
    history = _interrupted_history()
    history[0]["api_content"] = (
        "deploy the service\n\n[memory block from the original turn]")
    ctx = _build(agent, history)

    anchored = ctx.messages[0]
    assert anchored["api_content"] == (
        "deploy the service\n\n[memory block from the original turn]"
        f"\n\n{IDENTITY_NOTE}")


def test_continuation_without_note_touches_nothing():
    agent = _FakeAgent()
    history = _interrupted_history()
    ctx = _build(agent, history)
    assert "api_content" not in ctx.messages[0]
    assert ctx.messages[0]["content"] == "deploy the service"


def test_multimodal_anchored_row_gets_note_as_text_part():
    agent = _FakeAgent()
    agent._gateway_continuation_note = IDENTITY_NOTE
    history = _interrupted_history()
    history[0]["content"] = [
        {"type": "text", "text": "deploy the service"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    ctx = _build(agent, history)

    anchored = ctx.messages[0]
    parts = anchored["content"]
    assert isinstance(parts, list)
    assert parts[-1] == {"type": "text", "text": IDENTITY_NOTE}
    assert getattr(agent, "_gateway_continuation_note", "") == ""
