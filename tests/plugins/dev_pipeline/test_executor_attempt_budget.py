"""Attempt budget tests — pipeline runs must not consume repair slots."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _regression_results():
    fail = ex.CommandResult(
        command="pytest",
        exit_code=1,
        output_path=Path("/tmp/log"),
        output_preview="fail",
    )
    return [fail]


def test_repair_budget_one_attempt_plus_pipeline_allows_one_repair(
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
    assert ex.count_attempt_runs(conn, task_id) == 1

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)
    meta = ex.load_run_metadata(conn, pipeline_run)

    with patch.object(ex, "git_command") as mock_git:
        with patch.object(ex, "run_verification", return_value=_regression_results()):
            with patch.object(ex, "classify_verification", return_value="regression"):
                with patch.object(ex, "unified_diff", return_value="diff"):
                    with patch.object(executor, "_spawn_attempt") as mock_spawn:
                        executor._phase_verifying(
                            conn,
                            task_id,
                            pipeline_run,
                            meta,
                            ex.pipeline_state(meta),
                        )
                        mock_spawn.assert_called_once()

    conn.close()


def test_repair_budget_second_regression_does_not_spawn(kanban_home, tmp_path):
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
    assert ex.count_attempt_runs(conn, task_id) == 2

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_VERIFYING)
    meta = ex.load_run_metadata(conn, pipeline_run)

    with patch.object(ex, "git_command"):
        with patch.object(ex, "run_verification", return_value=_regression_results()):
            with patch.object(ex, "classify_verification", return_value="regression"):
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
    conn.close()
