"""Regression tests for transparent forced-interruption recovery.

Incident: a session forcibly interrupted by a gateway bounce (drain-timeout
restart/shutdown, crash) resumed with an LLM-visible "[System note: The
previous turn was interrupted by a gateway restart ...]" wrapper, so the
model narrated the outage instead of finishing the turn.  The contract:

* Only sessions that accepted ``COOPERATIVE_RESTART_STEER`` see restart
  guidance; every forced reason recovers below the LLM boundary with ZERO
  synthetic rows — no note, no synthetic user message, no blank user row.
* The unresolved / explicitly interrupted calls of the FINAL assistant
  tool batch re-run through the normal dispatcher with their original
  name, call id, and arguments — LITERALLY, with no tool-name whitelist:
  side-effecting victim calls replay too.  Completed results are
  preserved; each replacement persists exactly once; the turn continues
  through the ordinary in-loop seam.
* Only the lifecycle request that caused the bounce (the ``restart`` tool
  or a gateway-lifecycle shell command) never replays — that is the
  self-restart loop guard, not a safety whitelist.
* Real user text stays clean and queues AFTER the completed batch; strict
  provider pairing stays valid.

Helper-level proofs live here; the model-facing end-to-end proof
(``TurnRunner.run_sync`` with a stubbed agent at the provider boundary)
lives in ``test_forced_resume_integration.py``.  History-level outcomes go
through the REAL ``_build_gateway_agent_history`` pipeline (strippers
included) exactly like ``tests/gateway/test_auto_continue.py``.
"""

import pytest

from agent.replay_cleanup import (
    dedupe_tool_results_keep_last,
    is_interrupted_tool_result,
    strip_interrupted_tool_tails,
)
from agent.tool_dispatch_helpers import make_tool_result_message
from gateway.forced_resume_replay import (
    GATEWAY_LIFECYCLE_TOOL_NAMES,
    ReplayExecutionLedger,
    VictimReplayPlan,
    build_replay_assistant_message,
    build_victim_replay_plan,
    execute_victim_replay,
    is_forced_interruption_reason,
    is_lifecycle_replay_request,
    orphan_recovery_row,
    plan_forced_resume_turn,
    trim_incomplete_assistant_text_tail,
)
from gateway.run import (
    _build_gateway_agent_history,
    _is_auto_continue_noise,
    _prepare_resume_pending_message,
    _strip_auto_continue_noise,
)


@pytest.fixture(autouse=True)
def _isolated_replay_reservations():
    """Keep the process-global reservation fence out of neighboring tests.

    Real SessionStore substrates live in per-test SQLite files; only the
    in-process fallback map is shared state, so that is all this clears.
    """
    import gateway.forced_resume_replay as _freplay

    with _freplay._FALLBACK_RESERVATION_LOCK:
        _freplay._FALLBACK_RESERVATIONS.clear()


# ---------------------------------------------------------------------------
# Fakes: recording dispatcher agent + recording transcript store
# ---------------------------------------------------------------------------


class RecordingAgent:
    """Stand-in for the agent's normal tool dispatcher surface.

    Appends one canonical ``make_tool_result_message`` row per call —
    the same row shape the real dispatcher produces — and records every
    execution (name, call id, arguments) for exactly-once assertions.
    """

    def __init__(self, results=None, mark_db_persisted=False):
        self.executions: list = []
        self._results = results or {}
        self._mark_db_persisted = mark_db_persisted

    def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
        for call in assistant_message.tool_calls:
            self.executions.append(
                (call.function.name, call.id, call.function.arguments)
            )
            messages.append(
                make_tool_result_message(
                    call.function.name, self._results.get(call.id, "OK"), call.id
                )
            )
        if self._mark_db_persisted:
            # Mirror the dispatcher's incremental session-DB flush stamping
            # each written row durably-persisted.
            for row in messages:
                row["_db_persisted"] = True
        return "ok"


class RecordingStore:
    """Stand-in for the gateway SessionStore append path.

    Implements the SYNCHRONOUS CHECKED primitive the recovery persists
    through: record + return a real bool (True only when the row landed).
    """

    def __init__(self):
        self.appended: list = []
        self.checked_attempts: list = []

    def append_to_transcript(self, session_id, message):
        self.appended.append((session_id, message))

    def append_to_transcript_checked(self, session_id, message):
        self.checked_attempts.append((session_id, message.get("tool_call_id")))
        self.appended.append((session_id, message))
        return True


def _assistant_batch(calls):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in calls
        ],
    }


