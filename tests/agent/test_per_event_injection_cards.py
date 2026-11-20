"""Per-event injection cards: goal/skill/compaction/steer/spillover/subdirectory.

Every card covers context the model received that NO other card sees: a
user message assembled by the standing-goal loop or a skill slash command,
a conversation summary that replaced pruned history, a mid-turn steer row,
a tool result that spilled to disk (the model reads a stub), and a
directory AGENTS.md hint appended on first touch. Each asserts the payload
contract — kinds, meta (the invoked skill's name), sources, content — and
the silent paths (plain messages, skip-path compressions, no-hint calls).
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
from agent.compression_facade import _emit_compaction_card
from agent.context_injection_cards import emit_card, steer_card
from agent.tool_executor import _commit_tool_result, _ToolCallRef
from agent.turn_context import build_turn_context
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

def _wire_cards(agent):
    """Record every tool_progress_callback firing on the agent."""
    events: list = []
    agent._card_events = events
    agent.tool_progress_callback = lambda *a, **k: events.append((a, k))
    return agent


def _cards(agent):
    return [
        (args[3] if len(args) > 3 else kwargs)
        for args, kwargs in getattr(agent, "_card_events", [])
        if args and args[0] == "context.injected"
    ]


def _reset_session_card_state():
    from agent import context_injection_cards as _cic

    _cic._SESSION_CARD_SHAS.clear()
    yield
    _cic._SESSION_CARD_SHAS.clear()


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


class TestGoalAndSkillCards:
    def test_goal_continuation_message_cards_as_goal(self):
        agent = _wire_cards(_FakeAgent())
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, user_message=GOAL_MESSAGE)
        kinds = [p.get("kinds") for p in _cards(agent)]
        assert ["goal"] in kinds
        goal_card = next(p for p in _cards(agent) if p.get("kinds") == ["goal"])
        assert goal_card["content"] == GOAL_MESSAGE
        assert goal_card["sources"] == ["session"]

    def test_skill_invocation_message_cards_with_name_meta(self):
        agent = _wire_cards(_FakeAgent())
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, user_message=SKILL_MESSAGE)
        skill_card = next(p for p in _cards(agent) if p.get("kinds") == ["skill"])
        assert skill_card["meta"] == {"skill": "research"}
        assert skill_card["content"] == SKILL_MESSAGE

    def test_plain_typed_message_emits_no_classification_card(self):
        agent = _wire_cards(_FakeAgent())
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, user_message="just a normal question")
        for payload in _cards(agent):
            assert "goal" not in (payload.get("kinds") or [])
            assert "skill" not in (payload.get("kinds") or [])

    def test_goal_with_memory_prefetch_cards_both_facts(self):
        agent = _wire_cards(_FakeAgent())
        agent._memory_manager = types.SimpleNamespace(
            on_turn_start=lambda *a, **k: None,
            prefetch_all=lambda query, session_id=None: "recall",
        )
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            _build(agent, user_message=GOAL_MESSAGE)
        kinds = [tuple(p.get("kinds") or []) for p in _cards(agent)]
        assert ("memory",) in kinds  # the prefetch diff card
        assert ("goal",) in kinds  # the assembled-message card


class TestCompactionCard:
    def _summary_row(self, text):
        from agent.context_compressor import SUMMARY_PREFIX

        return {"role": "user", "content": f"{SUMMARY_PREFIX}\n{text}"}

    def test_new_summary_fires_one_card(self):
        agent = _wire_cards(_FakeAgent())
        before = [{"role": "user", "content": "old turn"}]
        result = ([self._summary_row("the story so far")], "SYSTEM")
        _emit_compaction_card(agent, before, result)
        cards = [p for p in _cards(agent) if p.get("kinds") == ["compaction"]]
        assert len(cards) == 1
        assert cards[0]["content"].endswith("the story so far")
        assert cards[0]["sources"] == ["session"]

    def test_skip_path_same_list_stays_silent(self):
        # A skipped compression returns the input list: no new summary row,
        # no card — even though a prior summary may live in history.
        agent = _wire_cards(_FakeAgent())
        history = [{"role": "user", "content": "old turn"}, self._summary_row("old summary")]
        _emit_compaction_card(agent, history, (history, "SYSTEM"))
        assert not [p for p in _cards(agent) if p.get("kinds") == ["compaction"]]

    def test_byte_identical_preserved_summary_stays_silent(self):
        agent = _wire_cards(_FakeAgent())
        row = self._summary_row("kept summary")
        before = [{"role": "user", "content": "old turn"}, dict(row)]
        result = ([dict(row)], "SYSTEM")
        _emit_compaction_card(agent, before, result)
        assert not [p for p in _cards(agent) if p.get("kinds") == ["compaction"]]

    def test_result_without_summary_stays_silent(self):
        agent = _wire_cards(_FakeAgent())
        result = ([{"role": "user", "content": "pruned history"}], "SYSTEM")
        _emit_compaction_card(agent, [{"role": "user", "content": "old"}], result)
        assert not [p for p in _cards(agent) if p.get("kinds") == ["compaction"]]


class TestSteerCards:
    def _steer_agent(self):
        agent = _wire_cards(_FakeAgent())
        agent._pending_steer_texts = ["turn left at the tests"]

        def _drain():
            drained = agent._pending_steer_texts
            agent._pending_steer_texts = []
            return "\n\n".join(drained)

        agent._drain_pending_steer = _drain
        agent._notify_steer_delivery_listeners = lambda _text: None
        return agent

    def test_post_tool_batch_steer_lands_its_card(self):
        agent = self._steer_agent()
        messages = [
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        ]
        apply_pending_steer_to_tool_results(agent, messages, 1)
        steer_cards = [p for p in _cards(agent) if p.get("kinds") == ["steer"]]
        assert len(steer_cards) == 1
        assert steer_cards[0]["content"] == "turn left at the tests"
        assert steer_cards[0]["sources"] == ["user"]
        assert messages[-1]["role"] == "user"

    def test_pre_api_steer_drain_lands_its_card(self):
        from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result

        agent = self._steer_agent()
        messages = [{"role": "tool", "tool_call_id": "t1", "content": "ok"}]
        _inject_steer_after_newest_tool_result(agent, messages, "turn left")
        steer_cards = [p for p in _cards(agent) if p.get("kinds") == ["steer"]]
        assert len(steer_cards) == 1
        assert messages[1]["role"] == "user"


class TestToolResultCards:
    def _agent(self):
        agent = _wire_cards(_FakeAgent())
        agent._touch_activity = lambda _label: None
        agent._current_tool = None
        agent._append_guardrail_observation = (
            lambda name, args, result, failed=False, tool_call_id=None: result
        )
        agent._record_file_mutation_result = lambda *a, **k: None
        agent._subdirectory_hints = types.SimpleNamespace(
            check_tool_call=lambda name, args: ""
        )
        agent._tool_result_content_for_active_model = (
            lambda name, result: result
        )
        agent.verbose_logging = False
        return agent

    def _ref(self):
        return _ToolCallRef(
            name="read_file", args={"path": "x.py"}, task_id="default",
            call_id="c1", trace=[],
        )

    def _commit(self, agent, monkeypatch, *, result, hints="", spilled=None):
        from agent import tool_executor as te
        from tools.budget_config import DEFAULT_BUDGET

        monkeypatch.setattr(te, "arm_gateway_restart_control", lambda *a, **k: None)
        monkeypatch.setattr(te, "get_active_env", lambda task_id: None)
        monkeypatch.setattr(te, "_record_persisted_path_for_stub", lambda *a, **k: None)
        monkeypatch.setattr(te, "_flush_session_db_after_tool_progress", lambda *a, **k: True)
        monkeypatch.setattr(te, "_safe_callback", lambda *a, **k: None)
        agent._subdirectory_hints.check_tool_call = lambda name, args: hints
        if spilled is not None:
            monkeypatch.setattr(
                te, "maybe_persist_tool_result",
                lambda **kwargs: spilled,
            )
        messages: list = []
        _commit_tool_result(
            agent, messages, self._ref(), result,
            budget=DEFAULT_BUDGET, tool_duration=0.1, is_error=False,
            blocked=False, effect_disposition=None, observed=False,
        )
        return messages

    def test_subdirectory_hint_cards(self, monkeypatch):
        agent = self._agent()
        hints = "[Subdirectory context discovered: pkg/AGENTS.md]"
        messages = self._commit(agent, monkeypatch, result="file body", hints=hints)
        cards = [p for p in _cards(agent) if p.get("kinds") == ["subdirectory"]]
        assert len(cards) == 1
        assert cards[0]["content"] == hints
        assert cards[0]["sources"] == ["tool_result"]
        assert hints in messages[-1]["content"]

    def test_plain_result_emits_no_tool_result_cards(self, monkeypatch):
        agent = self._agent()
        self._commit(agent, monkeypatch, result="file body")
        assert not [
            p for p in _cards(agent)
            if p.get("kinds") and p["kinds"][0] in ("subdirectory", "spillover")
        ]

    def test_spilled_string_result_cards_the_stub(self, monkeypatch):
        agent = self._agent()
        stub = (
            "<persisted-output>/tmp/spill-1.txt</persisted-output>\n"
            "Output too large; read the file."
        )
        messages = self._commit(
            agent, monkeypatch, result="x" * 9000, spilled=stub,
        )
        cards = [p for p in _cards(agent) if p.get("kinds") == ["spillover"]]
        assert len(cards) == 1
        assert cards[0]["content"] == stub
        assert messages[-1]["content"] == stub

    def test_spilled_multimodal_text_part_cards_the_stub(self, monkeypatch):
        from agent import tool_executor as te

        agent = self._agent()
        stub = "<persisted-output>/tmp/spill-2.txt</persisted-output>"
        result = {"_multimodal": True, "content": [{"type": "text", "text": "y" * 9000}]}

        def _fake_persist(value, *_a, **_k):
            value["content"][0]["text"] = stub
            return value

        monkeypatch.setattr(te, "_persist_multimodal_text_parts", _fake_persist)
        messages = self._commit(agent, monkeypatch, result=result)
        cards = [p for p in _cards(agent) if p.get("kinds") == ["spillover"]]
        assert len(cards) == 1
        assert cards[0]["content"] == stub

    def test_unspilled_persist_passthrough_emits_no_card(self, monkeypatch):
        agent = self._agent()
        self._commit(
            agent, monkeypatch, result="small body", spilled="small body",
        )
        assert not [p for p in _cards(agent) if p.get("kinds") == ["spillover"]]


class TestEmitterHelper:
    def test_no_callback_is_silent_not_fatal(self):
        agent = types.SimpleNamespace()  # no tool_progress_callback at all
        assert emit_card(agent, steer_card("text")) is False

    def test_raising_callback_is_swallowed(self):
        def _boom(*a, **k):
            raise RuntimeError("presenter is broken")

        agent = types.SimpleNamespace(tool_progress_callback=_boom)
        assert emit_card(agent, steer_card("text")) is False

    def test_sources_default_to_first_kind(self):
        from agent.context_injection_cards import card_payload

        seen = []
        agent = types.SimpleNamespace(
            tool_progress_callback=lambda *a, **k: seen.append(a[3] if len(a) > 3 else k),
        )
        assert emit_card(agent, card_payload(["goal"], "text")) is True
        assert seen[0]["sources"] == ["goal"]
        assert seen[0]["injected_chars"] == 4


class TestCardPipelinePurity:
    def test_tool_message_bytes_identical_with_and_without_cards(self, monkeypatch):
        """Display plumbing stays off the model path: the tool message the
        model receives is byte-identical whether the card pipeline is wired
        (cards emitted) or absent (no callback at all)."""
        harness = TestToolResultCards()

        stub = (
            "<persisted-output>/tmp/spill-9.txt</persisted-output>\n"
            "Output too large; read the file."
        )
        hints = "[Subdirectory context discovered: pkg/AGENTS.md]"

        quiet = harness._agent()
        quiet.tool_progress_callback = None
        wired = _wire_cards(harness._agent())

        quiet_msgs = harness._commit(
            quiet, monkeypatch, result="x" * 9000, hints=hints, spilled=stub,
        )
        wired_msgs = harness._commit(
            wired, monkeypatch, result="x" * 9000, hints=hints, spilled=stub,
        )

        def _sans_clock(msgs):
            return [{k: v for k, v in m.items() if k != "timestamp"} for m in msgs]

        assert _sans_clock(wired_msgs) == _sans_clock(quiet_msgs)
        assert [m["content"] for m in wired_msgs] == [m["content"] for m in quiet_msgs]
        assert _cards(wired), "cards rode the callback on the wired agent"
        assert not _cards(quiet)
