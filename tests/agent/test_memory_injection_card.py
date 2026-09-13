"""Tests for the system-prompt memory context-injection card.

The MEMORY / USER PROFILE / external-provider blocks composed by
``agent/system_prompt.py::_memory_parts`` ride in the system prompt, so the
user-message diff observability in ``agent/turn_context.py`` can never see
them — they add no bytes around the clean user text. The card gives them the
same ``context.injected`` progress event user-message injections already had,
hash-gated per session: exactly one card on the first turn, one more on the
first turn after any memory edit, and silence while the memory content is
unchanged. The prompt build stashes the exact block list on the agent
(``_last_memory_blocks``) so the card never re-parses prompt text.

Follows the fake-agent ``build_turn_context`` pattern of
tests/agent/test_api_content_sidecar.py; no network.
"""

from __future__ import annotations

import hashlib
import types
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt_parts
from agent.turn_context import build_turn_context


class _FakeTodoStore:
    def has_items(self):
        return True


class _FakeGuardrails:
    def reset_for_turn(self):
        pass


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches
    (mirrors tests/agent/test_api_content_sidecar.py). Deliberately does NOT
    define ``_last_memory_blocks``: an agent that never built a prompt with
    memory must take the silent path, exactly like the pre-existing fakes."""

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
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
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


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message="hello",
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


class TestMemoryCardEmission:
    def test_first_turn_with_memory_blocks_emits_exactly_one_card(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK", "PROFILE-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert len(agent.tool_progress_events) == 1
        args, kwargs = agent.tool_progress_events[0]
        assert kwargs == {}
        assert args[:3] == ("context.injected", "context", None)
        assert args[3] == {
            "content": "MEMORY-BLOCK\n\nPROFILE-BLOCK",
            "injected_chars": len("MEMORY-BLOCK\n\nPROFILE-BLOCK"),
            "sources": ["memory"],
        }

    def test_second_turn_with_unchanged_blocks_emits_nothing(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
            _build(agent)
        assert len(agent.tool_progress_events) == 1  # only the first turn fired

    def test_three_consecutive_turns_identical_memory_fire_exactly_one_card(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            for _ in range(3):
                _build(agent)
        assert len(agent.tool_progress_events) == 1

    def test_changed_blocks_emit_new_card_on_that_turn(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
            agent._last_memory_blocks = ["MEMORY-BLOCK", "EDITED-BLOCK"]
            _build(agent)
            _build(agent)  # settled again: no third card
        assert len(agent.tool_progress_events) == 2
        assert agent.tool_progress_events[1][0][3] == {
            "content": "MEMORY-BLOCK\n\nEDITED-BLOCK",
            "injected_chars": len("MEMORY-BLOCK\n\nEDITED-BLOCK"),
            "sources": ["memory"],
        }

    def test_empty_blocks_emit_no_card_and_no_state_churn(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = []
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert agent.tool_progress_events == []
        assert not hasattr(agent, "_memory_card_state")

    def test_no_stash_at_all_emits_no_card(self):
        # Pre-existing fakes (and agents whose prompt predates the stash) never
        # define _last_memory_blocks — the silent path.
        agent = _FakeAgent()
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert agent.tool_progress_events == []
        assert not hasattr(agent, "_memory_card_state")

    def test_moa_turn_emits_no_card(self):
        """MoA bypasses the api_messages build, so claiming an injection there
        would be a lie — and priming the hash state would swallow the card the
        agent's first normal turn should show."""
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, moa_active=True)
        assert agent.tool_progress_events == []
        assert not hasattr(agent, "_memory_card_state")

    def test_virtual_moa_provider_emits_no_card(self):
        agent = _FakeAgent()
        agent.provider = "moa"
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, moa_active=False)
        assert agent.tool_progress_events == []
        assert not hasattr(agent, "_memory_card_state")

    def test_codex_app_server_turn_emits_no_card(self):
        agent = _FakeAgent()
        agent.api_mode = "codex_app_server"
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert agent.tool_progress_events == []
        assert not hasattr(agent, "_memory_card_state")

    def test_continue_interrupted_turn_emits_no_card(self):
        """The continued turn already carried its memory blocks when it first
        ran; re-announcing them on recovery would double the card."""
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, continue_interrupted_turn=True)
        assert agent.tool_progress_events == []

    def test_uncallable_callback_is_silent(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        agent.tool_progress_callback = None
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert agent.tool_progress_events == []

    def test_callback_failure_never_blocks_the_turn(self):
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]

        def _broken_callback(*_args, **_kwargs):
            raise RuntimeError("presentation broke")

        agent.tool_progress_callback = _broken_callback
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            ctx = _build(agent)
        assert ctx.current_turn_user_idx >= 0
        # The hash state still reflects the memory the system prompt carries
        # (the card is a view of a fact that already happened), so the next
        # identical turn does not retry the card.
        assert agent._memory_card_state == hashlib.sha256(
            b"MEMORY-BLOCK"
        ).hexdigest()
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)  # must not raise either

    def test_memory_card_coexists_with_user_message_injection_card(self):
        """Both families fire independently: the memory card (hash-gated) ahead
        of the user-message diff card (per-turn), and the diff card's payload
        stays byte-identical to the pre-existing path."""
        agent = _FakeAgent()
        agent._last_memory_blocks = ["MEMORY-BLOCK"]
        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"context": "PLUGIN-CTX"}],
        ):
            _build(agent)

        assert len(agent.tool_progress_events) == 2
        memory_args, _kwargs = agent.tool_progress_events[0]
        assert memory_args[3] == {
            "content": "MEMORY-BLOCK",
            "injected_chars": len("MEMORY-BLOCK"),
            "sources": ["memory"],
        }
        diff_args, _kwargs = agent.tool_progress_events[1]
        assert diff_args[:3] == ("context.injected", "context", None)
        assert diff_args[3] == {
            "content": "\n\nPLUGIN-CTX",
            "injected_chars": len("\n\nPLUGIN-CTX"),
            "sources": ["plugin"],
        }

        # Second turn: memory unchanged (silent), the per-turn diff card fires
        # again with identical bytes.
        agent.tool_progress_events.clear()
        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"context": "PLUGIN-CTX"}],
        ):
            _build(agent)
        assert len(agent.tool_progress_events) == 1
        assert agent.tool_progress_events[0][0][3] == {
            "content": "\n\nPLUGIN-CTX",
            "injected_chars": len("\n\nPLUGIN-CTX"),
            "sources": ["plugin"],
        }


