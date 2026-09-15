"""A successful gateway ``restart`` result is terminal for its calling turn.

Reproduced bug (2026-09-04): ``handle_restart`` queued the drain and returned
ordinary success JSON; the conversation loop then treated it like any other
tool result, called the provider again, and executed more tools — so the
requester's own turn kept creating work inside the drain that was waiting for
that same turn to finish (~4 minutes of churn after the exact-word
confirmation).

The contract under test is a TYPED seam, never prose:

* only ``restarting`` / ``already_in_progress`` results stamp the reserved
  ``_hermes_turn_control`` field (the restart plugin's ``_result_json``) and
  arm the per-turn flag (``agent/turn_control``);
* an armed flag ends the turn in ``conversation_loop`` with a typed
  ``gateway_restart_queued`` exit, an empty ``final_response``, and ZERO
  further provider calls;
* later sibling tool calls never start — later calls in a sequential batch
  and later planner segments alike — but every unstarted call still gets a
  paired no-effect tool result (protocol pairing / role alternation);
* cancelled / failed restart results are not terminal: the normal
  provider/tool loop continues after them;
* the flag belongs to its turn only — the next turn starts unconstrained.

These tests drive the REAL boundaries: ``run_conversation`` with a mocked
provider client, ``run_agent.handle_function_call`` patched to return the
restart plugin's genuine ``_result_json`` payload, a real ``SessionDB`` for
durability assertions, and the real segment planner choosing dispatch paths.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_control import (
    GATEWAY_RESTART_CONTROL,
    TURN_CONTROL_FIELD,
    arm_gateway_restart_control,
    is_gateway_restart_armed,
    turn_control_field_for,
)
from hermes_state import SessionDB
from plugins.gateway_restart.tool import _cancelled_json, _error_json, _result_json
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent(tmp_path: Path):
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "logs").mkdir(parents=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search", "terminal", "restart"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _attach_real_session_db(agent, db_path: Path, session_id: str) -> SessionDB:
    db = SessionDB(db_path=db_path)
    db.create_session(session_id=session_id, source="tui", model="test/model")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    return db


def _durable_messages(db_path: Path, session_id: str) -> list[dict]:
    restarted_db = SessionDB(db_path=db_path)
    try:
        return restarted_db.get_messages_as_conversation(session_id)
    finally:
        restarted_db.close()


def _tool_call(name="restart", arguments="{}", call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _restart_success_json(status="restarting"):
    """The restart plugin's genuine success payload for a committed drain."""
    return _result_json(
        SimpleNamespace(_draining=True),
        {"status": status, "active_agents": 1, "via_service": False},
    )


def _restart_cancelled_json():
    return _cancelled_json("Reply was not the exact word 'restart'.")


def _restart_error_json():
    return _error_json("No live gateway runner is available.")


def _run_turn(agent, dispatch, tmp_path, session_id, user_message="restart please"):
    """Drive one real run_conversation turn; return its result dict."""
    db_path = tmp_path / "state.db"
    db = _attach_real_session_db(agent, db_path, session_id)
    try:
        with (
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.handle_function_call", side_effect=dispatch),
            patch(
                "agent.tool_executor.maybe_persist_tool_result",
                side_effect=lambda **kwargs: kwargs["content"],
            ),
            # The fork's end-of-turn "inbox sparks" nudge is an
            # environment-dependent plugin (one directive per cooldown
            # window, keyed to HERMES_HOME state) — neutralize it so the
            # provider-call counts here depend only on the restart control.
            patch(
                "hermes_cli.plugins.get_pre_turn_end_continue_message",
                return_value=None,
            ),
        ):
            return agent.run_conversation(user_message)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The typed control seam itself (agent/turn_control.py)
# ---------------------------------------------------------------------------


