"""Verification classification tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex
from hermes_cli import kanban_db as kb


def _executor_cfg() -> dict:
    return {
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    }


def _dev_block_kinds(conn, task_id: str) -> list[str]:
    return [
        (ev.payload or {}).get("block_kind")
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_blocked"
    ]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _result(cmd: str, code: int) -> ex.CommandResult:
    return ex.CommandResult(command=cmd, exit_code=code, output_path=Path("/tmp/x.log"))


def test_candidate_pass():
    assert ex.classify_verification([_result("true", 0)]) == "pass"


def test_candidate_fail_base_fail_baseline():
    cand = [_result("pytest", 1)]
    base = [_result("pytest", 1)]
    assert ex.classify_verification(cand, base) == "baseline_failure"


def test_candidate_fail_base_pass_regression():
    cand = [_result("pytest", 1)]
    base = [_result("pytest", 0)]
    assert ex.classify_verification(cand, base) == "regression"


def test_repair_prompt_contains_failure_evidence():
    results = [
        ex.CommandResult(
            command="pytest tests/foo.py",
            exit_code=1,
            output_path=Path("/tmp/log"),
            output_preview="AssertionError: boom",
        )
    ]
    prompt = ex.build_repair_prompt(
        "fix foo", {"task_summary": "x"}, results, "diff here"
    )
    assert "pytest tests/foo.py" in prompt
    assert "AssertionError: boom" in prompt
    assert "diff here" in prompt


def test_verifying_acceptance_timeout_classified(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 1",
        metadata=ex.merge_pipeline_state(
            {},
            {
                "run_kind": ex.RUN_KIND_ATTEMPT,
                "unit_started": True,
                "candidate_commit": "bbb",
            },
        ),
    )
    ex.start_new_run(
        conn,
        task_id,
        metadata={
            "dev_pipeline": {"run_kind": ex.RUN_KIND_ATTEMPT, "unit_started": True}
        },
    )
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 2 repair",
        metadata={
            "dev_pipeline": {"run_kind": ex.RUN_KIND_ATTEMPT, "unit_started": True}
        },
    )
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_VERIFYING,
                "repair_used": True,
                "contract": {
                    "task_summary": "x",
                    "acceptance_commands": ["pytest"],
                },
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)

    timeout_exc = subprocess.TimeoutExpired(cmd="pytest", timeout=600)
    heartbeat_calls: list[tuple] = []
    base_pass = [
        ex.CommandResult(
            command="pytest",
            exit_code=0,
            output_path=Path("/tmp/base.log"),
        )
    ]
    verify_calls = {"count": 0}

    def fake_verification(*_args, **_kwargs):
        verify_calls["count"] += 1
        if verify_calls["count"] == 1:
            raise timeout_exc
        return base_pass

    def track_heartbeat(*args, **kwargs):
        heartbeat_calls.append((args, kwargs))

    with patch.object(ex, "git_command"):
        with patch.object(ex, "git_head_sha", return_value="bbb"):
            with patch.object(ex, "run_verification", side_effect=fake_verification):
                with patch.object(kb, "heartbeat_claim", side_effect=track_heartbeat):
                    executor._phase_verifying(
                        conn,
                        task_id,
                        pipeline_run,
                        meta,
                        ex.pipeline_state(meta),
                    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert "acceptance_timeout" in _dev_block_kinds(conn, task_id)
    assert "verification_regression" not in _dev_block_kinds(conn, task_id)
    assert task_id not in executor._active
    assert len(heartbeat_calls) >= 1
    conn.close()


def test_verifying_exhausted_regression_emits_typed_dev_blocked_event(
    kanban_home, tmp_path
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 1",
        metadata={
            "dev_pipeline": {"run_kind": ex.RUN_KIND_ATTEMPT, "unit_started": True}
        },
    )
    ex.start_new_run(
        conn,
        task_id,
        metadata={"dev_pipeline": {"run_kind": ex.RUN_KIND_ATTEMPT}},
    )
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 2 repair",
        metadata={
            "dev_pipeline": {"run_kind": ex.RUN_KIND_ATTEMPT, "unit_started": True}
        },
    )
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_VERIFYING,
                "repair_used": True,
                "contract": {
                    "task_summary": "x",
                    "acceptance_commands": ["pytest"],
                },
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)

    fail = ex.CommandResult(
        command="pytest",
        exit_code=1,
        output_path=Path("/tmp/log"),
        output_preview="fail",
    )
    with patch.object(ex, "git_command"):
        with patch.object(ex, "run_verification", return_value=[fail]):
            with patch.object(ex, "classify_verification", return_value="regression"):
                executor._phase_verifying(
                    conn,
                    task_id,
                    pipeline_run,
                    meta,
                    ex.pipeline_state(meta),
                )

    assert "verification_regression" in _dev_block_kinds(conn, task_id)
    conn.close()


def test_verifying_base_timeout_blocks_as_acceptance_timeout_not_baseline(
    kanban_home, tmp_path
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 1",
        metadata=ex.merge_pipeline_state(
            {},
            {
                "run_kind": ex.RUN_KIND_ATTEMPT,
                "unit_started": True,
                "candidate_commit": "bbb",
            },
        ),
    )
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_VERIFYING,
                "contract": {
                    "task_summary": "x",
                    "acceptance_commands": ["pytest"],
                },
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)

    cand_fail = [
        ex.CommandResult(
            command="pytest",
            exit_code=1,
            output_path=Path("/tmp/cand.log"),
            output_preview="fail",
        )
    ]
    base_timeout = subprocess.TimeoutExpired(cmd="pytest", timeout=600)
    verify_calls = {"count": 0}

    def fake_verification(*_args, **_kwargs):
        verify_calls["count"] += 1
        if verify_calls["count"] == 1:
            return cand_fail
        raise base_timeout

    with patch.object(ex, "git_command"):
        with patch.object(ex, "git_head_sha", return_value="bbb"):
            with patch.object(ex, "run_verification", side_effect=fake_verification):
                with patch.object(ex, "unified_diff", return_value="diff"):
                    with patch.object(executor, "_spawn_attempt") as mock_spawn:
                        executor._phase_verifying(
                            conn,
                            task_id,
                            pipeline_run,
                            meta,
                            ex.pipeline_state(meta),
                        )
                        mock_spawn.assert_not_called()

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert "acceptance_timeout" in _dev_block_kinds(conn, task_id)
    assert "verification_regression" not in _dev_block_kinds(conn, task_id)
    assert task_id not in executor._active
    conn.close()


def test_verification_repair_prompt_not_double_wrapped(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    unique_task = "unique verification repair task marker"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": unique_task}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt 1",
        metadata=ex.merge_pipeline_state(
            {},
            {
                "run_kind": ex.RUN_KIND_ATTEMPT,
                "unit_started": True,
                "candidate_commit": "bbb",
            },
        ),
    )
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_VERIFYING,
                "contract": {
                    "task_summary": "unique summary marker",
                    "acceptance_commands": ["pytest"],
                },
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)

    fail = ex.CommandResult(
        command="pytest",
        exit_code=1,
        output_path=Path("/tmp/log"),
        output_preview="fail",
    )
    pass_result = ex.CommandResult(
        command="pytest",
        exit_code=0,
        output_path=Path("/tmp/base.log"),
    )

    with patch.object(ex, "git_command"):
        with patch.object(ex, "run_verification", side_effect=[[fail], [pass_result]]):
            with patch.object(ex, "unified_diff", return_value="diff"):
                with patch.object(executor, "_is_active", return_value=(False, "")):
                    with patch.object(
                        ex,
                        "systemd_run_attempt",
                        return_value=(True, 9999, 1_700_000_000),
                    ):
                        executor._phase_verifying(
                            conn,
                            task_id,
                            pipeline_run,
                            meta,
                            ex.pipeline_state(meta),
                        )

    new_run_id = executor._active[task_id].run_id
    new_meta = ex.load_run_metadata(conn, new_run_id)
    prompt = ex.pipeline_state(new_meta).get("attempt_prompt") or ""
    assert prompt.count("Task:\n") == 1
    assert prompt.count(unique_task) == 1
    assert prompt.count("unique summary marker") == 1
    conn.close()
