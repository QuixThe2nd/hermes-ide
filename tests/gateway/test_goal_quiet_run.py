"""Quiet goal runs: the gateway seam and its fail-open behavior.

The delivery rule itself lives in internal/goalrun (Go, tested there).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import goal_run_plan, run_goals
from gateway.run import GatewayRunner


class FakeGoalManager:
    def __init__(self, decision):
        self._decision = decision
        self.state = SimpleNamespace(goal="ship it", turns_used=3, max_turns=10)

    def is_active(self):
        return True

    def evaluate_after_turn(self, *args, **kwargs):
        return dict(self._decision)


async def _noop(*args, **kwargs):
    return None


def _runner(monkeypatch, decision):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._warm_goals_session_db = _noop
    runner._defer_goal_status_notice_after_delivery = _noop
    runner._adapter_for_source = lambda source: None
    monkeypatch.setattr("hermes_cli.goals.GoalManager", lambda **kw: FakeGoalManager(decision))
    return runner


def _stub_plan(monkeypatch):
    """Plan stub mirroring the Go policy: quiet only for a continuing internal turn."""
    calls = []

    async def fake_plan(**kwargs):
        calls.append(kwargs)
        if not kwargs["active"]:
            return dict(goal_run_plan.EMPTY_PLAN)
        return {
            "suppress": kwargs["should_continue"],
            "final": not kwargs["should_continue"],
            "objective": kwargs["objective"],
            "line": "🎯 turn 3/10 · ship it",
        }

    monkeypatch.setattr(run_goals, "plan_for_turn_async", fake_plan)
    return calls


def _capture_hooks(monkeypatch):
    seen = []
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kw: seen.append((name, kw)))
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,internal,suppress,hook_done",
    [
        ({"should_continue": True, "continuation_prompt": "keep going"}, True, True, False),
        ({"should_continue": False}, True, False, True),
        ({"should_continue": True, "continuation_prompt": "keep going"}, False, False, None),
    ],
)
async def test_goal_turn_delivery(monkeypatch, decision, internal, suppress, hook_done):
    runner = _runner(monkeypatch, decision)
    calls = _stub_plan(monkeypatch)
    hooks = _capture_hooks(monkeypatch)

    result = await runner._post_turn_goal_continuation(
        session_entry=SimpleNamespace(session_id="sess-1"),
        source=SimpleNamespace(platform="discord"),
        final_response="step 3 done",
        is_internal=internal,
    )

    assert result is suppress
    assert calls[0]["active"] is internal
    if hook_done is None:  # a human message always gets its answer, no bar line
        assert hooks == []
    else:
        assert hooks == [(
            "on_goal_progress",
            {
                "line": "🎯 turn 3/10 · ship it",
                "objective": "ship it",
                "done": hook_done,
                "session_id": "sess-1",
                "platform": "discord",
            },
        )]


def test_plan_is_fail_open_without_a_go_toolchain(monkeypatch):
    monkeypatch.setattr(goal_run_plan, "_binary_path", lambda: None)
    plan = goal_run_plan.plan_for_turn(active=True, should_continue=True, turn=3, max_turns=10)
    assert plan == goal_run_plan.EMPTY_PLAN
