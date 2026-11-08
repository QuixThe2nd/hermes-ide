"""Contract tests for opt-in test evidence on coding-lane delegates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest


TASK = "Implement the feature and add pytest coverage for its edge cases"
VALID_EVIDENCE = {
    "tests_added": ["tests/test_feature.py"],
    "fails_on_base": "main",
    "passes_on_head": "feat/tested-feature",
    "edge_cases": ["rejects malformed input", "handles an empty result"],
}


def _success_result(**extra):
    return {
        "success": True,
        "error": None,
        "final_report": "Implemented and verified.",
        **extra,
    }


@pytest.mark.parametrize(
    "task_text",
    [
        TASK,
        "FIX the bug and update its spec",
        "Add test coverage for retries",
        "feature: exercise the parser with tests",
        "Bug reproduction in pytest",
    ],
)
def test_heuristic_matches_code_and_test_work(task_text):
    from tools.delegate_evidence_gate import requires_test_evidence

    assert requires_test_evidence(task_text) is True


@pytest.mark.parametrize(
    "task_text",
    [
        "Implement the feature without verification",
        "Run the pytest suite",
        "Write documentation",
        "",
    ],
)
def test_heuristic_rejects_non_code_or_non_test_work(task_text):
    from tools.delegate_evidence_gate import requires_test_evidence

    assert requires_test_evidence(task_text) is False


@pytest.mark.parametrize("configured", [False, "false", 0, None])
def test_gate_off_passes_result_through_unchanged(configured):
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result()
    gated = apply_evidence_gate(
        result, TASK, {"require_test_evidence": configured}
    )

    assert gated is result
    assert gated == _success_result()


def test_gate_on_heuristic_miss_passes_result_through_unchanged():
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result()
    gated = apply_evidence_gate(
        result, "Explain the implementation", {"require_test_evidence": True}
    )

    assert gated is result
    assert gated == _success_result()


def test_failed_result_does_not_need_test_evidence():
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result(success=False, error="delegate failed")
    gated = apply_evidence_gate(result, TASK, {"require_test_evidence": True})

    assert gated is result
    assert gated["error"] == "delegate failed"


def test_missing_evidence_downgrades_success_and_preserves_original():
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result(session_id="delegate-session")
    original = copy.deepcopy(result)
    gated = apply_evidence_gate(result, TASK, {"require_test_evidence": True})

    assert gated["success"] is False
    assert gated["error"] == "missing_test_evidence"
    assert gated["evidence_gate"]["original_result"] == original
    assert gated["final_report"] == original["final_report"]
    assert result == original


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        [],
        {**VALID_EVIDENCE, "edge_cases": []},
        {**VALID_EVIDENCE, "tests_added": "tests/test_feature.py"},
        {**VALID_EVIDENCE, "tests_added": [42]},
        {**VALID_EVIDENCE, "fails_on_base": ""},
        {**VALID_EVIDENCE, "fails_on_base": "   "},
        {**VALID_EVIDENCE, "passes_on_head": ""},
        {**VALID_EVIDENCE, "passes_on_head": "   "},
        {**VALID_EVIDENCE, "edge_cases": ["valid case", 42]},
    ],
)
def test_malformed_evidence_is_downgraded(evidence):
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result(test_evidence=evidence)
    original = copy.deepcopy(result)
    gated = apply_evidence_gate(result, TASK, {"require_test_evidence": True})

    assert gated["success"] is False
    assert gated["error"] == "missing_test_evidence"
    assert gated["evidence_gate"]["original_result"] == original
    assert result == original


def test_well_formed_evidence_passes_through_unchanged():
    from tools.delegate_evidence_gate import apply_evidence_gate

    result = _success_result(test_evidence=copy.deepcopy(VALID_EVIDENCE))
    original = copy.deepcopy(result)
    gated = apply_evidence_gate(result, TASK, {"require_test_evidence": True})

    assert gated is result
    assert gated == original
    assert "evidence_gate" not in gated


def _enable_gate(monkeypatch, module) -> None:
    monkeypatch.setattr(
        module,
        "_cfg",
        lambda: {"require_test_evidence": True},
        raising=False,
    )


def _run_claude_delegate(monkeypatch, tmp_path: Path, *, evidence):
    from tools import claude_agent_tool

    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        claude_agent_tool, "resolve_claude_binary", lambda model=None: "/fake/claude"
    )
    _enable_gate(monkeypatch, claude_agent_tool)

    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Implemented and verified.",
        "session_id": "delegate-session",
    }
    if evidence is not ...:
        result_event["test_evidence"] = evidence

    def _fake_run(cmd, **kwargs):
        log_dir = kwargs["log_dir"]
        return (
            None,
            str(log_dir / "fake-log.jsonl"),
            json.dumps(result_event),
            0.5,
            0,
        )

    monkeypatch.setattr(claude_agent_tool, "_run_and_stream", _fake_run)
    return json.loads(
        claude_agent_tool.delegate_claude_agent(task=TASK, workdir=str(workdir))
    )


def test_claude_success_without_evidence_is_downgraded(monkeypatch, tmp_path):
    result = _run_claude_delegate(monkeypatch, tmp_path, evidence=...)

    assert result["success"] is False
    assert result["error"] == "missing_test_evidence"
    original = result["evidence_gate"]["original_result"]
    assert original["success"] is True
    assert original["final_report"] == "Implemented and verified."


def test_claude_success_with_evidence_passes_through(monkeypatch, tmp_path):
    result = _run_claude_delegate(
        monkeypatch, tmp_path, evidence=copy.deepcopy(VALID_EVIDENCE)
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["test_evidence"] == VALID_EVIDENCE
    assert "evidence_gate" not in result


def _run_cursor_delegate(monkeypatch, tmp_path: Path, *, evidence):
    from tools import cursor_agent_tool
    from tools.cursor_run_receipts import deterministic_client_agent_id

    workdir = tmp_path / "repo"
    workdir.mkdir()
    secret = tmp_path / "cursor-cloud.env"
    secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
    monkeypatch.setattr(
        cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/fake/agent"
    )
    monkeypatch.setattr(
        cursor_agent_tool,
        "resolve_workdir_origin",
        lambda workdir: "https://github.com/acme/demo",
    )
    monkeypatch.setattr(
        cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main"
    )
    monkeypatch.setattr(
        cursor_agent_tool, "detect_unpushed_head_commits", lambda workdir: None
    )
    _enable_gate(monkeypatch, cursor_agent_tool)

    session_id = "evidence-session"
    tool_call_id = "evidence-call"
    agent_id = deterministic_client_agent_id(session_id, tool_call_id)
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (
            {
                "id": agent_id,
                "name": "hermes-evidence-test",
                "latestRunId": "run-evidence",
            },
            {"id": "run-evidence", "agentId": agent_id, "status": "CREATING"},
        ),
    )
    run = {
        "id": "run-evidence",
        "agentId": agent_id,
        "status": "FINISHED",
        "result": "Implemented and verified.",
    }
    if evidence is not ...:
        run["test_evidence"] = evidence
    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", lambda **kwargs: run)

    return json.loads(
        cursor_agent_tool.delegate_cursor_agent(
            task=TASK,
            workdir=str(workdir),
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )


def test_cursor_success_without_evidence_is_downgraded(monkeypatch, tmp_path):
    result = _run_cursor_delegate(monkeypatch, tmp_path, evidence=...)

    assert result["success"] is False
    assert result["error"] == "missing_test_evidence"
    original = result["evidence_gate"]["original_result"]
    assert original["success"] is True
    assert original["final_report"] == "Implemented and verified."
    assert original["cloud_status"] == "FINISHED"


def test_cursor_success_with_evidence_passes_through(monkeypatch, tmp_path):
    result = _run_cursor_delegate(
        monkeypatch, tmp_path, evidence=copy.deepcopy(VALID_EVIDENCE)
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["test_evidence"] == VALID_EVIDENCE
    assert "evidence_gate" not in result
