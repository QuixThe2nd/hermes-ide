"""End-to-end producer coverage for ``recalled_count`` on ``context.injected``.

The real producer is ``build_turn_context``'s injection-observability block
(agent/turn_context.py): when memory is among the injection sources it asks
the memory manager for the discrete recall count and rides it on the event.
The gateway's counted heading (`🧠 Recalled N memories`) is only as good as
this payload, so these tests run the actual prologue with a recording
progress callback — no patched emit sites, no network.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent.turn_context import build_turn_context


class _TodoStore:
    def has_items(self):
        return True


class _Guardrails:
    def reset_for_turn(self):
        pass


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches
    (mirrors tests/agent/test_api_content_sidecar.py)."""

    def __init__(self):
        self.session_id = "sess-1"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-x"
        self.api_mode = "chat_completions"
        self.platform = "cli"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self._skip_mcp_refresh = True
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _TodoStore()
        self._tool_guardrails = _Guardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self.tool_progress_events = []
        self.tool_progress_callback = (
            lambda *args, **kwargs: self.tool_progress_events.append((args, kwargs))
        )

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


class _CountingMemoryManager:
    """Fake manager whose prefetch injects text and reports a settable count."""

    def __init__(self, count, *, text="RECALLED-MEMORY", raises_count=False,
                 no_count_method=False):
        self._count = count
        self._text = text
        self._raises_count = raises_count
        self._no_count_method = no_count_method

    def on_turn_start(self, *_args, **_kwargs):
        pass

    def prefetch_all(self, _query, *, session_id: str = ""):
        return self._text

    def describe_recall(self):
        return "🧠 Hindsight — recalled 2 memories"

    def recalled_count(self):
        if self._no_count_method:  # pragma: no cover - shape guard
            raise AttributeError
        if self._raises_count:
            raise RuntimeError("count boom")
        return self._count


class _NoCountMethodManager(_CountingMemoryManager):
    """Managers/fakes predating the counted-heading API."""

    def __init__(self):
        super().__init__(None)

    def recalled_count(self):  # noqa: D102 - deliberate removal
        raise AttributeError("recalled_count")


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message="explain agent communication",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


@pytest.fixture(autouse=True)
def _reset_session_memory_card_state():
    from agent import turn_context as _tc

    _tc._MEMORY_CARD_STATE.clear()
    yield
    _tc._MEMORY_CARD_STATE.clear()


def _single_injection_event(agent):
    memory_events = [
        event for event in agent.tool_progress_events
        if "memory" in (event[0][3] or {}).get("sources", [])
    ]
    assert len(memory_events) == 1
    return memory_events[0][0][3]


@pytest.mark.parametrize("count,expected", [(1, 1), (3, 3), (0, 0), (None, None)])
def test_memory_injection_carries_recalled_count(count, expected):
    agent = _FakeAgent()
    agent._memory_manager = _CountingMemoryManager(count)
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent)
    assert _single_injection_event(agent)["recalled_count"] is expected


def test_count_changes_between_turns():
    agent = _FakeAgent()
    manager = _CountingMemoryManager(1)
    agent._memory_manager = manager
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent)
        manager._count = 3
        _build(agent)
    counts = [
        event[0][3]["recalled_count"]
        for event in agent.tool_progress_events
        if "memory" in (event[0][3] or {}).get("sources", [])
    ]
    assert counts == [1, 3]


def test_manager_without_recalled_count_reports_none():
    agent = _FakeAgent()
    agent._memory_manager = _NoCountMethodManager()
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent)
    assert _single_injection_event(agent)["recalled_count"] is None


def test_raising_count_is_swallowed_not_fatal():
    agent = _FakeAgent()
    agent._memory_manager = _CountingMemoryManager(3, raises_count=True)
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent)
    assert ctx.ext_prefetch_cache  # the turn itself kept running
    assert _single_injection_event(agent)["recalled_count"] is None


def test_no_prefetch_means_no_memory_event():
    agent = _FakeAgent()
    agent._memory_manager = _CountingMemoryManager(5, text="")
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent)
    assert agent.tool_progress_events == []


def test_plugin_only_injection_has_no_recalled_count_key():
    agent = _FakeAgent()
    with patch(
        "hermes_cli.plugins.invoke_hook",
        return_value=[{"context": "PLUGIN-CTX"}],
    ):
        _build(agent, user_message="hello")
    assert len(agent.tool_progress_events) == 1
    payload = agent.tool_progress_events[0][0][3]
    assert payload["sources"] == ["plugin"]
    assert "recalled_count" not in payload
