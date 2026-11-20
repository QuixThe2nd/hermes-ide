"""Per-event injection cards fire at each chokepoint that injects context.

Goal/skill user messages, committed compaction summaries, mid-turn steers,
spilled tool results and subdirectory AGENTS.md hints each card exactly once
at the code path that lands them, and stay silent on the paths that inject
nothing. Payload shapes are pinned against the Go twin by the shared table in
tests/gateway/test_context_injection_labels.py; these assert the wiring.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
from agent.compression_facade import _emit_compaction_card
from agent.context_injection_cards import emit_card, event_card
from agent.tool_executor import _commit_tool_result, _ToolCallRef
from tests.agent.test_gateway_turn_sidecar import _FakeAgent, _build

GOAL_MESSAGE = (
    "[Continuing toward your standing goal]\n"
    "Goal: ship the release\n\n"
    "Continue working toward this goal. Take the next concrete step."
)
SKILL_MESSAGE = (
    'Leading text\n'
    '[IMPORTANT: The user has invoked the "research" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n"
    "# Research Skill\nDo the thing."
)
STUB = "<persisted-output>/tmp/spill-1.txt</persisted-output>\nOutput too large; read the file."
HINTS = "[Subdirectory context discovered: pkg/AGENTS.md]"


def _wire_cards(agent):
    events: list = []
    agent._card_events = events
    agent.tool_progress_callback = lambda *a, **k: events.append((a, k))
    return agent


def _cards(agent, kind=None):
    payloads = [
        (args[3] if len(args) > 3 else kwargs)
        for args, kwargs in getattr(agent, "_card_events", [])
        if args and args[0] == "context.injected"
    ]
    return [p for p in payloads if kind is None or p.get("kinds") == [kind]]


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


@pytest.mark.parametrize(
    "message,kind,meta",
    [
        pytest.param(GOAL_MESSAGE, "goal", None, id="goal"),
        pytest.param(SKILL_MESSAGE, "skill", {"skill": "research"}, id="skill"),
        pytest.param("just a normal question", None, None, id="typed"),
    ],
)
def test_assembled_user_message_cards_at_turn_prologue(message, kind, meta):
    agent = _wire_cards(_FakeAgent())
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent, user_message=message)
    assembled = [p for p in _cards(agent) if p.get("kinds") in (["goal"], ["skill"])]
    if kind is None:
        assert assembled == []
        return
    assert [(p["kinds"], p["content"], p.get("meta")) for p in assembled] == [
        ([kind], message, meta)
    ]


def test_goal_with_memory_prefetch_cards_both_facts():
    agent = _wire_cards(_FakeAgent())
    agent._memory_manager = types.SimpleNamespace(
        on_turn_start=lambda *a, **k: None,
        prefetch_all=lambda query, session_id=None: "recall",
    )
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        _build(agent, user_message=GOAL_MESSAGE)
    kinds = [tuple(p.get("kinds") or []) for p in _cards(agent)]
    assert ("memory",) in kinds and ("goal",) in kinds


def _summary_row(text):
    from agent.context_compressor import SUMMARY_PREFIX

    return {"role": "user", "content": f"{SUMMARY_PREFIX}\n{text}"}


_OLD = {"role": "user", "content": "old turn"}


@pytest.mark.parametrize(
    "before,after,fires",
    [
        pytest.param([_OLD], [_summary_row("the story so far")], True, id="new_summary"),
        pytest.param([_OLD, _summary_row("kept")], [_summary_row("kept")], False,
                     id="preserved_summary"),
        pytest.param([_OLD], [{"role": "user", "content": "pruned"}], False,
                     id="no_summary"),
    ],
)
def test_compaction_chokepoint_cards_only_new_summaries(before, after, fires):
    agent = _wire_cards(_FakeAgent())
    _emit_compaction_card(agent, before, (after, "SYSTEM"))
    assert [p["content"] for p in _cards(agent, "compaction")] == (
        [after[0]["content"]] if fires else []
    )


def test_compaction_skip_path_same_list_stays_silent():
    agent = _wire_cards(_FakeAgent())
    history = [_OLD, _summary_row("old summary")]
    _emit_compaction_card(agent, history, (history, "SYSTEM"))
    assert _cards(agent, "compaction") == []


def _post_tool_batch(agent, messages):
    apply_pending_steer_to_tool_results(agent, messages, 1)


def _pre_api_drain(agent, messages):
    from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result

    _inject_steer_after_newest_tool_result(agent, messages, agent._drain_pending_steer())


@pytest.mark.parametrize("deliver", [_post_tool_batch, _pre_api_drain])
def test_steer_chokepoints_card_the_steer_row(deliver):
    agent = _wire_cards(_FakeAgent())
    agent._pending_steer_texts = ["turn left at the tests"]

    def _drain():
        drained, agent._pending_steer_texts = agent._pending_steer_texts, []
        return "\n\n".join(drained)

    agent._drain_pending_steer = _drain
    agent._notify_steer_delivery_listeners = lambda _text: None
    messages = [
        {"role": "assistant", "tool_calls": []},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    deliver(agent, messages)
    assert [p["content"] for p in _cards(agent, "steer")] == ["turn left at the tests"]
    assert messages[-1]["role"] == "user"


def _tool_agent():
    agent = _FakeAgent()
    agent._touch_activity = lambda _label: None
    agent._current_tool = None
    agent._append_guardrail_observation = lambda name, args, result, **_k: result
    agent._record_file_mutation_result = lambda *a, **k: None
    agent._subdirectory_hints = types.SimpleNamespace(check_tool_call=lambda n, a: "")
    agent._tool_result_content_for_active_model = lambda name, result: result
    agent.verbose_logging = False
    return agent


def _commit(agent, monkeypatch, *, result, hints="", spilled=None, persist_parts=None):
    from agent import tool_executor as te
    from tools.budget_config import DEFAULT_BUDGET

    monkeypatch.setattr(te, "arm_gateway_restart_control", lambda *a, **k: None)
    monkeypatch.setattr(te, "get_active_env", lambda task_id: None)
    monkeypatch.setattr(te, "_record_persisted_path_for_stub", lambda *a, **k: None)
    monkeypatch.setattr(te, "_flush_session_db_after_tool_progress", lambda *a, **k: True)
    monkeypatch.setattr(te, "_safe_callback", lambda *a, **k: None)
    agent._subdirectory_hints.check_tool_call = lambda name, args: hints
    if spilled is not None:
        monkeypatch.setattr(te, "maybe_persist_tool_result", lambda **kwargs: spilled)
    if persist_parts is not None:
        monkeypatch.setattr(te, "_persist_multimodal_text_parts", persist_parts)
    messages: list = []
    ref = _ToolCallRef(name="read_file", args={"path": "x.py"}, task_id="default",
                       call_id="c1", trace=[])
    _commit_tool_result(
        agent, messages, ref, result, budget=DEFAULT_BUDGET, tool_duration=0.1,
        is_error=False, blocked=False, effect_disposition=None, observed=False,
    )
    return messages


def _stub_first_part(value, *_a, **_k):
    value["content"][0]["text"] = STUB
    return value


@pytest.mark.parametrize(
    "commit_kwargs,expected",
    [
        pytest.param({"result": "file body"}, [], id="plain"),
        pytest.param({"result": "small", "spilled": "small"}, [], id="unspilled_passthrough"),
        pytest.param({"result": "file body", "hints": HINTS}, [("subdirectory", HINTS)],
                     id="subdirectory_hint"),
        pytest.param({"result": "x" * 9000, "spilled": STUB}, [("spillover", STUB)],
                     id="spilled_string"),
        pytest.param(
            {"result": {"_multimodal": True, "content": [{"type": "text", "text": "y" * 9000}]},
             "persist_parts": _stub_first_part},
            [("spillover", STUB)], id="spilled_multimodal_part",
        ),
    ],
)
def test_tool_result_chokepoint_cards(commit_kwargs, expected, monkeypatch):
    agent = _wire_cards(_tool_agent())
    _commit(agent, monkeypatch, **commit_kwargs)
    assert [(p["kinds"][0], p["content"]) for p in _cards(agent)] == expected


def test_tool_message_bytes_identical_with_and_without_cards(monkeypatch):
    """Display plumbing stays off the model path: the tool message is
    byte-identical whether the card pipeline is wired or absent."""
    quiet = _tool_agent()
    quiet.tool_progress_callback = None
    wired = _wire_cards(_tool_agent())
    kwargs = dict(result="x" * 9000, hints=HINTS, spilled=STUB)
    quiet_msgs = _commit(quiet, monkeypatch, **kwargs)
    wired_msgs = _commit(wired, monkeypatch, **kwargs)

    def _sans_clock(msgs):
        return [{k: v for k, v in m.items() if k != "timestamp"} for m in msgs]

    assert _sans_clock(wired_msgs) == _sans_clock(quiet_msgs)
    assert _cards(wired) and not _cards(quiet)


@pytest.mark.parametrize(
    "agent",
    [
        pytest.param(types.SimpleNamespace(), id="no_callback"),
        pytest.param(
            types.SimpleNamespace(tool_progress_callback=lambda *a, **k: 1 / 0),
            id="raising_callback",
        ),
    ],
)
def test_emitter_never_raises(agent):
    assert emit_card(agent, event_card("steer", "text")) is False