# ---------------------------------------------------------------------------
# The prompt-build stash (agent/system_prompt.py)
# ---------------------------------------------------------------------------

class _FakeMemoryStore:
    def __init__(self, blocks):
        self._blocks = blocks

    def format_for_system_prompt(self, kind):
        return self._blocks.get(kind)


def _make_prompt_agent(**overrides):
    """SimpleNamespace covering what build_system_prompt_parts touches
    (mirrors tests/agent/test_system_prompt.py); no tools, so neither the
    skills index nor the coding git probes run."""
    base = dict(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        _emit_status=lambda *_args, **_kwargs: None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestMemoryBlocksStash:
    def test_prompt_build_stashes_exact_block_list(self):
        agent = _make_prompt_agent(
            _memory_enabled=True,
            _user_profile_enabled=True,
            _memory_store=_FakeMemoryStore(
                {"memory": "MEMORY-BLOCK", "user": "PROFILE-BLOCK"}
            ),
        )
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        assert agent._last_memory_blocks == ["MEMORY-BLOCK", "PROFILE-BLOCK"]

    def test_prompt_build_stashes_empty_list_without_memory(self):
        agent = _make_prompt_agent(
            _memory_enabled=True,
            _user_profile_enabled=True,
            _memory_store=_FakeMemoryStore({}),
        )
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        assert agent._last_memory_blocks == []

    def test_disabled_kinds_are_not_stashed(self):
        agent = _make_prompt_agent(
            _memory_enabled=False,
            _user_profile_enabled=True,
            _memory_store=_FakeMemoryStore(
                {"memory": "MEMORY-BLOCK", "user": "PROFILE-BLOCK"}
            ),
        )
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        assert agent._last_memory_blocks == ["PROFILE-BLOCK"]

    def test_rebuild_replaces_the_stash(self):
        store = _FakeMemoryStore({"memory": "MEMORY-BLOCK"})
        agent = _make_prompt_agent(
            _memory_enabled=True,
            _user_profile_enabled=False,
            _memory_store=store,
        )
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
            store._blocks["memory"] = "EDITED-BLOCK"
            build_system_prompt_parts(agent)
        assert agent._last_memory_blocks == ["EDITED-BLOCK"]
