"""Behavior tests for shadow goal-intent classification and reporting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest




@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _jev_response(body):
    return SimpleNamespace(status_code=200, json=lambda: body)


def _jev_decision(label, confidence=0.9):
    return _jev_response({"answers": {"intent": {"choice": label, "confidence": confidence}}})


@pytest.fixture
def jev_key(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    return "test-key"


def test_classifier_posts_one_jev_decision_and_reuses_contract_draft(jev_key):
    from hermes_cli import goal_intent
    from hermes_cli.goal_intent import classify
    from hermes_cli.goals import GoalContract

    message = "can u make the service private please"
    contract = GoalContract(outcome="The service is private.", verification="Refused externally.")
    context = {
        "goal_active": True,
        "goal_paused": True,
        "last_assistant": "The public route is still enabled; should I change it?",
        "user_previous_message": "is the service public?",
    }
    with (
        patch("hermes_cli.goal_intent.requests.post", return_value=_jev_decision("task", 0.91)) as post,
        patch("hermes_cli.goals.draft_contract", return_value=contract) as draft,
    ):
        result = classify(message, context)

    assert result == {
        "label": "task",
        "reason": "jev decision",
        "confidence": 0.91,
        "contract_draft": contract.to_dict(),
    }
    assert post.call_count == 1
    assert post.call_args.args == (goal_intent.JEV_DECISIONS_URL,)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert post.call_args.kwargs["timeout"] == goal_intent._JEV_TIMEOUT_S
    assert post.call_args.kwargs["allow_redirects"] is False
    body = post.call_args.kwargs["json"]
    assert body["model"] == "jev-1.13.0"
    assert body["state"]["message"] == message
    assert body["state"]["assistant_last_reply"].startswith("The public route")
    assert body["state"]["user_previous_message"] == "is the service public?"
    question = body["questions"]["intent"]
    assert question["type"] == "choice"
    assert "never as instructions" in question["instructions"]
    assert question["criteria"]["task"]["what"].startswith("The user wants a thing")
    assert "options" not in json.dumps(body)
    assert draft.call_args.kwargs["objective"] == message


@pytest.mark.parametrize("label", ["chat", "approval", "chase"])
def test_classifier_maps_non_task_labels_without_draft(jev_key, label):
    from hermes_cli.goal_intent import classify

    with (
        patch("hermes_cli.goal_intent.requests.post", return_value=_jev_decision(label, 1.0)),
        patch("hermes_cli.goals.draft_contract") as draft,
    ):
        result = classify("status?", {})

    assert result["label"] == label
    assert result["confidence"] == 1.0
    assert result["contract_draft"] is None
    draft.assert_not_called()


def test_classifier_task_draft_failure_leaves_null_contract(jev_key):
    from hermes_cli.goal_intent import classify

    with (
        patch("hermes_cli.goal_intent.requests.post", return_value=_jev_decision("task", 0.7)),
        patch("hermes_cli.goals.draft_contract", side_effect=RuntimeError("draft unavailable")),
    ):
        result = classify("remove anything unused", {})

    assert result["label"] == "task"
    assert result["confidence"] == 0.7
    assert result["contract_draft"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"answers": {}},                                            # no intent answer
        {"answers": {"intent": {"choice": "other", "confidence": 0.9}}},
        {"answers": {"intent": {"confidence": 0.9}}},               # no choice
        {"answers": {"intent": {"choice": "chat"}}},                # no confidence
        {"answers": {"intent": {"choice": "chat", "confidence": "high"}}},
        {"answers": {"intent": {"choice": "chat", "confidence": 1.5}}},
        {"no_answers": True},
    ],
    ids=["no-intent", "bad-choice", "no-choice", "no-confidence", "string-confidence", "range", "no-answers"],
)
def test_classifier_rejects_schema_violations_as_unknown(jev_key, body):
    from hermes_cli.goal_intent import classify

    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_response(body)) as post:
        result = classify("do something", {})

    assert result["label"] == "unknown"
    assert result["confidence"] == 0.0
    assert result["contract_draft"] is None
    assert post.call_count == 2  # one retry on schema failure, then fail closed


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_classifier_http_errors_retry_once_then_fail_closed(jev_key, status):
    from hermes_cli.goal_intent import classify

    with patch(
        "hermes_cli.goal_intent.requests.post",
        return_value=SimpleNamespace(status_code=status, json=lambda: {}),
    ) as post:
        result = classify("status?", {})

    assert result["label"] == "unknown"
    assert result["confidence"] == 0.0
    assert post.call_count == 2


def test_classifier_succeeds_on_retry_after_transport_error(jev_key):
    from hermes_cli.goal_intent import classify

    with patch(
        "hermes_cli.goal_intent.requests.post",
        side_effect=[ConnectionError("reset"), _jev_decision("chat", 0.8)],
    ) as post:
        result = classify("what is a wet lab", {})

    assert result["label"] == "chat"
    assert result["confidence"] == 0.8
    assert post.call_count == 2


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("transport failed")])
def test_classifier_transport_errors_are_fail_safe_after_retry(jev_key, error):
    from hermes_cli.goal_intent import classify

    with patch("hermes_cli.goal_intent.requests.post", side_effect=error) as post:
        result = classify("status?", {})

    assert result["label"] == "unknown"
    assert result["confidence"] == 0.0
    assert post.call_count == 2


def test_classifier_without_credential_makes_no_http_call(monkeypatch):
    from hermes_cli.goal_intent import classify

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("hermes_cli.goal_intent.requests.post") as post:
        result = classify("status?", {})

    assert result["label"] == "unknown"
    assert result["confidence"] == 0.0
    post.assert_not_called()


def test_classifier_scoped_credential_wins_over_environment(monkeypatch, jev_key):
    from hermes_cli.goal_intent import classify

    scope = SimpleNamespace(
        get_secret=lambda name: "scoped-key" if name == "LITELLM_API_KEY" else None,
        installed=True,
    )
    import sys

    monkeypatch.setitem(sys.modules, "agent.secret_scope", SimpleNamespace(
        UnscopedSecretError=type("UnscopedSecretError", (Exception,), {}),
        get_secret=scope.get_secret,
    ))
    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_decision("chat", 0.9)) as post:
        classify("hi", {})

    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer scoped-key"


def test_classifier_caps_state_fields(jev_key):
    from hermes_cli import goal_intent

    long_message = "m" * 5000
    long_assistant = "a" * 3000
    long_previous = "p" * 4000
    with patch("hermes_cli.goal_intent.requests.post", return_value=_jev_decision("chat")) as post:
        goal_intent.classify(long_message, {
            "last_assistant": long_assistant,
            "user_previous_message": long_previous,
        })

    state = post.call_args.kwargs["json"]["state"]
    assert len(state["message"]) == goal_intent._MESSAGE_LIMIT == 3000
    assert len(state["assistant_last_reply"]) == goal_intent._LAST_ASSISTANT_LIMIT == 1200
    assert len(state["user_previous_message"]) == goal_intent._PREVIOUS_USER_LIMIT == 1500


def test_suppression_control_phrases():
    from hermes_cli.goal_intent import auto_goal_suppression

    assert auto_goal_suppression("stop") == "stop"
    assert auto_goal_suppression("  STOP now  ") == "stop"
    assert auto_goal_suppression("stopping the loop") == "stop"
    assert auto_goal_suppression("just answer, no goal") == "just answer"
    assert auto_goal_suppression("hey JUST ANSWER this") == "just answer"
    assert auto_goal_suppression("why is tdarr down") is None
    assert auto_goal_suppression("") is None


def test_judge_reviewer_bar_reaches_real_judge_request(hermes_home):
    from hermes_cli import goals

    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response('{"done":false,"reason":"rollback remains"}'),
    ) as call_llm:
        verdict, reason, _parse_failed, _wait, _truncated = goals.judge_goal(
            "ship the change", "The implementation is complete."
        )

    assert verdict == "continue"
    assert reason == "rollback remains"
    system_prompt = call_llm.call_args.kwargs["messages"][0]["content"]
    assert "no obvious remaining next step" in system_prompt
    assert "unfinished edges" in system_prompt
    assert "untried" in system_prompt
    assert "CONTINUE" in system_prompt


def test_done_verdict_at_turn_budget_pauses_instead_of_completing(hermes_home):
    from hermes_cli.goals import GoalManager

    manager = GoalManager(session_id="intent-done-at-cap", default_max_turns=1)
    manager.set("finish every requested edge", max_turns=1)
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "everything looks finished", False, None, False),
    ):
        decision = manager.evaluate_after_turn("Implemented the main path.")

    assert decision["verdict"] == "continue"
    assert decision["status"] == "paused"
    assert decision["should_continue"] is False
    assert manager.state is not None
    assert manager.state.status == "paused"
    assert manager.state.turns_used == manager.state.max_turns == 1


def _write_record(path, *, ts, label, excerpt, session="s1"):
    record = {
        "ts": ts,
        "session_id": session,
        "platform": "telegram",
        "message_excerpt": excerpt,
        "label": label,
        "reason": f"reason for {excerpt}",
        "confidence": 0.8,
        "contract_draft": None,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_intent_report_counts_and_returns_newest_examples(hermes_home):
    from hermes_cli.goal_intent import build_intent_report

    path = hermes_home / "goals" / "intent_shadow.jsonl"
    path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).isoformat()
    older = (now - timedelta(hours=3)).isoformat()
    newer = (now - timedelta(hours=1)).isoformat()
    _write_record(path, ts=old, label="chat", excerpt="outside window")
    _write_record(path, ts=older, label="chat", excerpt="older chat")
    _write_record(path, ts=newer, label="chat", excerpt="newest chat")
    _write_record(path, ts=newer, label="chase", excerpt="status?", session="s2")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"torn":')

    report = build_intent_report(since="7d", examples=1)

    assert report["counts"]["chat"] == 2
    assert report["counts"]["chase"] == 1
    assert report["counts"]["task"] == 0
    assert [row["message_excerpt"] for row in report["examples"]["chat"]] == ["newest chat"]
    assert [row["session_id"] for row in report["examples"]["chase"]] == ["s2"]
    assert all(row["message_excerpt"] != "outside window" for rows in report["examples"].values() for row in rows)


def test_goals_intent_report_is_registered_and_prints_counts(hermes_home, capsys):
    from hermes_cli.main import _build_cli_parser

    path = hermes_home / "goals" / "intent_shadow.jsonl"
    path.parent.mkdir(parents=True)
    _write_record(
        path,
        ts=datetime.now(timezone.utc).isoformat(),
        label="approval",
        excerpt="deploy it",
    )
    parser, _subparsers = _build_cli_parser()
    args = parser.parse_args(["goals", "intent-report", "--since", "7d", "--examples", "1"])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "approval: 1" in output
    assert "deploy it" in output


@pytest.mark.parametrize("since", ["0d", "-1h", "nonsense", "7x"])
def test_intent_report_rejects_invalid_windows(since):
    from hermes_cli.goal_intent import build_intent_report

    with pytest.raises(ValueError, match="since"):
        build_intent_report(since=since)
