"""Retry workspace reset and attempts-exhausted blocking tests."""

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


def _dev_block_kinds(conn, task_id: str) -> list[str]:
    return [
        (ev.payload or {}).get("block_kind")
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_blocked"
    ]


def _dev_phase_payloads(conn, task_id: str, *, phase: str) -> list[dict]:
    return [
        ev.payload or {}
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_phase" and (ev.payload or {}).get("phase") == phase
    ]


def test_retry_resets_workspace_before_spawn(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement feature"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run_id = kb.latest_run(conn, task_id).id
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "run_kind": ex.RUN_KIND_ATTEMPT,
            "unit_started": True,
            "repo_path": str(repo),
            "logs_root": str(logs),
            "base_commit": "aaa111",
            "dev_branch": "hermes-dev/t1",
            "contract": {"task_summary": "summary"},
        },
    )
    ex.save_run_metadata(conn, run_id, meta)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_RUNNING)
    events: list[str] = []

    def track_git(args, **kwargs):
        if args[0:1] == ["checkout"]:
            events.append("checkout")
        elif args[0:2] == ["reset", "--hard"]:
            events.append("reset")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def track_spawn(*_args, **_kwargs):
        events.append("spawn")

    with patch.object(ex, "git_head_sha", return_value="dirtybbb"):
        with patch.object(ex, "git_command", side_effect=track_git):
            with patch.object(executor, "_spawn_attempt", side_effect=track_spawn):
                executor._finish_attempt(
                    conn,
                    task_id,
                    run_id,
                    meta,
                    exit_code=1,
                    classification_hint="crashed",
                )

    assert events == ["checkout", "reset", "spawn"]
    reset_events = [
        p
        for p in _dev_phase_payloads(conn, task_id, phase=ex.PHASE_RUNNING)
        if p.get("retry_reset_to") == "aaa111"
    ]
    assert reset_events
    conn.close()


def test_attempts_exhausted_block_kind(kanban_home, tmp_path):
    assert "attempts_exhausted" in ex.DEV_BLOCK_KINDS
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement feature"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="crashed",
        summary="attempt 1",
        metadata=ex.merge_pipeline_state(
            {},
            {"run_kind": ex.RUN_KIND_ATTEMPT, "unit_started": True},
        ),
    )
    run_id = ex.start_new_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_RUNNING,
                "run_kind": ex.RUN_KIND_ATTEMPT,
                "unit_started": True,
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa111",
                "dev_branch": "hermes-dev/t1",
                "contract": {"task_summary": "summary"},
            },
        ),
    )
    meta = ex.load_run_metadata(conn, run_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_RUNNING)

    with patch.object(ex, "git_head_sha", return_value="bbb"):
        with patch.object(ex, "git_command") as mock_git:
            with patch.object(executor, "_spawn_attempt") as mock_spawn:
                executor._finish_attempt(
                    conn,
                    task_id,
                    run_id,
                    meta,
                    exit_code=1,
                    classification_hint="crashed",
                )
                mock_spawn.assert_not_called()
                mock_git.assert_not_called()

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert "attempts_exhausted" in _dev_block_kinds(conn, task_id)
    assert task_id not in executor._active
    conn.close()


@pytest.mark.parametrize(
    "failing_git_args",
    [
        ["checkout", "hermes-dev/t1"],
        ["reset", "--hard", "aaa111"],
    ],
)
def test_retry_git_reset_failure_blocks_without_spawn(
    kanban_home, tmp_path, failing_git_args
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement feature"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run_id = kb.latest_run(conn, task_id).id
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "run_kind": ex.RUN_KIND_ATTEMPT,
            "unit_started": True,
            "repo_path": str(repo),
            "logs_root": str(logs),
            "base_commit": "aaa111",
            "dev_branch": "hermes-dev/t1",
            "contract": {"task_summary": "summary"},
        },
    )
    ex.save_run_metadata(conn, run_id, meta)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_RUNNING)

    def failing_git(args, **kwargs):
        if list(args) == failing_git_args:
            return type(
                "P", (), {"returncode": 1, "stdout": "", "stderr": "git failed"}
            )()
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(ex, "git_head_sha", return_value="dirtybbb"):
        with patch.object(ex, "git_command", side_effect=failing_git):
            with patch.object(executor, "_spawn_attempt") as mock_spawn:
                executor._finish_attempt(
                    conn,
                    task_id,
                    run_id,
                    meta,
                    exit_code=1,
                    classification_hint="crashed",
                )
                mock_spawn.assert_not_called()

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert "infra_broken" in _dev_block_kinds(conn, task_id)
    assert task_id not in executor._active
    conn.close()
