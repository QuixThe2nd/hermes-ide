"""One-shot ``chat -q`` runs report their outcome in the exit code, like ``-Q`` always did.

The non-quiet one-shot path used to fall through to an implicit rc=0 for every
outcome, so scripts could not tell a failed run from a good one and the Kanban
dispatcher (which spawns ``chat -q`` workers) booked a provider quota wall as a
protocol violation (#111770, #101800; salvage of #110917 / #97623).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE, KANBAN_TERMINAL_PROVIDER_EXIT_CODE


@pytest.fixture(autouse=True)
def _no_inherited_kanban_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)


def _run_non_quiet(monkeypatch, turn_result):
    """Drive ``_run_single_query_mode`` down the non-quiet tail; return the exit code (None = fell through)."""
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda imgs: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: None)
    stub = SimpleNamespace(
        _single_query_mode=False,
        _claim_active_session=lambda *a, **k: True,
        console=SimpleNamespace(print=lambda *a, **k: None),
        _show_security_advisories=lambda: None,
        chat=lambda *a, **k: "response",
        _print_exit_summary=lambda **k: None,
        _last_turn_result=turn_result,
    )
    try:
        cli._run_single_query_mode(stub, "do the thing", None, False, True)
    except SystemExit as exc:
        return exc.code
    return None


@pytest.mark.parametrize(
    "reason", ["rate_limit", "upstream_rate_limit", "billing", "overloaded", "server_error", "timeout"]
)
def test_dispatcher_spawned_worker_signals_a_provider_outage_not_a_protocol_violation(monkeypatch, reason):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    code = _run_non_quiet(monkeypatch, {"failed": True, "failure_reason": reason})
    assert code == KANBAN_RATE_LIMIT_EXIT_CODE


@pytest.mark.parametrize(
    "reason", ["auth", "auth_permanent", "model_not_found", "ssl_cert_verification", "upstream_blocked"]
)
def test_dispatcher_spawned_worker_signals_a_terminal_provider_error(monkeypatch, reason):
    """A revoked credential / missing model / WAF User-Agent block cannot be retried into working:
    the worker says so with EX_CONFIG so the dispatcher parks the card after one spawn. A person's
    run keeps 1."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    assert _run_non_quiet(monkeypatch, {"failed": True, "failure_reason": reason}) == KANBAN_TERMINAL_PROVIDER_EXIT_CODE
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert _run_non_quiet(monkeypatch, {"failed": True, "failure_reason": reason}) == 1