class TestArmGatewayRestartControl:
    def test_only_the_restart_status_pairs_stamp_the_control_field(self):
        assert turn_control_field_for("restarting") == {
            TURN_CONTROL_FIELD: GATEWAY_RESTART_CONTROL
        }
        assert turn_control_field_for("already_in_progress") == {
            TURN_CONTROL_FIELD: GATEWAY_RESTART_CONTROL
        }
        for non_terminal in ("cancelled", "failed", None, ""):
            assert turn_control_field_for(non_terminal) is None

    def test_arms_from_a_restart_result_carrying_the_field(self):
        agent = SimpleNamespace(_turn_gateway_restart_queued=False)
        armed = arm_gateway_restart_control(
            agent, "restart", _restart_success_json()
        )
        assert armed is True
        assert is_gateway_restart_armed(agent) is True

    def test_rejects_wrong_tool_name(self):
        # A different tool emitting the exact reserved field must not arm —
        # the contract is tool name AND field, so untrusted outputs cannot
        # fabricate the terminal control.
        agent = SimpleNamespace(_turn_gateway_restart_queued=False)
        assert (
            arm_gateway_restart_control(
                agent, "web_search", _restart_success_json()
            )
            is False
        )
        assert is_gateway_restart_armed(agent) is False

    def test_rejects_non_json_and_non_dict_payloads(self):
        agent = SimpleNamespace(_turn_gateway_restart_queued=False)
        for result in ("not json {", "[]", '"a string"', None, 42):
            assert arm_gateway_restart_control(agent, "restart", result) is False
        assert is_gateway_restart_armed(agent) is False

    def test_rejects_cancelled_and_failed_restart_payloads(self):
        agent = SimpleNamespace(_turn_gateway_restart_queued=False)
        for result in (_restart_cancelled_json(), _restart_error_json()):
            assert arm_gateway_restart_control(agent, "restart", result) is False
        assert is_gateway_restart_armed(agent) is False

    def test_wrong_control_value_is_a_noop(self):
        agent = SimpleNamespace(_turn_gateway_restart_queued=False)
        forged = json.dumps(
            {TURN_CONTROL_FIELD: "something_else", "status": "restarting"}
        )
        assert arm_gateway_restart_control(agent, "restart", forged) is False

    def test_already_armed_returns_false_without_rearming(self):
        agent = SimpleNamespace(_turn_gateway_restart_queued=True)
        assert (
            arm_gateway_restart_control(agent, "restart", _restart_success_json())
            is False
        )
        assert is_gateway_restart_armed(agent) is True


# ---------------------------------------------------------------------------
# Success: the turn ends at the restart result — zero further provider calls
# ---------------------------------------------------------------------------


def test_restart_success_ends_turn_with_zero_further_provider_calls(tmp_path):
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_tool_call("restart", call_id="call_restart")],
        ),
        # Must never be requested: the armed control ends the turn first.
        _response(content="The restart is queued."),
    ]

    result = _run_turn(agent, lambda *a, **k: _restart_success_json(), tmp_path,
                       "restart-terminal-success")

    # ZERO further provider calls after the committed restart result.
    assert agent.client.chat.completions.create.call_count == 1
    # Silent final response — the restart lifecycle UI is the only output.
    assert result["final_response"] == ""
    # Typed exit reason + typed result bit, never prose matching.
    assert result["turn_exit_reason"] == "gateway_restart_queued"
    assert result["gateway_restart_queued"] is True
    assert result["failed"] is False
    # The per-turn flag is armed (and only for this turn — see below).
    assert agent._turn_gateway_restart_queued is True


def test_restart_success_persists_tool_rows_and_keeps_alternation(tmp_path):
    """The assistant tool-call row and the restart result row are durable."""
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_tool_call("restart", call_id="call_restart")],
        ),
    ]

    _run_turn(agent, lambda *a, **k: _restart_success_json(), tmp_path,
              "restart-terminal-durable")

    durable = _durable_messages(tmp_path / "state.db", "restart-terminal-durable")
    roles = [m["role"] for m in durable]
    # user → assistant(tool_calls) → tool(restart result) → closing assistant.
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert durable[1]["tool_calls"][0]["id"] == "call_restart"
    restart_row = durable[2]
    assert restart_row["tool_call_id"] == "call_restart"
    # The persisted tool result carries the reserved typed control field.
    payload = json.loads(restart_row["content"])
    assert payload[TURN_CONTROL_FIELD] == GATEWAY_RESTART_CONTROL
    assert payload["status"] == "restarting"
    # The closing assistant row is transcript bookkeeping (the interrupted
    # tool tail is closed so role alternation survives the post-restart
    # turn), not a delivered response.
    assert "restart" in durable[3]["content"].lower()


def test_already_in_progress_is_terminal_too(tmp_path):
    """A concurrent restart already draining also ends its caller's turn —
    continuing that turn would delay the active drain."""
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_tool_call("restart", call_id="call_restart")],
        ),
        _response(content="never requested"),
    ]

    result = _run_turn(agent, lambda *a, **k: _restart_success_json(
        status="already_in_progress"), tmp_path, "restart-terminal-in-progress")

    assert agent.client.chat.completions.create.call_count == 1
    assert result["final_response"] == ""
    assert result["turn_exit_reason"] == "gateway_restart_queued"
    assert result["gateway_restart_queued"] is True


# ---------------------------------------------------------------------------
# Cancel / failure: NOT terminal — the normal loop continues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["cancelled", "error"],
                         ids=["cancelled", "error"])
