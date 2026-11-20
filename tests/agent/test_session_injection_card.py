"""Session-context injection card: the once-per-shape system-prompt inventory.

``build_system_prompt_parts`` records which sections landed in the prompt
(``agent._system_prompt_section_manifest``); the turn prologue renders that
manifest as ONE ``context.injected`` card, sha-gated exactly like the memory
card — first turn and first turn after a shape change fire once, steady-state
turns stay silent. Memory is excluded (its own card covers it); restore paths
that rebuild the prefix from stored bytes record no manifest and stay silent.

Also covers the manifest recording itself: a prompt with project context,
skills index and workspace must carry exactly those rows, in prompt order.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt, build_system_prompt_parts
from tests.agent.test_memory_injection_card import (
    _FakeMemoryStore,
    _make_prompt_agent,
)
from agent.turn_context import build_turn_context
from tests.agent.test_gateway_turn_sidecar import _FakeAgent, _build


@pytest.fixture(autouse=True)
def _reset_session_card_state():
    """The session-scoped hash registry is process state; every test starts dry
    (fake agents share session id "sess-1")."""
    from agent import context_injection_cards as _cic

    _cic._SESSION_CARD_SHAS.clear()
    yield
    _cic._SESSION_CARD_SHAS.clear()


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _manifest_agent(rows, **extra):
    agent = _FakeAgent()
    agent._system_prompt_section_manifest = rows
    for name, value in extra.items():
        setattr(agent, name, value)
    return agent


def _session_cards(events):
    """The session-context payloads the fake agent captured."""
    return [
        payload
        for topic, payload in events
        if topic == "context.injected" and payload.get("kinds") == ["session"]
    ]


def _capture(agent):
    events = []
    agent.tool_progress_callback = (
        lambda *args, **kwargs: events.append((args[0], args[3] if len(args) > 3 else kwargs))
    )
    return events


class TestSessionCardEmission:
    def test_first_turn_with_manifest_emits_exactly_one_card(self):
        agent = _manifest_agent(
            [("identity", "soul"), ("context_files", None), ("timestamp", None)]
        )
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        cards = _session_cards(events)
        assert len(cards) == 1
        assert cards[0]["sources"] == ["session"]
        assert cards[0]["content"] == "identity (soul)\ncontext_files\ntimestamp"
        assert cards[0]["injected_chars"] == len(cards[0]["content"])

    def test_second_turn_same_manifest_stays_silent(self):
        agent = _manifest_agent([("identity", "default")])
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
            _build(agent)
        assert len(_session_cards(events)) == 1

    def test_fresh_agent_same_session_does_not_reemit(self):
        # The api_server builds a fresh agent per turn; only the process-scoped
        # registry keeps the card silent for unchanged prompt shapes.
        agent = _manifest_agent([("identity", "default")])
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        twin = _manifest_agent([("identity", "default")])
        twin_events = _capture(twin)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(twin)
        assert len(_session_cards(events)) == 1
        assert len(_session_cards(twin_events)) == 0

    def test_changed_shape_fires_again_on_that_turn(self):
        agent = _manifest_agent([("identity", "default")])
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        agent._system_prompt_section_manifest = [
            ("identity", "default"),
            ("skills_index", None),
        ]
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        cards = _session_cards(events)
        assert len(cards) == 2
        assert cards[1]["content"] == "identity (default)\nskills_index"

    def test_ephemeral_and_tools_ride_the_card(self):
        agent = _manifest_agent(
            [("identity", "soul")], ephemeral_system_prompt="SESSION CONTEXT", tools=[{}, {}],
        )
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        cards = _session_cards(events)
        assert len(cards) == 1
        assert cards[0]["content"].endswith(
            "session context (ephemeral, per-request)\n2 tool schemas"
        )

    def test_ephemeral_change_alone_reopens_the_card(self):
        # The ephemeral row is part of the sha: a session that gains the
        # per-request context block gets one fresh card, not silence.
        agent = _manifest_agent([("identity", "soul")])
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        agent.ephemeral_system_prompt = "SESSION CONTEXT"
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert len(_session_cards(events)) == 2

    def test_no_manifest_no_ephemeral_no_tools_stays_silent(self):
        # Restore paths record no manifest; an agent with neither manifest nor
        # the per-request additions must not claim a session card.
        agent = _FakeAgent()
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent)
        assert _session_cards(events) == []

    def test_moa_turn_does_not_emit(self):
        agent = _manifest_agent([("identity", "default")])
        events = _capture(agent)
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, moa_active=True)
        assert _session_cards(events) == []


class TestManifestRecording:
    """build_system_prompt_parts records what actually landed, in prompt order."""

    def test_minimal_prompt_records_core_sections_in_order(self):
        agent = _make_prompt_agent()
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        rows = [kind for kind, _detail in agent._system_prompt_section_manifest]
        # Identity leads the stable tier; the timestamp line is the volatile
        # tail when no runtime-env hints landed.
        assert rows[0] == "identity"
        assert "docs" in rows
        assert rows[-1] == "timestamp"

    def test_default_identity_detail_when_soul_not_loaded(self):
        agent = _make_prompt_agent()  # load_soul_identity=False, skip_context_files=True
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        rows = dict(agent._system_prompt_section_manifest)
        assert rows["identity"] == "default"

    def test_caller_system_message_lands_in_the_manifest(self):
        agent = _make_prompt_agent()
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent, system_message="CALLER INSTRUCTIONS")
        rows = dict(agent._system_prompt_section_manifest)
        assert rows.get("caller_system") is None

    def test_skipped_context_files_leave_no_row(self):
        # The row is only recorded when the part actually landed; a
        # skip_context_files agent must not claim a context_files injection.
        agent = _make_prompt_agent(skip_context_files=True)
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        kinds = [kind for kind, _detail in agent._system_prompt_section_manifest]
        assert "context_files" not in kinds
        assert "workspace" not in kinds

    def test_memory_sections_are_excluded(self):
        # Memory rides its own sha-gated card; the session inventory must not
        # double-announce it.
        agent = _make_prompt_agent(
            _memory_enabled=True,
            _user_profile_enabled=True,
        )
        with patch("agent.prompt_builder.build_environment_hints", return_value=""):
            build_system_prompt_parts(agent)
        kinds = [kind for kind, _detail in agent._system_prompt_section_manifest]
        assert "memory" not in kinds


# ---------------------------------------------------------------------------
# Byte identity against the pre-card scaffold (6ac6f2c9)
# ---------------------------------------------------------------------------

_BASE_COMMIT = "6ac6f2c9"
_SOUL_BYTES = "You are Hermes Agent — pinned soul identity bytes."


def _pinned_dependencies():
    """Dependency surface both scaffold versions read: pinned so the only
    variable between them is the scaffold itself."""
    return [
        patch("agent.prompt_builder.load_soul_md", return_value=_SOUL_BYTES),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch(
            "agent.prompt_builder.build_context_files_prompt",
            return_value="PROJECT CONTEXT BYTES",
        ),
        patch(
            "agent.prompt_builder.build_skills_system_prompt",
            return_value="SKILLS INDEX BYTES",
        ),
        patch("agent.skill_commands.build_auto_load_prompt", return_value=("", [], [])),
        patch(
            "agent.coding_context.coding_system_prompt_parts",
            return_value=(["CODING PREFIX"], ["WORKSPACE SNAPSHOT"], ["CODING TRAILING"]),
        ),
        patch(
            "agent.coding_context.coding_compact_skill_categories",
            return_value=frozenset(),
        ),
        patch(
            "tools.bot_mode_probe.get_bot_mode_protocol_section",
            return_value="BOT MODE PROTOCOL",
        ),
        patch("tools.bot_mode_probe.epoch_line", return_value="EPOCH LINE"),
        patch(
            "hermes_cli.plugins.format_system_prompt_sections",
            return_value="PLUGIN SECTIONS RENDERED",
        ),
    ]


def _fresh_default():
    return _make_prompt_agent()


def _fresh_soul():
    return _make_prompt_agent(load_soul_identity=True, skip_context_files=True)


def _fresh_workspace():
    agent = _make_prompt_agent(
        valid_tool_names=["skills_list", "skill_view", "terminal"],
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_store=_FakeMemoryStore(
            {"memory": "MEMORY-BLOCK", "user": "PROFILE-BLOCK"}
        ),
    )
    agent._plugin_system_prompt_sections_snapshot = (
        types.SimpleNamespace(position="after_memory"),
    )
    return agent


def _fresh_bot_mode():
    agent = _make_prompt_agent(_bot_mode_protocol=True)
    agent._session_title_hint = "Bot Chat"
    return agent


_PROMPT_SHAPES = [
    pytest.param(_fresh_default, id="default"),
    pytest.param(_fresh_soul, id="soul_identity"),
    pytest.param(_fresh_workspace, id="workspace_skills_memory_plugins"),
    pytest.param(_fresh_bot_mode, id="bot_mode"),
]


@pytest.fixture(scope="session")
def base_system_prompt(tmp_path_factory):
    """The pre-card scaffold loaded from git: the byte-identity baseline."""
    import importlib.util
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = subprocess.run(
        ["git", "-C", str(repo), "show", f"{_BASE_COMMIT}:agent/system_prompt.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    path = tmp_path_factory.mktemp("scaffold_base", numbered=True) / "system_prompt_base.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("system_prompt_base", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("factory", _PROMPT_SHAPES)
def test_system_prompt_bytes_identical_to_pre_card_scaffold(factory, base_system_prompt):
    """The session-card recording is pure scaffolding: every tier of the
    prompt — and the joined full prompt — is byte-identical to the scaffold
    before the card existed (6ac6f2c9), across the representative shapes."""
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _pinned_dependencies():
            stack.enter_context(p)
        base_parts = base_system_prompt.build_system_prompt_parts(
            factory(), system_message=None
        )
        current_parts = build_system_prompt_parts(factory(), system_message=None)
    assert current_parts == base_parts
    assert (
        "\n\n".join(p for p in current_parts.values() if p)
        == "\n\n".join(p for p in base_parts.values() if p)
    )


@pytest.mark.parametrize("factory", _PROMPT_SHAPES)
def test_full_prompt_bytes_identical_to_pre_card_scaffold(factory, base_system_prompt):
    """The cached-prompt entrypoint (build_system_prompt) also produces
    identical bytes — manifest recording changes no model-facing output."""
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _pinned_dependencies():
            stack.enter_context(p)
        base_prompt = base_system_prompt.build_system_prompt(factory())
        current_prompt = build_system_prompt(factory())
    assert current_prompt == base_prompt


def test_manifest_recording_is_pure():
    """Purity on the current scaffold: a stale manifest attribute is
    replaced, never consulted, and the prompt bytes do not depend on it."""
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _pinned_dependencies():
            stack.enter_context(p)
        clean = build_system_prompt_parts(_fresh_workspace())
        agent = _fresh_workspace()
        agent._system_prompt_section_manifest = [("junk", "stale")]
        reloaded = build_system_prompt_parts(agent)
    assert reloaded == clean
    assert ("junk", "stale") not in agent._system_prompt_section_manifest
    assert agent._system_prompt_section_manifest


def test_prompt_is_deterministic_across_rebuilds():
    """Rebuilding on a fresh agent yields identical bytes: the manifest and
    its bookkeeping introduce no render-order nondeterminism."""
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _pinned_dependencies():
            stack.enter_context(p)
        first = build_system_prompt_parts(_fresh_workspace())
        second = build_system_prompt_parts(_fresh_workspace())
    assert first == second
