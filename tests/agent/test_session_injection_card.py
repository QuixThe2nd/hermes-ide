"""Session-context injection card: the once-per-shape system-prompt inventory.

``build_system_prompt_parts`` records which sections landed
(``agent._system_prompt_section_manifest``); the turn prologue renders it as
ONE sha-gated ``context.injected`` card — first turn and first turn after a
shape change fire, steady state stays silent. The prompt bytes themselves
stay identical to the pre-card scaffold.
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
from tests.agent.test_gateway_turn_sidecar import _FakeAgent, _build


@pytest.fixture(autouse=True)
def _reset_session_card_state():
    """The hash registry is process state; fake agents share "sess-1"."""
    from agent import context_injection_cards as _cic

    _cic._SESSION_CARD_SHAS.clear()
    yield
    _cic._SESSION_CARD_SHAS.clear()


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _session_cards(agent):
    return [
        payload
        for args, payload in agent._events
        if args[0] == "context.injected" and payload.get("kinds") == ["session"]
    ]


def _agent(rows=None, **extra):
    agent = _FakeAgent()
    if rows is not None:
        agent._system_prompt_section_manifest = rows
    for name, value in extra.items():
        setattr(agent, name, value)
    agent._events = []
    agent.tool_progress_callback = lambda *a, **k: agent._events.append(
        (a, a[3] if len(a) > 3 else k)
    )
    return agent


def _turn(agent, **overrides):
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent, **overrides)


_SOUL = [("identity", "soul")]


def _same_shape(agent):
    pass


def _gain_section(agent):
    agent._system_prompt_section_manifest = [*_SOUL, ("skills_index", None)]


def _gain_ephemeral(agent):
    agent.ephemeral_system_prompt = "SESSION CONTEXT"


@pytest.mark.parametrize(
    "mutate,second_turn_cards",
    [
        pytest.param(_same_shape, [], id="steady_state_silent"),
        pytest.param(
            _gain_section, ["🪪 SOUL.md\n📚 Skills index\n🔧 2 tool schemas"],
            id="section_change",
        ),
        pytest.param(
            _gain_ephemeral,
            ["🪪 SOUL.md\n🧵 Session context (ephemeral)\n🔧 2 tool schemas"],
            id="ephemeral_change",
        ),
    ],
)
def test_session_card_fires_once_per_prompt_shape(mutate, second_turn_cards):
    agent = _agent(list(_SOUL), tools=[{}, {}])
    _turn(agent)
    first = _session_cards(agent)
    assert [c["content"] for c in first] == ["🪪 SOUL.md\n🔧 2 tool schemas"]
    assert first[0]["sources"] == ["session"]
    mutate(agent)
    _turn(agent)
    assert [c["content"] for c in _session_cards(agent)[1:]] == second_turn_cards


def test_unknown_manifest_kind_keeps_raw_rendering():
    """A manifest kind the label table does not know renders as the raw
    ``kind`` / ``kind (detail)`` text, so future manifest rows stay
    additive-safe instead of blanking the row."""
    agent = _agent([("future_section", None), ("mystery", "3")])
    _turn(agent)
    assert [c["content"] for c in _session_cards(agent)] == [
        "future_section\nmystery (3)"
    ]


def test_fresh_agent_same_session_does_not_reemit():
    # The api_server builds a fresh agent per turn; only the process-scoped
    # registry keeps the card silent for unchanged prompt shapes.
    first, twin = _agent(list(_SOUL)), _agent(list(_SOUL))
    _turn(first)
    _turn(twin)
    assert (len(_session_cards(first)), len(_session_cards(twin))) == (1, 0)


@pytest.mark.parametrize(
    "rows,overrides",
    [
        pytest.param(None, {}, id="restore_path_no_manifest"),
        pytest.param(_SOUL, {"moa_active": True}, id="moa_turn"),
    ],
)
def test_session_card_stays_silent(rows, overrides):
    agent = _agent(rows)
    _turn(agent, **overrides)
    assert _session_cards(agent) == []


def _manifest_kinds(**agent_kwargs):
    agent = _make_prompt_agent(**agent_kwargs)
    with patch("agent.prompt_builder.build_environment_hints", return_value=""):
        build_system_prompt_parts(agent)
    return [kind for kind, _detail in agent._system_prompt_section_manifest], agent


def test_manifest_records_what_landed_in_prompt_order():
    kinds, agent = _manifest_kinds(_memory_enabled=True, _user_profile_enabled=True)
    assert kinds[0] == "identity" and kinds[-1] == "timestamp"
    assert dict(agent._system_prompt_section_manifest)["identity"] == "default"
    # Unlanded parts claim no row; memory rides its own sha-gated card.
    assert not {"context_files", "workspace", "memory"} & set(kinds)


# ---------------------------------------------------------------------------
# Byte identity against the pre-card scaffold (6ac6f2c9)
# ---------------------------------------------------------------------------

_BASE_COMMIT = "6ac6f2c9"
_SOUL_BYTES = "You are Hermes Agent — pinned soul identity bytes."


_PINNED = {
    "agent.prompt_builder.load_soul_md": _SOUL_BYTES,
    "agent.prompt_builder.build_environment_hints": "",
    "agent.prompt_builder.build_context_files_prompt": "PROJECT CONTEXT BYTES",
    "agent.prompt_builder.build_skills_system_prompt": "SKILLS INDEX BYTES",
    "agent.skill_commands.build_auto_load_prompt": ("", [], []),
    "agent.coding_context.coding_system_prompt_parts": (
        ["CODING PREFIX"], ["WORKSPACE SNAPSHOT"], ["CODING TRAILING"],
    ),
    "agent.coding_context.coding_compact_skill_categories": frozenset(),
    "tools.bot_mode_probe.get_bot_mode_protocol_section": "BOT MODE PROTOCOL",
    "tools.bot_mode_probe.epoch_line": "EPOCH LINE",
    "hermes_cli.plugins.format_system_prompt_sections": "PLUGIN SECTIONS RENDERED",
}


@pytest.fixture
def pinned_dependencies():
    """Dependency surface both scaffold versions read: pinned so the only
    variable between them is the scaffold itself."""
    import contextlib

    with contextlib.ExitStack() as stack:
        for target, value in _PINNED.items():
            stack.enter_context(patch(target, return_value=value))
        yield


def _fresh_workspace():
    agent = _make_prompt_agent(
        valid_tool_names=["skills_list", "skill_view", "terminal"],
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_store=_FakeMemoryStore({"memory": "MEMORY-BLOCK", "user": "PROFILE-BLOCK"}),
    )
    agent._plugin_system_prompt_sections_snapshot = (
        types.SimpleNamespace(position="after_memory"),
    )
    return agent


def _fresh_bot_mode():
    agent = _make_prompt_agent(_bot_mode_protocol=True)
    agent._session_title_hint = "Bot Chat"
    return agent


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


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(_make_prompt_agent, id="default"),
        pytest.param(
            lambda: _make_prompt_agent(load_soul_identity=True, skip_context_files=True),
            id="soul_identity",
        ),
        pytest.param(_fresh_workspace, id="workspace_skills_memory_plugins"),
        pytest.param(_fresh_bot_mode, id="bot_mode"),
    ],
)
def test_prompt_bytes_identical_to_pre_card_scaffold(
    factory, base_system_prompt, pinned_dependencies
):
    """Manifest recording is pure scaffolding: every tier and the cached full
    prompt are byte-identical to the scaffold before the card existed
    (6ac6f2c9), and a stale manifest attribute is replaced, never consulted."""
    stale = factory()
    stale._system_prompt_section_manifest = [("junk", "stale")]
    current_parts = build_system_prompt_parts(stale, system_message=None)
    assert current_parts == base_system_prompt.build_system_prompt_parts(
        factory(), system_message=None
    )
    assert ("junk", "stale") not in stale._system_prompt_section_manifest
    assert build_system_prompt(factory()) == base_system_prompt.build_system_prompt(factory())