def test_cancelled_or_failed_restart_continues_the_normal_loop(tmp_path, payload):
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_tool_call("restart", call_id="call_restart")],
        ),
        _response(content="Okay — staying up then."),
    ]
    restart_result = (
        _restart_cancelled_json() if payload == "cancelled" else _restart_error_json()
    )

    result = _run_turn(agent, lambda *a, **k: restart_result, tmp_path,
                       f"restart-nonterminal-{payload}")

    # The provider continuation happened exactly as for any other tool.
    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "Okay — staying up then."
    assert result["turn_exit_reason"].startswith("text_response")
    assert "gateway_restart_queued" not in result
    assert agent._turn_gateway_restart_queued is False
    # The persisted restart result carries NO control field.
    durable = _durable_messages(
        tmp_path / "state.db", f"restart-nonterminal-{payload}"
    )
    tool_rows = [m for m in durable if m["role"] == "tool"]
    assert len(tool_rows) == 1
    assert TURN_CONTROL_FIELD not in json.loads(tool_rows[0]["content"])


# ---------------------------------------------------------------------------
# Batches: later siblings never start, but stay protocol-paired
# ---------------------------------------------------------------------------


def test_later_sequential_sibling_never_runs_and_gets_paired_no_effect_result(
    tmp_path,
):
    """A side-effecting sibling after the restart is never invoked, and
    receives an explicit skipped/no-effect tool result for its call id."""
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call("restart", call_id="call_restart"),
                _tool_call("terminal", '{"command":"touch side-effect"}',
                           call_id="call_terminal"),
            ],
        ),
        _response(content="never requested"),
    ]
    dispatched: list[str] = []

    def _dispatch(function_name, function_args, effective_task_id, **kwargs):
        dispatched.append(function_name)
        if function_name == "restart":
            return _restart_success_json()
        return json.dumps({"ok": True})

    result = _run_turn(agent, _dispatch, tmp_path, "restart-skip-sequential")

    # Only the restart dispatched — the terminal sibling never started.
    assert dispatched == ["restart"]
    assert agent.client.chat.completions.create.call_count == 1
    assert result["turn_exit_reason"] == "gateway_restart_queued"

    # Protocol pairing: exactly one tool result per emitted call, in order,
    # and the skipped sibling's result is marked no-effect.
    tool_rows = [m for m in result["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_rows] == [
        "call_restart",
        "call_terminal",
    ]
    skipped = tool_rows[1]
    assert skipped["name"] == "terminal"
    assert skipped["effect_disposition"] == "none"
    assert "skipped" in skipped["content"].lower()
    assert "restart" in skipped["content"].lower()


def test_restart_in_earlier_segment_drains_later_parallel_segment(tmp_path):
    """restart is a sequential barrier: an earlier-segment restart must
    drain a LATER parallel segment without executing any of its calls."""
    from agent.tool_dispatch_helpers import _plan_tool_batch_segments

    calls = [
        _tool_call("restart", call_id="call_restart"),
        _tool_call("web_search", '{"query":"a"}', call_id="call_s1"),
        _tool_call("web_search", '{"query":"b"}', call_id="call_s2"),
    ]
    # The planner really puts restart before a parallel run — this is the
    # segmented dispatch shape the test relies on.
    segments = _plan_tool_batch_segments(calls)
    assert [kind for kind, _ in segments] == ["sequential", "parallel"]

    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(content="", finish_reason="tool_calls", tool_calls=calls),
        _response(content="never requested"),
    ]
    dispatched: list[str] = []

    def _dispatch(function_name, function_args, effective_task_id, **kwargs):
        dispatched.append(function_name)
        if function_name == "restart":
            return _restart_success_json()
        return json.dumps({"ok": True})

    result = _run_turn(agent, _dispatch, tmp_path, "restart-skip-segments")

    assert dispatched == ["restart"]
    assert result["turn_exit_reason"] == "gateway_restart_queued"
    tool_rows = [m for m in result["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_rows] == [
        "call_restart",
        "call_s1",
        "call_s2",
    ]
    for skipped in tool_rows[1:]:
        assert skipped["effect_disposition"] == "none"
        assert "skipped" in skipped["content"].lower()


# ---------------------------------------------------------------------------
# The flag belongs to exactly one turn
# ---------------------------------------------------------------------------


def test_flag_does_not_leak_into_the_next_turn(tmp_path):
    """build_turn_context reopens the loop unconstrained: the turn AFTER a
    terminal restart behaves normally (provider called, response delivered)."""
    agent = _make_agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_tool_call("restart", call_id="call_restart")],
        ),
        _response(content="Back online — what next?"),
    ]

    first = _run_turn(agent, lambda *a, **k: _restart_success_json(), tmp_path,
                      "restart-turn-scoped")
    assert first["gateway_restart_queued"] is True
    assert agent._turn_gateway_restart_queued is True

    second = _run_turn(agent, lambda *a, **k: json.dumps({"ok": True}),
                       tmp_path, "restart-turn-scoped",
                       user_message="hello again")
    assert agent.client.chat.completions.create.call_count == 2
    assert second["final_response"] == "Back online — what next?"
    assert "gateway_restart_queued" not in second
    assert agent._turn_gateway_restart_queued is False