def _tool_row(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


_INTERRUPTED_RESULT = '{"exit_code": 130, "output": "[Command interrupted]"}'


def _assert_no_restart_prose(rows):
    """No row may mention the restart/interruption/recovery scaffolding."""
    for row in rows:
        text = str(row.get("content") or "")
        lowered = text.lower()
        assert "gateway restart" not in lowered, row
        assert "was interrupted by" not in lowered, row
        assert "was restored" not in lowered, row
        assert "continue the current task" not in lowered, row


def _assert_strict_pairing(agent_history):
    """Every tool row must pair with its issuing assistant call, uniquely."""
    issued: set = set()
    answered: list = []
    for row in agent_history:
        if not isinstance(row, dict):
            continue
        if row.get("role") == "assistant" and row.get("tool_calls"):
            for call in row["tool_calls"]:
                cid = str(call.get("id") or call.get("call_id") or "")
                assert cid, f"assistant tool_call without id: {call}"
                assert cid not in issued, f"call id issued twice: {cid}"
                issued.add(cid)
        elif row.get("role") == "tool":
            cid = str(row.get("tool_call_id") or "")
            assert cid in issued, f"tool row without issuing call: {cid}"
            answered.append(cid)
    assert len(answered) == len(set(answered)), "duplicate tool_call_id rows"


# ---------------------------------------------------------------------------
# Proof 1: forced victim, one unresolved read-only call
# ---------------------------------------------------------------------------


def test_forced_victim_replays_unresolved_read_only_call_exactly_once():
    history = [
        {"role": "user", "content": "read the config"},
        _assistant_batch([("call_read", "read_file", '{"path": "/etc/app.conf"}')]),
    ]

    plan = build_victim_replay_plan(history)
    assert plan.batch_present is True
    assert [
        (c.name, c.call_id, c.arguments, c.kind) for c in plan.replay_calls
    ] == [("read_file", "call_read", '{"path": "/etc/app.conf"}', "unresolved")]

    agent = RecordingAgent({"call_read": "CONFIG-DATA"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        plan,
        raw_history=history,
        session_store=store,
        session_id="sid-victim",
        effective_task_id="sid-victim",
    )

    # Executes that exact call ONCE with original name, call id, arguments.
    assert agent.executions == [
        ("read_file", "call_read", '{"path": "/etc/app.conf"}')
    ]
    # Persists exactly ONE replacement result through the canonical path.
    assert len(store.appended) == 1
    persisted_sid, persisted_row = store.appended[0]
    assert persisted_sid == "sid-victim"
    assert persisted_row["tool_call_id"] == "call_read"
    assert "CONFIG-DATA" in persisted_row["content"]

    # The repaired history feeds the real gateway history pipeline; the
    # model sees the fresh result at the tail and NO restart prose.
    assert outcome.repaired_history is not None
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    assert agent_history[-1]["role"] == "tool"
    assert agent_history[-1]["tool_call_id"] == "call_read"
    assert "CONFIG-DATA" in agent_history[-1]["content"]
    _assert_no_restart_prose(agent_history)
    _assert_strict_pairing(agent_history)

    # The synthesized auto-resume turn is a pure continuation: NO message,
    # no note, nothing persisted as a user row.
    turn = plan_forced_resume_turn("")
    assert turn.message is None
    assert turn.continue_interrupted_turn is True
    assert turn.persist_user_message is None


# ---------------------------------------------------------------------------
# Proof 2: mixed batch — completed preserved, only unresolved executed
# ---------------------------------------------------------------------------


def test_mixed_batch_preserves_completed_and_replays_only_unresolved():
    history = [
        {"role": "user", "content": "read both files"},
        _assistant_batch(
            [
                ("call_done", "read_file", '{"path": "a.txt"}'),
                ("call_open", "search_files", '{"query": "needle"}'),
            ]
        ),
        _tool_row("call_done", "ALREADY DONE RESULT"),
    ]

    plan = build_victim_replay_plan(history)
    assert plan.completed_call_ids == {"call_done"}
    assert [c.call_id for c in plan.replay_calls] == ["call_open"]

    agent = RecordingAgent({"call_open": "FRESH SEARCH"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )

    assert agent.executions == [("search_files", "call_open", '{"query": "needle"}')]
    assert [sid for sid, _row in store.appended] == ["sid"]

    repaired = outcome.repaired_history
    contents = {
        row["tool_call_id"]: row["content"]
        for row in repaired
        if row.get("role") == "tool"
    }
    # Completed result preserved VERBATIM (never re-executed, never rewritten).
    assert contents["call_done"] == "ALREADY DONE RESULT"
    assert "FRESH SEARCH" in contents["call_open"]

    agent_history, _, _ = _build_gateway_agent_history(repaired)
    _assert_strict_pairing(agent_history)
    _assert_no_restart_prose(agent_history)


# ---------------------------------------------------------------------------
# Proof 3: explicitly interrupted replayable call retried exactly once
# ---------------------------------------------------------------------------


def test_interrupted_replayable_call_retried_once_and_marker_not_final():
    history = [
        {"role": "user", "content": "advise me on the release"},
        _assistant_batch([("call_moa", "moa_ask", '{"prompt": "should we ship?"}')]),
        _tool_row("call_moa", _INTERRUPTED_RESULT),
    ]

    plan = build_victim_replay_plan(history)
    assert [(c.call_id, c.kind) for c in plan.replay_calls] == [
        ("call_moa", "interrupted")
    ]
    assert plan.interrupted_call_ids == {"call_moa"}

    agent = RecordingAgent({"call_moa": "ADVISORY VERDICT"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )

    # Retried EXACTLY once with the original call id and arguments — with no
    # whitelist, moa_ask needs no special case: it is just a victim call.
    assert agent.executions == [("moa_ask", "call_moa", '{"prompt": "should we ship?"}')]

    # The stale interrupted marker is REPLACED in place — exactly one row
    # for the call id, and it is not an interrupted marker.
    repaired = outcome.repaired_history
    final_rows = [
        row
        for row in repaired
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_moa"
    ]
    assert len(final_rows) == 1
    assert is_interrupted_tool_result(_INTERRUPTED_RESULT)  # sanity
    assert not is_interrupted_tool_result(final_rows[0]["content"])
    assert "ADVISORY VERDICT" in final_rows[0]["content"]

    # Append-only disk state: the stale row is still ahead of the appended
    # fresh one.  The real replay-tail stripper must keep the LAST row so
    # the interrupted marker can never win the model-visible final result.
    on_disk = list(history) + [dict(store.appended[0][1])]
    agent_history, _, _ = _build_gateway_agent_history(on_disk)
    moa_rows = [
        row
        for row in agent_history
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_moa"
    ]
    assert len(moa_rows) == 1
    assert not is_interrupted_tool_result(moa_rows[0].get("content", ""))
    assert "ADVISORY VERDICT" in moa_rows[0]["content"]
    _assert_strict_pairing(agent_history)


# ---------------------------------------------------------------------------
# Proof 4: fully completed trailing batch — silent continuation
# ---------------------------------------------------------------------------


def test_fully_completed_trailing_batch_not_reexecuted_and_silent():
    history = [
        {"role": "user", "content": "read it"},
        _assistant_batch(
            [
                ("call_1", "read_file", '{"path": "a"}'),
                ("call_2", "web_search", '{"query": "b"}'),
            ]
        ),
        _tool_row("call_1", "result-1"),
        _tool_row("call_2", "result-2"),
    ]

    plan = build_victim_replay_plan(history)
    assert plan.batch_present is True
    assert plan.has_replay_work is False
    assert plan.completed_call_ids == {"call_1", "call_2"}

    agent = RecordingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    # Nothing re-executed, nothing persisted, no history rewrite.
    assert agent.executions == []
    assert store.appended == []
    assert outcome.repaired_history is None

    # Continuation stays silent: no message at all, never a note.
    turn = plan_forced_resume_turn("")
    assert turn.message is None
    assert turn.continue_interrupted_turn is True


def test_no_tool_calls_at_all_continues_without_inventing_prose():
    """Interruption before any tool call existed (text-only turn): continue
    the original turn; the plan finds no batch and no prose is invented."""
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Working on it..."},
    ]
    plan = build_victim_replay_plan(history)
    assert plan.batch_present is False
    assert plan.has_replay_work is False

    agent = RecordingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == []
    assert store.appended == []
    assert outcome.repaired_history is None

    turn = plan_forced_resume_turn("")
    assert turn.message is None
    assert turn.continue_interrupted_turn is True


# ---------------------------------------------------------------------------
# Proof 5: literal replay — side-effecting victim calls execute once
# ---------------------------------------------------------------------------


def test_side_effecting_non_lifecycle_victim_executes_once():
    """The literal replay contract: an interrupted side-effecting call that
    is NOT the lifecycle request re-runs through the dispatcher exactly
    once instead of being downgraded to an UNKNOWN orphan row."""
    history = [
        {"role": "user", "content": "ship it"},
        _assistant_batch(
            [("call_deploy", "terminal", '{"command": "make deploy"}')]
        ),
        _tool_row("call_deploy", _INTERRUPTED_RESULT),
    ]
    plan = build_victim_replay_plan(history)
    assert [(c.name, c.kind) for c in plan.replay_calls] == [
        ("terminal", "interrupted")
    ]
    assert plan.fail_closed_calls == []

    agent = RecordingAgent({"call_deploy": '{"exit_code": 0, "output": "deployed"}'})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == [
        ("terminal", "call_deploy", '{"command": "make deploy"}')
    ]
    assert outcome.repaired_history is not None

    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    deploy_rows = [
        row for row in agent_history if row.get("tool_call_id") == "call_deploy"
    ]
    assert len(deploy_rows) == 1
    assert "deployed" in deploy_rows[0]["content"]
    assert deploy_rows[0].get("effect_disposition") != "unknown"
    _assert_strict_pairing(agent_history)


def test_unknown_mcp_side_effecting_victim_replays_literally():
    """No name whitelist: an unrecognized MCP tool is an ordinary victim
    call — it replays through the dispatcher rather than failing closed."""
    history = [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_mcp", "mcp__ci__deploy", '{"env": "prod"}')]),
    ]
    plan = build_victim_replay_plan(history)
    assert [c.name for c in plan.replay_calls] == ["mcp__ci__deploy"]
    assert plan.fail_closed_calls == []

    agent = RecordingAgent({"call_mcp": '{"ok": true}'})
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=RecordingStore(), session_id="s"
    )
    assert agent.executions == [("mcp__ci__deploy", "call_mcp", '{"env": "prod"}')]
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    assert agent_history[-1]["tool_call_id"] == "call_mcp"
    _assert_strict_pairing(agent_history)


def test_replay_carries_original_argument_bytes_and_call_ids():
    """Reconciliation and re-execution use the exact original call data —
    argument bytes passed through unchanged, order preserved."""
    args_json = '{"path": "/tmp/a b/c.txt", "offset": 3}'
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch(
            [
                ("call_2", "write_file", args_json),
                ("call_1", "read_file", '{"path": "/etc/hosts"}'),
            ]
        ),
    ]
    plan = build_victim_replay_plan(history)
    agent = RecordingAgent()
    execute_victim_replay(
        agent, plan, raw_history=history, session_store=RecordingStore(), session_id="s"
    )
    # Original order, original argument bytes, original call ids.
    assert agent.executions == [
        ("write_file", "call_2", args_json),
        ("read_file", "call_1", '{"path": "/etc/hosts"}'),
    ]
    # The synthetic assistant message carries the same calls to the
    # dispatcher, keyed by the original ids.
    synthetic = build_replay_assistant_message(plan)
    assert [c.id for c in synthetic.tool_calls] == ["call_2", "call_1"]


# ---------------------------------------------------------------------------
# Proof 6: lifecycle requests never replay (self-restart loop guard)
# ---------------------------------------------------------------------------


def test_lifecycle_classifier_is_narrow_by_name_and_command():
    # The agent-callable restart tool, by name and via a bridge wrapper.
    assert is_lifecycle_replay_request("restart", "{}") is True
    assert GATEWAY_LIFECYCLE_TOOL_NAMES == {"restart"}
    assert (
        is_lifecycle_replay_request(
            "tool_call", '{"name": "restart", "arguments": {}}'
        )
        is True
    )
    # Shell commands targeting the gateway's own lifecycle.
    assert (
        is_lifecycle_replay_request(
            "terminal", '{"command": "hermes gateway restart"}'
        )
        is True
    )
    assert (
        is_lifecycle_replay_request(
            "terminal", '{"command": "sudo systemctl restart hermes-gateway"}'
        )
        is True
    )
    # Ordinary side-effecting victim commands are NOT lifecycle requests.
    assert (
        is_lifecycle_replay_request("terminal", '{"command": "make deploy"}') is False
    )
    assert is_lifecycle_replay_request("terminal", '{"command": "ls -la"}') is False
    assert is_lifecycle_replay_request("write_file", '{"path": "/tmp/a"}') is False
    assert is_lifecycle_replay_request("mcp__ci__deploy", '{"env": "prod"}') is False
    # Bridge wrapping an ordinary tool stays replayable; junk stays honest.
    assert (
        is_lifecycle_replay_request(
            "tool_call", '{"name": "terminal", "arguments": {"command": "ls"}}'
        )
        is False
    )
    assert is_lifecycle_replay_request("tool_call", "not-json") is False


def test_lifecycle_restart_requester_is_never_a_victim():
    """The restart requester's own lifecycle command must not replay — but
    a lifecycle-only batch still CLOSES: one durable UNKNOWN orphan row per
    call, so the reconstructed transcript pairs instead of leaking an
    unanswered batch (user,assistant,assistant) into the next turn."""
    dangling = [
        {"role": "user", "content": "restart the gateway"},
        _assistant_batch(
            [("call_restart", "terminal", '{"command": "hermes gateway restart"}')]
        ),
    ]
    plan = build_victim_replay_plan(dangling)
    assert plan.has_replay_work is False
    assert [c.name for c in plan.fail_closed_calls] == ["terminal"]
    assert plan.lifecycle_call_ids == {"call_restart"}

    agent = RecordingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=dangling, session_store=store, session_id="sid"
    )
    # Never executed…
    assert agent.executions == []
    # …but durably closed exactly once with the ordinary UNKNOWN row.
    assert [(sid, row["tool_call_id"]) for sid, row in store.appended] == [
        ("sid", "call_restart")
    ]
    orphan = store.appended[0][1]
    assert orphan["effect_disposition"] == "unknown"
    assert "UNKNOWN" in orphan["content"]
    assert outcome.repaired_history is not None
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    _assert_no_restart_prose(agent_history)

    # Requester whose restart command DID return before the bounce: the
    # batch is complete — completed calls are never replayed.
    completed = dangling + [
        _tool_row("call_restart", '{"exit_code": 0, "output": "restarting"}')
    ]
    plan2 = build_victim_replay_plan(completed)
    assert plan2.has_replay_work is False
    assert plan2.completed_call_ids == {"call_restart"}


