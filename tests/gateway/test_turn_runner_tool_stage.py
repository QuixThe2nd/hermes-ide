"""Regression: the TurnRunner that run_turn._run_agent_inner builds must carry
the tool-stage embed API it subscribes to.

#569 kept run_turn._run_agent_inner (which subscribes
``turn_runner.tool_stage_event_callback`` and drains
``turn_runner.send_tool_stage_embeds()``) but those members only existed on the
gateway.run facade's TurnRunner, so every gateway turn raised AttributeError.
"""

import inspect

import gateway.run_turn as run_turn
from gateway.run_turn_runner import TurnRunner

USED_BY_RUN_AGENT_INNER = ("tool_stage_event_callback", "send_tool_stage_embeds")


def test_run_turn_builds_turn_runner_from_run_turn_runner():
    src = inspect.getsource(run_turn)
    assert "from gateway.run_turn_runner import TurnRunner" in src


def test_turn_runner_has_tool_stage_members_used_by_run_agent_inner():
    src = inspect.getsource(run_turn)
    for name in USED_BY_RUN_AGENT_INNER:
        assert f"turn_runner.{name}" in src, f"run_turn no longer uses {name}; update this test"
        assert callable(getattr(TurnRunner, name, None)), f"TurnRunner is missing {name}"


def test_turn_runner_covers_facade_tool_stage_members():
    import gateway.run as facade

    stage = {
        n for n in dir(facade.TurnRunner)
        if "stage" in n.lower() or n == "_deliver_agent_status_marker"
    }
    missing = sorted(n for n in stage if not hasattr(TurnRunner, n))
    assert not missing, f"run_turn_runner.TurnRunner is missing facade tool-stage members: {missing}"
