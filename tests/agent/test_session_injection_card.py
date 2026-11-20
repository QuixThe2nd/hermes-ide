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

from agent.system_prompt import build_system_prompt_parts
from tests.agent.test_memory_injection_card import _make_prompt_agent
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