def test_agent_restart_tool_call_is_fail_closed():
    history = [
        {"role": "user", "content": "bounce it"},
        _assistant_batch([("call_r", "restart", "{}")]),
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert [c.name for c in plan.fail_closed_calls] == ["restart"]
    agent = RecordingAgent()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=RecordingStore(), session_id="s"
    )
    assert agent.executions == []


def test_mixed_batch_with_lifecycle_call_pairs_and_closes_it():
    """A batch mixing an ordinary victim call with the lifecycle request:
    the victim replays, the lifecycle call gets the existing UNKNOWN orphan
    treatment — synthesized AND persisted so the batch pairs exactly once
    for strict providers, with no re-execution."""
    history = [
        {"role": "user", "content": "read then restart"},
        _assistant_batch(
            [
                ("call_read", "read_file", '{"path": "a"}'),
                ("call_restart", "restart", "{}"),
            ]
        ),
    ]
    plan = build_victim_replay_plan(history)
    assert [c.call_id for c in plan.replay_calls] == ["call_read"]
    assert [c.call_id for c in plan.fail_closed_calls] == ["call_restart"]

    agent = RecordingAgent({"call_read": "DATA"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == [("read_file", "call_read", '{"path": "a"}')]
    assert outcome.repaired_history is not None

    persisted_ids = [row["tool_call_id"] for _sid, row in store.appended]
    # Fresh result persisted once; the orphan-recovery row persisted once.
    assert persisted_ids.count("call_read") == 1
    assert persisted_ids.count("call_restart") == 1
    orphan_row = next(
        row for _sid, row in store.appended if row["tool_call_id"] == "call_restart"
    )
    assert orphan_row["effect_disposition"] == "unknown"
    assert "UNKNOWN" in orphan_row["content"]
    # The marker key itself never reaches the transcript.
    assert "_orphan_recovery" not in orphan_row

    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    _assert_no_restart_prose(agent_history)


def test_orphan_recovery_row_matches_existing_disposition_semantics():
    row = orphan_recovery_row(type("C", (), {"call_id": "c1", "name": "terminal"})())
    assert row["role"] == "tool"
    assert row["tool_call_id"] == "c1"
    assert row["effect_disposition"] == "unknown"
    assert "UNKNOWN" in row["content"]
    read_only = orphan_recovery_row(
        type("C", (), {"call_id": "c2", "name": "read_file"})()
    )
    assert read_only["effect_disposition"] == "none"


def test_replay_is_idempotent_across_restarts():
    """After a successful transparent replay, re-planning — on the repaired
    history or on the append-only disk state — must find nothing left to
    run, so a second bounce cannot loop the replay."""
    history = [
        {"role": "user", "content": "advise"},
        _assistant_batch([("call_moa", "moa_ask", '{"prompt": "q"}')]),
        _tool_row("call_moa", _INTERRUPTED_RESULT),
    ]
    agent = RecordingAgent({"call_moa": "VERDICT"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
    )
    assert outcome.repaired_history is not None

    # Re-plan on the repaired history and on the raw disk state.
    assert build_victim_replay_plan(outcome.repaired_history).has_replay_work is False
    on_disk = list(history) + [dict(store.appended[0][1])]
    replan = build_victim_replay_plan(on_disk)
    assert replan.has_replay_work is False
    assert replan.completed_call_ids == {"call_moa"}

    # A second execution attempt dispatches nothing new.
    agent2 = RecordingAgent({"call_moa": "SHOULD NOT RUN"})
    execute_victim_replay(
        agent2, build_victim_replay_plan(on_disk),
        raw_history=on_disk, session_store=store, session_id="sid",
    )
    assert agent2.executions == []


def test_dispatcher_persisted_rows_are_not_appended_twice():
    """Rows the dispatcher already flushed (``_db_persisted`` marker) are
    durable via the agent session DB — the gateway append path must skip
    them so exactly-once holds across both canonical paths."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([("call_r", "read_file", '{"path": "a"}')]),
    ]
    agent = RecordingAgent({"call_r": "DATA"}, mark_db_persisted=True)
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
    )
    assert outcome.repaired_history is not None
    assert store.appended == []  # already durable; no second write


def test_duplicate_call_ids_fail_the_whole_batch_closed():
    """Providers occasionally reuse one id across a batch — a replayed
    result cannot pair unambiguously, so nothing replays: the batch
    identity is MALFORMED and blocks below the provider with zero rows."""
    history = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "dup", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "dup", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert len(plan.fail_closed_calls) == 2
    assert plan.identity_malformed is True

    # Malformed identity blocks BEFORE any reservation, dispatch, or row:
    # no execution, no fabricated/duplicate rows, no continuation.
    agent = RecordingAgent({"dup": "MUST NOT RUN"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == []
    assert store.appended == []
    assert outcome.repaired_history is None
    assert outcome.failure and "malformed batch identity" in outcome.failure
    assert outcome.ready_for_continuation is False


def test_dispatcher_failure_fails_closed_without_fabrication():
    """A dispatcher crash keeps the existing fail-closed treatment — no
    fabricated success rows, no repaired history."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([("call_x", "read_file", '{"path": "a"}')]),
    ]

    class _ExplodingAgent:
        def _execute_tool_calls(self, *_args, **_kwargs):
            raise RuntimeError("dispatch blew up")

    outcome = execute_victim_replay(
        _ExplodingAgent(),
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None
    assert outcome.replayed_call_ids == []


# ---------------------------------------------------------------------------
# Proof 7: cooperative parked sessions keep the safe-pause guidance
# ---------------------------------------------------------------------------


def _route_resume_turn(reason, message):
    """Mirror of the run.py ``_is_resume_pending`` branch routing: forced
    reasons take the transparent path, cooperative ones keep the existing
    guidance builder."""
    if is_forced_interruption_reason(reason):
        return plan_forced_resume_turn(message)
    return _prepare_resume_pending_message(reason, message)


def test_recovery_classification_splits_cooperative_from_forced():
    assert is_forced_interruption_reason("cooperative_restart") is False
    for reason in (
        "restart_timeout",
        "shutdown_timeout",
        "restart_interrupted",
        "legacy_marker",
        None,
        "",
    ):
        assert is_forced_interruption_reason(reason) is True, reason


def test_cooperative_restart_keeps_safe_pause_guidance():
    recovery, persist = _route_resume_turn("cooperative_restart", "")
    # The parked-task safe-pause guidance survives verbatim.
    assert "parked itself" in recovery
    assert "CONTINUE the parked task" in recovery
    assert recovery == persist  # synthesized turn persists the guidance row


def test_cooperative_route_does_not_run_victim_replay():
    """A cooperative parked session with a dangling batch must not have its
    calls re-executed: is_forced_interruption_reason gates execute_victim_replay
    in run.py, and the gate is False exactly for cooperative_restart."""
    assert is_forced_interruption_reason("cooperative_restart") is False
    # The gate is the ONLY thing standing between the plan and execution —
    # verify the plan itself WOULD have found work, so the gate is load-bearing.
    history = [
        {"role": "user", "content": "long task"},
        _assistant_batch([("call_open", "read_file", '{"path": "a"}')]),
    ]
    assert build_victim_replay_plan(history).has_replay_work is True


def test_forced_reasons_never_produce_the_cooperative_pause_note():
    """No forced reason may leak the cooperative safe-pause guidance —
    system guidance is exclusive to sessions that accepted the steer."""
    for reason in ("restart_timeout", "shutdown_timeout", "restart_interrupted"):
        turn = _route_resume_turn(reason, "")
        assert turn.message is None, reason
        assert turn.continue_interrupted_turn is True, reason


# ---------------------------------------------------------------------------
# Proof 8: real user input queues clean at a legal boundary
# ---------------------------------------------------------------------------


def test_real_user_message_stays_clean_user_text():
    text = "What happened to the deploy?"
    turn = plan_forced_resume_turn(text)
    # Verbatim both to the model and to the transcript; a normal new user
    # turn AFTER the completed batch, not a continuation.
    assert turn.message == text
    assert turn.persist_user_message == text
    assert turn.continue_interrupted_turn is False
    assert not turn.message.startswith("[System note:")

    # The forced route wraps nothing — unlike the legacy wrapper.
    legacy, _ = _prepare_resume_pending_message("restart_timeout", text)
    assert legacy.startswith("[System note:")
    assert "interrupted" in legacy
    assert legacy.endswith(text)


def test_user_message_queues_after_completed_batch_at_legal_boundary():
    """Pair/persist every tool result first, THEN the real user message
    after the complete assistant→tool batch (never inside it)."""
    history = [
        {"role": "user", "content": "deploy then tell me"},
        _assistant_batch(
            [
                ("call_done", "read_file", '{"path": "a"}'),
                ("call_open", "terminal", '{"command": "make deploy"}'),
            ]
        ),
        _tool_row("call_done", "result-a"),
    ]
    plan = build_victim_replay_plan(history)
    agent = RecordingAgent({"call_open": "DEPLOYED"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert outcome.repaired_history is not None
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)

    # The batch is complete before any user/model continuation…
    batch_index = next(
        i for i, row in enumerate(agent_history) if row.get("tool_calls")
    )
    tool_ids = {
        row["tool_call_id"]
        for row in agent_history[batch_index + 1:]
        if row.get("role") == "tool"
    }
    assert tool_ids == {"call_done", "call_open"}
    _assert_strict_pairing(agent_history)

    # …and the run-side entry point reports the real text verbatim with no
    # recovery wrapper, as an ordinary turn.
    turn = plan_forced_resume_turn("What happened to the deploy?")
    assert turn.message == "What happened to the deploy?"
    assert turn.continue_interrupted_turn is False


# ---------------------------------------------------------------------------
# Proof 9: no synthetic auto-continue noise for forced victims
# ---------------------------------------------------------------------------


def test_auto_continue_noise_stripper_covers_only_live_note_prefixes():
    """The stripper exists for the notes other paths still emit (legacy
    "Your previous turn" wraps and the fresh-tool-tail "A new message"
    wrap).  Forced victims produce no note at all, so nothing of theirs
    needs hiding from replay."""
    legacy = "[System note: Your previous turn was interrupted.]\n\nreal text"
    assert _is_auto_continue_noise(legacy) is True
    assert _strip_auto_continue_noise(legacy) == "real text"
    fallback = "[System note: A new message has arrived.]\n\nreal text"
    assert _is_auto_continue_noise(fallback) is True
    # Real user text is untouched.
    assert _strip_auto_continue_noise("regular question") == "regular question"
    # No synthetic continuation note exists to be classified: a plain
    # "continue" style row a user might actually send stays user text.
    assert _is_auto_continue_noise("[System note: Continue the current task.]") is False


# ---------------------------------------------------------------------------
# Proof 10: transcript pairing stays valid for strict providers
# ---------------------------------------------------------------------------


def _param_history_dangling():
    return [
        {"role": "user", "content": "read"},
        _assistant_batch(
            [
                ("call_a", "read_file", '{"path": "a"}'),
                ("call_b", "read_file", '{"path": "b"}'),
            ]
        ),
        _tool_row("call_a", "done-a"),
    ]


def _param_history_interrupted():
    return [
        {"role": "user", "content": "advise"},
        _assistant_batch(
            [
                ("call_m", "moa_ask", '{"prompt": "q"}'),
                ("call_d", "terminal", '{"command": "ls"}'),
            ]
        ),
        _tool_row("call_m", _INTERRUPTED_RESULT),
        _tool_row("call_d", _INTERRUPTED_RESULT),
    ]


def _param_history_side_effecting_unknown():
    return [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_mcp", "mcp__ci__deploy", '{"env": "prod"}')]),
    ]


_PAIRING_CASES = [
    ("dangling-append", _param_history_dangling),
    ("interrupted-replacement", _param_history_interrupted),
    ("side-effecting-unknown", _param_history_side_effecting_unknown),
]


def test_replay_keeps_pairing_valid_for_strict_providers():
    """Every original call id has exactly one result before any user/model
    continuation — across dangling, interrupted, and side-effecting victim
    batches (no whitelist distinction between them)."""
    for _label, make in _PAIRING_CASES:
        history = make()
        plan = build_victim_replay_plan(history)
        assert plan.has_replay_work is True, _label
        agent = RecordingAgent(
            {"call_a": "A", "call_b": "B", "call_m": "M", "call_d": "D", "call_mcp": "OK"}
        )
        store = RecordingStore()
        outcome = execute_victim_replay(
            agent, plan, raw_history=history, session_store=store, session_id="sid"
        )
        assert outcome.repaired_history is not None, _label
        agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
        _assert_strict_pairing(agent_history)

        issued = {
            str(call.get("id"))
            for row in agent_history
            if row.get("role") == "assistant"
            for call in row.get("tool_calls") or []
        }
        answered = {
            str(row.get("tool_call_id"))
            for row in agent_history
            if row.get("role") == "tool"
        }
        assert issued == answered, _label


# ---------------------------------------------------------------------------
# Replay-tail stripper keep-last dedup (agent/replay_cleanup.py)
# ---------------------------------------------------------------------------


def test_dedupe_tool_results_keep_last_prefers_replacement():
    stale = _tool_row("call_x", _INTERRUPTED_RESULT)
    fresh = _tool_row("call_x", "REPLACEMENT")
    other = _tool_row("call_y", "kept")
    assert dedupe_tool_results_keep_last([stale, fresh, other]) == [fresh, other]
    # No duplicates → same list contents, untouched.
    unique = [other, _tool_row("call_z", "z")]
    assert dedupe_tool_results_keep_last(unique) == unique
    # Rows without an id are always kept.
    no_id = {"role": "tool", "content": "legacy"}
    assert dedupe_tool_results_keep_last([no_id, no_id]) == [no_id, no_id]


def test_strip_interrupted_tool_tails_keeps_replacement_not_marker():
    history = [
        {"role": "user", "content": "advise"},
        _assistant_batch([("call_m", "moa_ask", '{"prompt": "q"}')]),
        _tool_row("call_m", _INTERRUPTED_RESULT),
        _tool_row("call_m", "FRESH REPLACEMENT"),
    ]
    cleaned = strip_interrupted_tool_tails(history)
    moa_rows = [
        row
        for row in cleaned
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_m"
    ]
    # Exactly one row — the fresh replacement; no orphan-recovery rewrite,
    # no interrupted marker, assistant batch preserved.
    assert len(moa_rows) == 1
    assert moa_rows[0]["content"] == "FRESH REPLACEMENT"
    assert "effect_disposition" not in moa_rows[0]
    assert any(
        row.get("role") == "assistant" and row.get("tool_calls") for row in cleaned
    )


def test_strip_interrupted_tool_tails_still_fails_closed_without_replacement():
    """Pre-existing behaviour unchanged: a lone interrupted side-effecting
    result still becomes UNKNOWN orphan recovery."""
    history = [
        {"role": "user", "content": "run"},
        _assistant_batch([("call_t", "terminal", '{"command": "ls"}')]),
        _tool_row("call_t", _INTERRUPTED_RESULT),
    ]
    cleaned = strip_interrupted_tool_tails(history)
    tool_rows = [row for row in cleaned if row.get("role") == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["effect_disposition"] == "unknown"
    assert "UNKNOWN" in tool_rows[0]["content"]


# ---------------------------------------------------------------------------
# Plan shape sanity: empty plan executes nothing
# ---------------------------------------------------------------------------


def test_empty_plan_outcome_is_inert():
    outcome = execute_victim_replay(
        RecordingAgent(),
        VictimReplayPlan(),
        raw_history=[{"role": "user", "content": "hi"}],
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None
    assert outcome.replayed_call_ids == []


# ===========================================================================
# Adversarial review repairs: boundary-level regressions for the seven
# confirmed failures of 7864bef501 (REPAIR.md).
# ===========================================================================


# ---------------------------------------------------------------------------
# Repair 1: original call ids preserved byte-for-byte (no "|" truncation)
# ---------------------------------------------------------------------------


def test_composite_call_id_survives_byte_for_byte():
    """A provider-native composite id (``call_alpha|item_beta``) must stay
    EXACT through classification, dispatch, pairing, persistence, and
    provider history — the pre-fix plan truncated it at the pipe."""
    cid = "call_alpha|item_beta"
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([(cid, "read_file", '{"path": "/etc/hosts"}')]),
    ]
    plan = build_victim_replay_plan(history)
    assert [c.call_id for c in plan.replay_calls] == [cid]

    agent = RecordingAgent({cid: "PIPE-DATA"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    # Dispatch carried the exact id (not the call-id half).
    assert agent.executions == [("read_file", cid, '{"path": "/etc/hosts"}')]
    # Persisted with the exact id…
    assert [row["tool_call_id"] for _sid, row in store.appended] == [cid]
    # …and the model-facing history pairs strictly on the exact id.
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    assert agent_history[-1]["tool_call_id"] == cid
    assert "PIPE-DATA" in agent_history[-1]["content"]


def test_normalized_dispatcher_output_restamped_to_exact_id():
    """The dispatcher's ``make_tool_result_message`` normalizes composite
    ids to the call-id half — the replay must re-stamp the EXACT original
    id onto the fresh row so the batch pairs byte-for-byte."""
    cid = "call_alpha|item_beta"

    class NormalizingAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            for call in assistant_message.tool_calls:
                self.executions.append(
                    (call.function.name, call.id, call.function.arguments)
                )
                # Canonical row construction — truncates composite ids.
                messages.append(
                    make_tool_result_message(call.function.name, "NORM", call.id)
                )
            return "ok"

    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([(cid, "read_file", '{"path": "/etc/hosts"}')]),
    ]
    agent = NormalizingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
    )
    assert agent.executions == [("read_file", cid, '{"path": "/etc/hosts"}')]
    assert outcome.repaired_history is not None
    assert [row["tool_call_id"] for _sid, row in store.appended] == [cid]
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    assert agent_history[-1]["tool_call_id"] == cid


def test_aliasing_composite_ids_fail_closed():
    """Two composite ids sharing a normalized half (``a|1`` / ``a|2``)
    cannot pair unambiguously after dispatcher normalization — a failed
    recovery, never a guess."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch(
            [
                ("call_a|1", "read_file", '{"path": "a"}'),
                ("call_a|2", "read_file", '{"path": "b"}'),
            ]
        ),
    ]

    class NormalizingAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            for call in assistant_message.tool_calls:
                self.executions.append((call.function.name, call.id, call.function.arguments))
                messages.append(
                    make_tool_result_message(call.function.name, "NORM", call.id)
                )
            return "ok"

    agent = NormalizingAgent()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None
    assert outcome.failure and "call_a|" in outcome.failure


# ---------------------------------------------------------------------------
# Repair 2: every stale interrupted row reconciled per exact call id
# ---------------------------------------------------------------------------


def test_duplicate_stale_markers_all_superseded_by_single_fresh_row():
    """Two stale interrupted rows for one call id: BOTH must be superseded
    so exactly one final result remains.  The pre-fix splice replaced only
    the first occurrence, leaving fresh + stale — the model-side cleanup
    then selected UNKNOWN over the fresh result."""
    history = [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_d", "terminal", '{"command": "make deploy"}')]),
        _tool_row("call_d", _INTERRUPTED_RESULT),
        _tool_row("call_d", _INTERRUPTED_RESULT),
    ]
    plan = build_victim_replay_plan(history)
    agent = RecordingAgent({"call_d": '{"exit_code": 0, "output": "deployed"}'})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == [("terminal", "call_d", '{"command": "make deploy"}')]

    repaired = outcome.repaired_history
    final_rows = [
        row for row in repaired if row.get("role") == "tool" and row.get("tool_call_id") == "call_d"
    ]
    assert len(final_rows) == 1
    assert "deployed" in final_rows[0]["content"]
    assert not is_interrupted_tool_result(final_rows[0]["content"])
    _assert_strict_pairing(_build_gateway_agent_history(repaired)[0])

    # The append-only DISK state keeps both stale rows ahead of the fresh
    # one — the replay-tail cleanup must still surface exactly one row and
    # it must be the fresh result, never UNKNOWN.
    on_disk = list(history) + [dict(row) for _sid, row in store.appended]
    agent_history, _, _ = _build_gateway_agent_history(on_disk)
    disk_rows = [
        row
        for row in agent_history
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_d"
    ]
    assert len(disk_rows) == 1
    assert "deployed" in disk_rows[0]["content"]
    assert disk_rows[0].get("effect_disposition") != "unknown"
    _assert_strict_pairing(agent_history)


def test_completed_result_beats_later_stale_marker():
    """A real completed result followed by a stale marker: the call is
    COMPLETE (no replay) and the stale marker must not win model-side —
    the pre-fix keep-last dedupe let the marker turn a SUCCESS into
    UNKNOWN."""
    history = [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_d", "terminal", '{"command": "make deploy"}')]),
        _tool_row("call_d", '{"exit_code": 0, "output": "deployed"}'),
        _tool_row("call_d", _INTERRUPTED_RESULT),
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert plan.completed_call_ids == {"call_d"}

    agent = RecordingAgent()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=RecordingStore(), session_id="sid"
    )
    assert agent.executions == []
    assert outcome.repaired_history is None

    # Model-side cleanup: exactly one row, the real result — not UNKNOWN.
    agent_history, _, _ = _build_gateway_agent_history(history)
    rows = [
        row for row in agent_history
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_d"
    ]
    assert len(rows) == 1
    assert "deployed" in rows[0]["content"]
    assert "effect_disposition" not in rows[0]
    _assert_strict_pairing(agent_history)


def test_dedupe_prefers_real_result_in_both_orderings():
    stale = _tool_row("call_x", _INTERRUPTED_RESULT)
    fresh = _tool_row("call_x", "REAL RESULT")
    other = _tool_row("call_y", "kept")
    # Fresh first, stale later: the real result wins.
    assert dedupe_tool_results_keep_last([fresh, stale, other]) == [fresh, other]
    # Stale first, fresh later (append-only replacement): the fresh wins.
    assert dedupe_tool_results_keep_last([stale, fresh, other]) == [fresh, other]
    # Only markers: keep the LAST marker (newest observation).
    both_stale = [_tool_row("call_x", _INTERRUPTED_RESULT), _tool_row("call_x", _INTERRUPTED_RESULT)]
    deduped = dedupe_tool_results_keep_last(both_stale)
    assert len(deduped) == 1
    assert deduped[0] is both_stale[1]


# ---------------------------------------------------------------------------
# Repair 3: partial dispatcher output is a failed recovery
# ---------------------------------------------------------------------------


def test_partial_dispatcher_output_fails_recovery():
    """A two-call batch whose dispatcher returns only one result is a
    FAILED recovery — never a repaired history with the second call
    silently absent, and never a model continuation."""
    history = [
        {"role": "user", "content": "read both"},
        _assistant_batch(
            [
                ("call_a", "read_file", '{"path": "a"}'),
                ("call_b", "read_file", '{"path": "b"}'),
            ]
        ),
    ]

    class PartialAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            # Executes BOTH calls (side effects applied) but only one
            # result row comes back — the partial-output scenario.
            for call in assistant_message.tool_calls:
                self.executions.append(
                    (call.function.name, call.id, call.function.arguments)
                )
            first = assistant_message.tool_calls[0]
            messages.append(
                make_tool_result_message(first.function.name, "ONLY-A", first.id)
            )
            return "ok"

    agent = PartialAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
    )
    assert outcome.repaired_history is None
    assert outcome.failure and "call_b" in outcome.failure
    # No repaired history → no repaired persistence either.
    assert store.appended == []


def test_partial_output_leaves_durable_rows_for_later_bounded_recovery():
    """When partial output includes a row the dispatcher already flushed
    durably, that row survives for a later bounded recovery: the answered
    call is preserved verbatim (never re-executed), and the still-unresolved
    call — whose execution the ledger recorded — fails closed UNKNOWN rather
    than re-running its side effect."""
    history = [
        {"role": "user", "content": "read then deploy"},
        _assistant_batch(
            [
                ("call_a", "read_file", '{"path": "a"}'),
                ("call_b", "terminal", '{"command": "make deploy"}'),
            ]
        ),
    ]

    class PartialFlushedAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            for call in assistant_message.tool_calls:
                self.executions.append(
                    (call.function.name, call.id, call.function.arguments)
                )
            first = assistant_message.tool_calls[0]
            row = make_tool_result_message(first.function.name, "DURABLE-A", first.id)
            row["_db_persisted"] = True  # flushed through the session DB
            messages.append(row)
            return "ok"

    agent = PartialFlushedAgent()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None  # partial output failed recovery

    # The bounded second recovery over the durable state: call_a answered on
    # disk, call_b's execution recorded but its result never durable.
    on_disk = list(history) + [
        {"role": "tool", "tool_call_id": "call_a", "content": "DURABLE-A"}
    ]
    agent2 = RecordingAgent({"call_b": "SHOULD NOT RUN AGAIN"})
    outcome2 = execute_victim_replay(
        agent2,
        build_victim_replay_plan(on_disk),
        raw_history=on_disk,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert agent.executions == [
        ("read_file", "call_a", '{"path": "a"}'),
        ("terminal", "call_b", '{"command": "make deploy"}'),
    ]
    # Exactly once across recoveries: the ledger blocks call_b's re-run.
    assert agent2.executions == []
    # A reservation conflict is another worker owning the unresolved batch:
    # the loser stands down BLOCKED — no repaired history (so no provider
    # continuation), no UNKNOWN row fabricated over the unresolved side
    # effect, recovery left pending for the bounded owner/later retry.
    assert outcome2.repaired_history is None
    assert outcome2.failure and "reservation conflict" in outcome2.failure
    assert outcome2.ready_for_continuation is False


def test_unexpected_dispatcher_row_fails_recovery():
    """A result row for an id outside the replay set is unexpected output
    — failed recovery, never spliced in."""

    class RogueAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            for call in assistant_message.tool_calls:
                self.executions.append(
                    (call.function.name, call.id, call.function.arguments)
                )
                messages.append(
                    make_tool_result_message(call.function.name, "OK", call.id)
                )
            messages.append(
                make_tool_result_message("read_file", "ROGUE", "call_not_in_batch")
            )
            return "ok"

    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([("call_a", "read_file", '{"path": "a"}')]),
    ]
    outcome = execute_victim_replay(
        RogueAgent(),
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None
    assert outcome.failure and "call_not_in_batch" in outcome.failure


def test_duplicate_dispatcher_rows_fail_recovery():
    """Two result rows for one call and none for the other: the fresh-id
    multiset must match the replay-call set exactly."""

    class DupAgent(RecordingAgent):
        def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
            for call in assistant_message.tool_calls:
                self.executions.append(
                    (call.function.name, call.id, call.function.arguments)
                )
            first = assistant_message.tool_calls[0]
            messages.append(make_tool_result_message(first.function.name, "A1", first.id))
            messages.append(make_tool_result_message(first.function.name, "A2", first.id))
            return "ok"

    history = [
        {"role": "user", "content": "read"},
        _assistant_batch(
            [
                ("call_a", "read_file", '{"path": "a"}'),
                ("call_b", "read_file", '{"path": "b"}'),
            ]
        ),
    ]
    outcome = execute_victim_replay(
        DupAgent(),
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert outcome.repaired_history is None
    # Two rows claim call_a — ambiguous pairing is the named failure.
    assert outcome.failure and "call_a" in outcome.failure


# ---------------------------------------------------------------------------
# Repair 4: persistence failure propagates; executions are idempotent
# ---------------------------------------------------------------------------


class FlakyStore:
    """Appends fail for chosen call ids; everything else records.

    Failures surface through the CHECKED primitive the way a real store
    failure does: the exception escapes the checked append and the
    recovery sees "not durable" rather than inferring success.
    """

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.appended = []
        self.append_attempts = []

    def append_to_transcript(self, session_id, message, skip_db=False):
        cid = message.get("tool_call_id")
        self.append_attempts.append(cid)
        if cid in self.fail_ids:
            raise RuntimeError(f"simulated append failure for {cid}")
        self.appended.append((session_id, message))

    def append_to_transcript_checked(self, session_id, message):
        self.append_to_transcript(session_id, message)
        return True


def _interrupted_terminal_history_call_x():
    return [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_x", "terminal", '{"command": "make deploy"}')]),
        _tool_row("call_x", _INTERRUPTED_RESULT),
    ]


def test_append_failure_fails_recovery_and_retry_never_reexecutes():
    """Forced append failure → failed recovery (no repaired outcome, no
    provider continuation); a retry over the UNCHANGED transcript must not
    re-execute the terminal call — execution count stays one."""
    history = _interrupted_terminal_history_call_x()
    store = FlakyStore(fail_ids={"call_x"})
    ledger = ReplayExecutionLedger(store, "sid")
    agent = RecordingAgent({"call_x": '{"exit_code": 0, "output": "deployed"}'})

    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
        ledger=ledger,
    )
    # The side effect ran once, but nothing may claim repair or continue.
    assert agent.executions == [
        ("terminal", "call_x", '{"command": "make deploy"}')
    ]
    assert outcome.repaired_history is None
    assert outcome.failure and "call_x" in outcome.failure

    # Retry over unchanged durable history (append STILL failing).
    agent2 = RecordingAgent({"call_x": "SHOULD NOT RUN"})
    store2 = FlakyStore(fail_ids={"call_x"})
    outcome2 = execute_victim_replay(
        agent2,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store2,
        session_id="sid",
        ledger=ReplayExecutionLedger(store2, "sid"),
    )
    assert agent2.executions == []  # exactly-once execution across retries
    assert outcome2.repaired_history is None
    assert outcome2.failure

    # Third attempt: append works again, but the first attempt's execution
    # reservation is still held (its result never became durable).  A
    # reservation conflict means another worker owns the unresolved batch:
    # this loser stands down without execution, without an UNKNOWN row,
    # and without a repaired outcome — recovery stays pending.
    store3 = FlakyStore()
    outcome3 = execute_victim_replay(
        RecordingAgent({"call_x": "STILL MUST NOT RUN"}),
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store3,
        session_id="sid",
        ledger=ReplayExecutionLedger(store3, "sid"),
    )
    assert store3.append_attempts == []  # no fabricated rows at all
    assert store3.appended == []
    assert outcome3.repaired_history is None
    assert outcome3.failure and "reservation conflict" in outcome3.failure
    assert outcome3.ready_for_continuation is False


def test_reservation_released_after_durable_persist():
    """The reservation exists only until the result is durable — a provider
    REUSING the same call id with the SAME arguments in a later batch (the
    exact identity the key fences) still replays."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([("call_reuse", "read_file", '{"path": "a"}')]),
    ]
    store = RecordingStore()
    agent = RecordingAgent({"call_reuse": "ONE"})
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
    )
    assert outcome.repaired_history is not None

    # Same id AND same arguments in a NEW batch — the identical reservation
    # identity: an ordinary victim call that must replay again.
    history2 = [
        {"role": "user", "content": "read again"},
        _assistant_batch([("call_reuse", "read_file", '{"path": "a"}')]),
    ]
    agent2 = RecordingAgent({"call_reuse": "TWO"})
    outcome2 = execute_victim_replay(
        agent2,
        build_victim_replay_plan(history2),
        raw_history=history2,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert agent2.executions == [("read_file", "call_reuse", '{"path": "a"}')]
    assert outcome2.repaired_history is not None


def test_reservation_failure_fails_closed_without_execution():
    """When the reservation itself cannot be made (store error), the call
    must not execute — idempotence would be unprovable, and an unprovable
    batch identity is a BLOCKED outcome: no execution, no fabricated row,
    no continuation."""
    class UnreservableLedger(ReplayExecutionLedger):
        def reserve_execution(self, call):
            return False, "simulated reservation store failure"

    history = [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_x", "terminal", '{"command": "make deploy"}')]),
    ]
    agent = RecordingAgent({"call_x": "MUST NOT RUN"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=store,
        session_id="sid",
        ledger=UnreservableLedger(RecordingStore(), "sid"),
    )
    assert agent.executions == []
    assert store.appended == []  # no UNKNOWN row fabricated over the conflict
    assert outcome.repaired_history is None
    assert outcome.failure and "reservation conflict" in outcome.failure
    assert outcome.ready_for_continuation is False


def test_no_session_store_fails_rather_than_faking_durability():
    """Without a durable store the replacement cannot be committed — the
    recovery must fail instead of pretending the transcript was repaired,
    and the call must never execute with nowhere to put its result."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([("call_r", "read_file", '{"path": "a"}')]),
    ]
    agent = RecordingAgent({"call_r": "DATA"})
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=None,
        session_id="sid",
    )
    assert agent.executions == []  # nothing runs when nothing can commit
    assert outcome.repaired_history is None
    assert outcome.failure


# ---------------------------------------------------------------------------
# Repair 5: lifecycle classification fails CLOSED
# ---------------------------------------------------------------------------


def test_lifecycle_classifier_failure_fails_closed(monkeypatch):
    """When the canonical classifier is unavailable or raises, a
    command-bearing call is treated as a lifecycle request — a
    mis-classified ordinary command costs one UNKNOWN row, a
    mis-classified lifecycle command costs another gateway bounce."""

    def _raise(command):
        raise RuntimeError("classifier exploded")

    import cron.lifecycle_guard as lifecycle_guard

    monkeypatch.setattr(lifecycle_guard, "contains_gateway_lifecycle_command", _raise)

    # Direct command-bearing call.
    assert (
        is_lifecycle_replay_request(
            "terminal", '{"command": "hermes gateway restart"}'
        )
        is True
    )
    # Bridge-wrapped command-bearing call.
    assert (
        is_lifecycle_replay_request(
            "tool_call",
            '{"name": "terminal", "arguments": {"command": "systemctl restart hermes-gateway"}}',
        )
        is True
    )
    # Calls without a command have nothing to classify and stay replayable.
    assert is_lifecycle_replay_request("write_file", '{"path": "/tmp/a"}') is False
    assert is_lifecycle_replay_request("mcp__ci__deploy", '{"env": "prod"}') is False

    # And the plan routes such a call to fail-closed, never to the
    # dispatcher — no self-restart loop through a broken classifier.
    history = [
        {"role": "user", "content": "bounce it"},
        _assistant_batch(
            [("call_r", "terminal", '{"command": "hermes gateway restart"}')]
        ),
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert [c.call_id for c in plan.fail_closed_calls] == ["call_r"]
    assert plan.lifecycle_call_ids == {"call_r"}

    agent = RecordingAgent()
    outcome = execute_victim_replay(
        agent,
        build_victim_replay_plan(history),
        raw_history=history,
        session_store=RecordingStore(),
        session_id="sid",
    )
    assert agent.executions == []


def test_lifecycle_import_failure_fails_closed(monkeypatch):
    """A broken/unimportable lifecycle guard must also fail closed for
    command-bearing calls (``None`` in sys.modules halts the import)."""
    import sys

    monkeypatch.setitem(sys.modules, "cron.lifecycle_guard", None)
    assert (
        is_lifecycle_replay_request(
            "terminal", '{"command": "sudo systemctl restart hermes-gateway"}'
        )
        is True
    )


# ---------------------------------------------------------------------------
# Repair 7 helper: text-only boundary trim
# ---------------------------------------------------------------------------


def test_trim_incomplete_assistant_text_tail_variants():
    user = {"role": "user", "content": "hello"}
    partial = {"role": "assistant", "content": "Working on it…"}
    batch = _assistant_batch([("c1", "read_file", "{}")])
    tool = _tool_row("c1", "data")

    # Single incomplete text tail → dropped, boundary at the user row.
    assert trim_incomplete_assistant_text_tail([user, partial]) == (
        [user],
        [partial],
    )
    # A run of trailing text rows → all dropped.
    assert trim_incomplete_assistant_text_tail(
        [user, dict(partial), dict(partial, content="more")]
    ) == ([user], [dict(partial), dict(partial, content="more")])
    # assistant(tool_calls) tail belongs to the replay path — untouched.
    trimmed, dropped = trim_incomplete_assistant_text_tail([user, batch])
    assert trimmed == [user, batch] and dropped == []
    # Tail after a completed tool batch → the tool row is the boundary.
    assert trim_incomplete_assistant_text_tail([user, batch, tool, partial]) == (
        [user, batch, tool],
        [partial],
    )
    # Already-legal tails are unchanged.
    assert trim_incomplete_assistant_text_tail([user]) == ([user], [])
    assert trim_incomplete_assistant_text_tail([user, batch, tool]) == (
        [user, batch, tool],
        [],
    )
    # Non-dict junk at the tail stops the trim.
    junk = ["not-a-row"]
    assert trim_incomplete_assistant_text_tail([user, partial, junk]) == (
        [user, partial, junk],
        [],
    )


# ===========================================================================
# REPAIR 2, finding 8: malformed ids are never silently rewritten
# ===========================================================================


def test_padded_call_id_preserved_byte_for_byte():
    """A non-empty id with leading/trailing whitespace is VALID provider
    data — it must survive classification, dispatch, persistence, and
    model history byte-for-byte, never trimmed into a rewrite."""
    cid = " call_read "
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([(cid, "read_file", '{"path": "/etc/hosts"}')]),
    ]
    plan = build_victim_replay_plan(history)
    # Whitespace-only ids fail closed; non-empty padded ones replay as-is.
    assert [c.call_id for c in plan.replay_calls] == [cid]

    agent = RecordingAgent({cid: "PADDED-DATA"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == [("read_file", cid, '{"path": "/etc/hosts"}')]
    # Persisted with the exact padded bytes…
    assert [row["tool_call_id"] for _sid, row in store.appended] == [cid]
    # …and the model-facing history pairs on them.
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    assert agent_history[-1]["tool_call_id"] == cid
    assert "PADDED-DATA" in agent_history[-1]["content"]


def test_padded_composite_id_restamped_verbatim_from_canonical_echo():
    """The dispatcher's canonical echo of ``" call_alpha|item_beta "`` is
    ``call_alpha`` (strip + pipe-normalize through make_tool_result_message)
    — the replay must re-stamp the EXACT padded composite bytes back onto
    the row instead of persisting the trimmed alias or failing."""
    cid = " call_alpha|item_beta "
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch([(cid, "read_file", '{"path": "/etc/hosts"}')]),
    ]
    plan = build_victim_replay_plan(history)
    assert [c.call_id for c in plan.replay_calls] == [cid]

    # RecordingAgent builds rows through the REAL make_tool_result_message,
    # so its rows carry the canonical alias exactly as the live dispatcher
    # would emit them.
    agent = RecordingAgent({cid: "PIPE-DATA"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == [("read_file", cid, '{"path": "/etc/hosts"}')]
    assert [row["tool_call_id"] for _sid, row in store.appended] == [cid]
    agent_history, _, _ = _build_gateway_agent_history(outcome.repaired_history)
    _assert_strict_pairing(agent_history)
    assert agent_history[-1]["tool_call_id"] == cid


def test_whitespace_only_and_missing_ids_fail_closed():
    """Whitespace-only and missing ids have no unambiguous pairing key —
    the batch identity is MALFORMED and blocks below the provider: zero
    dispatches, zero persisted rows, no repaired outcome, recovery stays
pending, instead of being rewritten into something replayable."""
    missing_id_call = {
        "function": {"name": "read_file", "arguments": '{"path": "a"}'}
    }
    history = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "   ", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "", "function": {"name": "read_file", "arguments": "{}"}},
                dict(missing_id_call),
            ],
        },
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert [c.call_id for c in plan.fail_closed_calls] == ["   ", "", ""]
    assert plan.identity_malformed is True

    agent = RecordingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    # Never executed, never dispatched…
    assert agent.executions == []
    # …and no fabricated or duplicate rows anywhere — a tool row under a
    # whitespace-only or empty id pairs with nothing, so closure would be
    # noise, not repair.  The batch stays open and recovery stays pending.
    assert store.appended == []
    assert outcome.repaired_history is None
    assert outcome.failure and "malformed batch identity" in outcome.failure
    assert outcome.ready_for_continuation is False


def test_variant_answered_completed_call_never_replays():
    """A durable result row stored under the NORMALIZED alias (the live
    dispatcher's canonical echo) DOES answer a composite victim call —
    replaying it would re-run a completed side effect.  The variant-aware
    reconciliation must mark it completed, exactly like an exact-id row."""
    history = [
        {"role": "user", "content": "deploy"},
        _assistant_batch([("call_alpha|item_beta", "terminal", '{"command": "make deploy"}')]),
        # The interrupted session's own dispatcher flushed this row under
        # the normalized alias before the bounce.
        _tool_row("call_alpha", '{"exit_code": 0, "output": "deployed"}'),
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert plan.completed_call_ids == {"call_alpha|item_beta"}

    agent = RecordingAgent({"call_alpha|item_beta": "MUST NOT RUN"})
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == []
    assert store.appended == []
    assert outcome.repaired_history is None

    # And the completed (aliased) row is the model-visible answer — not an
    # interrupted marker, not UNKNOWN.
    agent_history, _, _ = _build_gateway_agent_history(history)
    rows = [
        row
        for row in agent_history
        if row.get("role") == "tool" and row.get("tool_call_id") == "call_alpha"
    ]
    assert len(rows) == 1
    assert "deployed" in rows[0]["content"]


def test_ambiguous_variant_row_fails_whole_batch_closed():
    """A result row aliasing MORE than one call of the batch (here: the
    shared normalized half of two composite ids) cannot say which call it
    answers — the whole batch fails closed rather than guessing."""
    history = [
        {"role": "user", "content": "read"},
        _assistant_batch(
            [
                ("call_a|1", "read_file", '{"path": "a"}'),
                ("call_a|2", "read_file", '{"path": "b"}'),
            ]
        ),
        _tool_row("call_a", "AMBIGUOUS HALF ROW"),
    ]
    plan = build_victim_replay_plan(history)
    assert plan.has_replay_work is False
    assert len(plan.fail_closed_calls) == 2

    agent = RecordingAgent()
    store = RecordingStore()
    outcome = execute_victim_replay(
        agent, plan, raw_history=history, session_store=store, session_id="sid"
    )
    assert agent.executions == []
    # Each call closed once with the ordinary orphan treatment…
    assert sorted(row["tool_call_id"] for _sid, row in store.appended) == [
        "call_a|1",
        "call_a|2",
    ]
    # …the ambiguous row itself survives verbatim (never rewritten)…
    assert outcome.repaired_history is not None
    assert any(
        row.get("tool_call_id") == "call_a"
        and row.get("content") == "AMBIGUOUS HALF ROW"
        for row in outcome.repaired_history
    )