def test_dispatcher_spawned_worker_keeps_a_plain_failure_at_one(monkeypatch):
    """Control: a task-level failure (or an unknown reason) is neither transient nor terminal —
    the worker exits 1 and the dispatcher counts it against ``kanban.failure_limit`` as before."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    assert _run_non_quiet(monkeypatch, {"failed": True, "failure_reason": "some_unknown_reason"}) == 1
    assert _run_non_quiet(monkeypatch, {"failed": True}) == 1


@pytest.mark.parametrize(
    ("turn_result", "expected"),
    [
        ({"final_response": "done", "completed": True}, 0),
        ({"failed": True, "failure_reason": "rate_limit"}, 1),  # a person's run: a wall is just a failure
        ({"final_response": "half", "completed": False, "partial": True}, 1),
        ({"completed": False, "interrupted": True}, 130),
        (None, 1),  # credentials / agent init failed before any turn ran
    ],
)
def test_a_plain_one_shot_run_reports_its_outcome(monkeypatch, turn_result, expected):
    assert _run_non_quiet(monkeypatch, turn_result) == expected


def _run_quiet_oneshot(monkeypatch, capsys, *, run_conversation=None, credentials=True, init_agent=True):
    """Drive ``_run_single_query_mode(quiet=True, oneshot=True)`` end-to-end through the real
    ``_run_quiet_single_query`` / ``exit_single_query``; return ``(exit_code, stdout, stderr, finalized, turn_ran)``.

    ``run_conversation=None`` means the turn never started (credentials / agent init rejected).
    """
    finalized = []
    interrupted_emitted = []
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_collect_query_images", lambda q, i: (q, []))
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda imgs: [])
    monkeypatch.setattr(cli, "_route_single_query_images", lambda *a, **k: a[2])
    monkeypatch.setattr(cli, "_configure_quiet_agent", lambda agent: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda c: finalized.append(True))
    monkeypatch.setattr(cli, "_emit_interrupted_session_end", lambda c, **k: interrupted_emitted.append(k.get("reason")))
    import hermes_cli.quiet_single_query as _qsq
    monkeypatch.setattr(_qsq, "quiet_notify_linger_seconds", lambda: 0.0)
    monkeypatch.setattr(_qsq, "continue_quiet_notify_completions", lambda *a, **k: None)
    turn_ran = []

    def _run(user_message, conversation_history, **kwargs):
        turn_ran.append(user_message)
        return run_conversation(user_message, conversation_history, **kwargs)

    stub = SimpleNamespace(
        _single_query_mode=False,
        session_id="s_exit1",
        conversation_history=[],
        agent=SimpleNamespace(run_conversation=_run) if run_conversation is not None else None,
        _claim_active_session=lambda *a, **k: True,
        _ensure_runtime_credentials=lambda: credentials,
        _active_agent_route_signature="sig",
        _resolve_turn_agent_config=lambda q: {"signature": "sig", "model": None, "runtime": None},
        _init_agent=lambda **k: init_agent,
    )
    code = None
    try:
        cli._run_single_query_mode(stub, "do the thing", None, True, True)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err, finalized, turn_ran


@pytest.mark.parametrize(
    ("case", "run_conversation", "credentials", "init_agent", "expected_code", "stdout_has", "stderr_has"),
    [
        pytest.param(
            "success", lambda *a, **k: {"final_response": "all done", "completed": True}, True, True,
            0, "all done", "session_id", id="quiet-success-rc0",
        ),
        pytest.param(
            "failed-no-response", lambda *a, **k: {"failed": True, "error": "provider 4xx"}, True, True,
            1, None, "Error: provider 4xx", id="quiet-failed-rc1-error-stderr",
        ),
        pytest.param(
            "partial", lambda *a, **k: {"final_response": "half an answer", "completed": False, "partial": True}, True, True,
            1, "half an answer", "session_id", id="quiet-partial-rc1-partial-stdout",
        ),
        pytest.param(
            "returned-interruption", lambda *a, **k: {"completed": False, "interrupted": True}, True, True,
            130, None, "session_id", id="quiet-returned-interrupt-rc130",
        ),
        pytest.param(
            "keyboard-interrupt", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()), True, True,
            130, None, "session_id", id="quiet-keyboard-interrupt-rc130",
        ),
        pytest.param(
            "credentials-rejected", None, False, True,
            1, None, None, id="quiet-credentials-rejected-rc1-no-turn",
        ),
        pytest.param(
            "agent-init-rejected", None, True, False,
            1, None, None, id="quiet-agent-init-rejected-rc1-no-turn",
        ),
    ],
)
def test_a_quiet_one_shot_run_exits_through_the_documented_codes(
    monkeypatch, capsys, case, run_conversation, credentials, init_agent, expected_code, stdout_has, stderr_has
):
    """The quiet -Q contract: exit_single_query is the ONLY exit, with the documented rc, and
    stdout carries just the final answer while errors/session_id go to stderr."""
    code, out, err, finalized, turn_ran = _run_quiet_oneshot(
        monkeypatch, capsys, run_conversation=run_conversation, credentials=credentials, init_agent=init_agent,
    )
    assert code == expected_code
    assert finalized == [True]
    if stdout_has is None:
        assert out.strip() == ""
    else:
        assert stdout_has in out
    if stderr_has is None:
        assert err.strip() == ""
    else:
        assert stderr_has in err
    # A turn only runs when credentials AND agent init both succeeded; the rejected
    # cases must fail before the agent is ever invoked.
    assert bool(turn_ran) == (credentials and init_agent)
