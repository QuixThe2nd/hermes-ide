"""Behavior tests for shadow goal-intent classification and reporting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest


HAND_LABELLED_EXAMPLES = (
    ("chat", "hi"),
    ("chat", "huh"),
    ("chat", "what is a wet lab"),
    ("chat", "can u explain graph engineering in ai plz"),
    ("chat", "what apps do people sideload"),
    ("chat", "I have my own certs. List BYO cert providers"),
    ("chat", "gimme ideas of what i can do with my apple developer acc"),
    ("chat", "tony got a new phone btw"),
    ("chat", "fuck that i love ai"),
    ("task", "why is tdarr down"),
    ("task", "why is element so slow right now"),
    ("task", "plz sign this for my phone https://github.com/Nouvborne/Ketamine"),
    ("task", "can u make ipa.yazdani.au only available locally and on tailscale? not public plz"),
    ("task", "can u find all codepaths that use /approve and /deny please"),
    ("task", "remove anything our config doesnt use"),
    ("task", "can u monitor what processes use the most upload?"),
    ("approval", "merge"),
    ("approval", "deploy it"),
    ("approval", "yeah go ahead"),
    ("approval", "great! merge and deploy"),
    ("chase", "status?"),
    ("chase", "so u done?"),
    ("chase", "howd it go"),
    ("chase", "check on the claude run, u sure its still running?"),
    ("chase", "u got interrupted, continue"),
    ("chase", "youve been restarted"),
    ("chase", "again, winnie didnt dm him"),
    ("chase", "better, still slow asf"),
    ("chase", "honestly, i want u to get just do whatever u think, experiment bro, keep changing, optimise, find whats best"),
)


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


def test_classifier_uses_goal_intent_aux_and_reuses_contract_draft():
    from hermes_cli.goal_intent import classify
    from hermes_cli.goals import GoalContract

    message = "can u make the service private please"
    contract = GoalContract(
        outcome="The service is private.",
        verification="An external request is refused.",
    )
    context = {
        "goal_active": True,
        "goal_recently_completed": False,
        "goal_paused": True,
        "last_assistant": "The public route is still enabled; should I change it?",
    }
    with (
        patch("agent.auxiliary_client.call_llm", return_value=_response(
            '{"label":"task","reason":"requests a configuration change","confidence":0.91}'
        )) as call_llm,
        patch("hermes_cli.goals.draft_contract", return_value=contract) as draft,
    ):
        result = classify(message, context)

    assert result == {
        "label": "task",
        "reason": "requests a configuration change",
        "confidence": 0.91,
        "contract_draft": contract.to_dict(),
    }
    assert call_llm.call_args.kwargs["task"] == "goal_intent"
    assert call_llm.call_args.kwargs["temperature"] == 0
    prompt = "\n".join(
        str(item.get("content", ""))
        for item in call_llm.call_args.kwargs["messages"]
    )
    assert message in prompt
    assert "The public route is still enabled; should I change it?" in prompt
    assert '"goal_active": true' in prompt
    assert '"goal_paused": true' in prompt
    assert draft.call_args.kwargs["objective"] == message


@pytest.mark.parametrize("label", ["chat", "approval", "chase"])
def test_classifier_accepts_fenced_json_for_non_task_labels(label):
    from hermes_cli.goal_intent import classify

    payload = f'```json\n{{"label":"{label}","reason":"fixture","confidence":1}}\n```'
    with (
        patch("agent.auxiliary_client.call_llm", return_value=_response(payload)),
        patch("hermes_cli.goals.draft_contract") as draft,
    ):
        result = classify("status?", {})

    assert result["label"] == label
    assert result["confidence"] == 1.0
    assert result["contract_draft"] is None
    draft.assert_not_called()


def test_classifier_extracts_embedded_task_json_and_survives_draft_failure():
    from hermes_cli.goal_intent import classify

    with (
        patch("agent.auxiliary_client.call_llm", return_value=_response(
            'Result: {"label":"task","reason":"change requested","confidence":0.7}'
        )),
        patch("hermes_cli.goals.draft_contract", side_effect=RuntimeError("draft unavailable")),
    ):
        result = classify("remove anything unused", {})

    assert result["label"] == "task"
    assert result["reason"] == "change requested"
    assert result["confidence"] == 0.7
    assert result["contract_draft"] is None


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        '{"label":"other","reason":"bad label","confidence":0.8}',
        '{"label":"chat","reason":"bad confidence","confidence":"high"}',
        '{"label":"chat","reason":"bad confidence","confidence":NaN}',
        '{"label":"chat","reason":"missing confidence"}',
    ],
)
def test_classifier_rejects_bad_responses_as_unknown(content):
    from hermes_cli.goal_intent import classify

    with (
        patch("agent.auxiliary_client.call_llm", return_value=_response(content)),
        patch("hermes_cli.goals.draft_contract") as draft,
    ):
        result = classify("do something", {})

    assert result["label"] == "unknown"
    assert result["reason"]
    assert result["confidence"] == 0.0
    assert result["contract_draft"] is None
    draft.assert_not_called()


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("transport failed")])
def test_classifier_aux_errors_are_fail_safe(error):
    from hermes_cli.goal_intent import classify

    with patch("agent.auxiliary_client.call_llm", side_effect=error):
        result = classify("status?", {})

    assert result["label"] == "unknown"
    assert result["confidence"] == 0.0
    assert result["contract_draft"] is None


def test_classifier_prompt_contains_hand_labelled_decision_boundaries():
    from hermes_cli.goal_intent import classify

    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response('{"label":"chat","reason":"fixture","confidence":0.5}'),
    ) as call_llm:
        classify("current message", {})

    system_prompt = call_llm.call_args.kwargs["messages"][0]["content"]
    for _label, example in HAND_LABELLED_EXAMPLES:
        assert example in system_prompt
    assert "state-change" in system_prompt
    assert "why is" in system_prompt
    assert "keep going" in system_prompt


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
