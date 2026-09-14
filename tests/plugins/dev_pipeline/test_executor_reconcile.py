"""Reconcile matrix and executor-level integration tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex
from hermes_cli import kanban_db as kb


@pytest.mark.parametrize(
    "unit_active,pid_match,candidate,phase,unit_started,attempts,max_attempts,expected_action,expected_phase,expected_reason",
    [
        (True, True, "abc", "RUNNING", False, 1, 2, "adopt", "RUNNING", None),
        (True, False, None, "RUNNING", False, 1, 2, "unit_gone", None, "pid_mismatch"),
        (False, False, "abc123", "RUNNING", True, 1, 2, "resume", "VERIFYING", None),
        (False, False, "abc123", "RUNNING", False, 1, 2, "retry", "RUNNING", None),
        (False, False, None, "RUNNING", False, 1, 2, "retry", "RUNNING", None),
        (
            False,
            False,
            None,
            "RUNNING",
            False,
            2,
            2,
            "block",
            None,
            "executor_restarted",
        ),
        (False, False, "abc", "REVIEWING", False, 1, 2, "resume", "REVIEWING", None),
        (False, False, "abc", "PUBLISHING", False, 1, 2, "resume", "PUBLISHING", None),
    ],
)
def test_reconcile_task_state_matrix(
    unit_active,
    pid_match,
    candidate,
    phase,
    unit_started,
    attempts,
    max_attempts,
    expected_action,
    expected_phase,
    expected_reason,
):
    state = {"phase": phase, "unit_started": unit_started}
    if candidate is not None:
        state["candidate_commit"] = candidate
    decision = ex.reconcile_task_state(
        state,
        unit_active=unit_active,
        pid_match=pid_match,
        candidate_commit=candidate if unit_started else None,
        unit_started=unit_started,
        attempts_used=attempts,
        max_attempts=max_attempts,
    )
    assert decision.action == expected_action
    if expected_phase:
        assert decision.phase == expected_phase
    if expected_reason:
        assert decision.reason == expected_reason


@pytest.fixture
def kanban_home_fixture(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("dev")
    return kb.connect(board="dev")


def _seed_running_task(conn, metadata: dict) -> tuple[str, int]:
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"repo": "/tmp/r", "branch": "main", "task": "work"}),
        workspace_kind="scratch",
        board="dev",
    )
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock='dev-executor', claim_expires=? WHERE id=?",
        (int(time.time()) + 900, task_id),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, metadata, claim_lock, claim_expires) "
        "VALUES (?, 'running', ?, ?, 'dev-executor', ?)",
        (
            task_id,
            int(time.time()),
            json.dumps({
                "dev_pipeline": {
                    **metadata,
                    "run_kind": metadata.get("run_kind", ex.RUN_KIND_ATTEMPT),
                }
            }),
            int(time.time()) + 900,
        ),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task_id))
    conn.commit()
    return task_id, int(run_id)


def test_reconcile_applies_adopt_to_executor_active_set(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, run_id = _seed_running_task(
        conn,
        {
            "phase": "RUNNING",
            "unit_pid": 99,
            "host_start_time": 42,
            "last_jsonl_size": 100,
            "last_jsonl_growth_at": 1234.5,
        },
    )
    unit = ex.unit_name(task_id, run_id)
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (
            json.dumps({
                "dev_pipeline": {
                    "phase": "RUNNING",
                    "unit_name": unit,
                    "unit_pid": 99,
                    "host_start_time": 42,
                    "last_jsonl_size": 100,
                    "last_jsonl_growth_at": 1234.5,
                }
            }),
            run_id,
        ),
    )
    conn.commit()

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })

    def fake_active(u):
        return u == unit, "active"

    def fake_pid(pid, start):
        return pid == 99 and start == 42

    ex.reconcile_board(
        conn,
        executor.cfg,
        executor=executor,
        is_active_fn=fake_active,
        pid_match_fn=fake_pid,
    )
    assert task_id in executor._active
    assert executor._active[task_id].phase == ex.PHASE_RUNNING
    assert executor._active[task_id].last_jsonl_size == 100


def test_reconcile_resume_reviewing_advances_executor(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, run_id = _seed_running_task(
        conn,
        {
            "phase": "REVIEWING",
            "candidate_commit": "deadbeef",
            "repo_path": "/tmp/fake",
            "logs_root": "/tmp/logs",
            "base_commit": "aaa",
            "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
            "mechanical_pass": True,
        },
    )
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })

    with patch.object(ex, "unified_diff", return_value="safe"):
        with patch.object(executor, "_phase_reviewing") as mock_review:
            ex.reconcile_board(
                conn,
                executor.cfg,
                executor=executor,
                is_active_fn=lambda _u: (False, "inactive"),
            )
            assert task_id in executor._active
            assert executor._active[task_id].phase == ex.PHASE_REVIEWING
            mock_review.assert_called_once()


def test_reconcile_retry_spawns_attempt(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, _run_id = _seed_running_task(
        conn,
        {
            "phase": "RUNNING",
            "repo_path": "/tmp/repo",
            "logs_root": "/tmp/logs",
            "contract": {"task_summary": "x"},
        },
    )
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })

    with patch.object(executor, "_spawn_attempt") as mock_spawn:
        ex.reconcile_board(
            conn,
            executor.cfg,
            executor=executor,
            is_active_fn=lambda _u: (False, "inactive"),
        )
        assert task_id in executor._active
        mock_spawn.assert_called_once()


def test_reconcile_block_marks_task_blocked(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, _run_id = _seed_running_task(
        conn,
        {"phase": "RUNNING", "run_kind": "attempt", "unit_started": True},
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, 'crashed', ?, ?, 'crashed', ?)",
        (
            task_id,
            int(time.time()),
            int(time.time()),
            json.dumps({"dev_pipeline": {"run_kind": "attempt", "unit_started": True}}),
        ),
    )
    conn.commit()

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    ex.reconcile_board(
        conn,
        executor.cfg,
        executor=executor,
        is_active_fn=lambda _u: (False, "inactive"),
    )
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    assert task_id not in executor._active


def test_fresh_executor_tick_adopts_running_task_after_restart(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, run_id = _seed_running_task(
        conn,
        {"phase": "PLANNING", "repo": "/tmp/r", "branch": "main"},
    )
    conn.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (
            json.dumps({
                "dev_pipeline": {
                    "phase": "PLANNING",
                    "repo": "/tmp/r",
                    "branch": "main",
                }
            }),
            run_id,
        ),
    )
    conn.commit()

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })

    contract = {
        "task_summary": "x",
        "lane_hint": "cursor",
        "estimated_minutes": 10,
        "allowed_paths": ["src/**"],
        "acceptance_commands": ["true"],
        "broad_flags": {
            "migration": False,
            "repo_wide_change": False,
            "toolchain_change": False,
            "multi_subsystem": False,
            "long_verification": False,
        },
        "blocked_reasons": [],
        "step_plan": [{"id": "s1", "description": "d", "verifiable": True}],
        "assumptions": [],
    }

    with patch.object(ex, "clone_repo", return_value=(True, "/tmp/r")):
        with patch.object(ex, "build_repo_summary", return_value="summary"):
            with patch.object(ex, "run_planning", return_value=(contract, "", [])):
                ex.reconcile_board(
                    conn,
                    executor.cfg,
                    executor=executor,
                    is_active_fn=lambda _u: (False, "inactive"),
                )
                assert task_id in executor._active
                executor._advance(conn, task_id)
                meta = ex.load_run_metadata(conn, executor._active[task_id].run_id)
                assert ex.pipeline_state(meta).get("phase") in {
                    ex.PHASE_ROUTING,
                    ex.PHASE_PREPARING,
                    ex.PHASE_RUNNING,
                }


def test_reconcile_planning_resumes_planning_not_spawn(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, run_id = _seed_running_task(conn, {"phase": "PLANNING"})
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    with patch.object(
        ex, "run_planning", return_value=({"task_summary": "x"}, None, [])
    ) as mock_plan:
        with patch.object(ex, "clone_repo", return_value=(True, "")):
            with patch.object(ex, "build_repo_summary", return_value="summary"):
                with patch.object(executor, "_spawn_attempt") as mock_spawn:
                    ex.reconcile_board(
                        conn,
                        executor.cfg,
                        executor=executor,
                        is_active_fn=lambda _u: (False, "inactive"),
                    )
                    mock_spawn.assert_not_called()
                    mock_plan.assert_called_once()
    assert task_id in executor._active


def test_reconcile_preparing_reruns_prepare_not_spawn(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, _run_id = _seed_running_task(
        conn,
        {
            "phase": "PREPARING",
            "repo": "/tmp/r",
            "branch": "main",
            "contract": {"task_summary": "x"},
        },
    )
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    with patch.object(ex, "start_new_run") as mock_new_run:
        with patch.object(ex, "clone_repo", return_value=(True, "/tmp/r")):
            with patch.object(
                ex, "ensure_dev_branch", return_value=("hermes-dev/t", "abc")
            ):
                with patch.object(ex, "install_pinned_agents", return_value="pinned"):
                    with patch.object(
                        ex, "systemd_run_attempt", return_value=(True, 1, 100)
                    ):
                        ex.reconcile_board(
                            conn,
                            executor.cfg,
                            executor=executor,
                            is_active_fn=lambda _u: (False, "inactive"),
                        )
                        mock_new_run.assert_not_called()


def test_reconcile_stale_candidate_without_unit_started_retries(
    kanban_home_fixture, tmp_path
):
    conn = kanban_home_fixture
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, _run_id = _seed_running_task(
        conn,
        {
            "phase": "RUNNING",
            "candidate_commit": "C1",
            "unit_started": False,
            "repo_path": str(repo),
        },
    )
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    with patch.object(ex, "git_head_sha", return_value="C2"):
        with patch.object(executor, "_spawn_attempt"):
            decisions = ex.reconcile_board(
                conn,
                executor.cfg,
                executor=executor,
                is_active_fn=lambda _u: (False, "inactive"),
            )
    assert len(decisions) == 1
    assert decisions[0].action == "retry"
    assert decisions[0].phase == ex.PHASE_RUNNING


def test_start_new_attempt_clears_stale_candidate(kanban_home_fixture):
    conn = kanban_home_fixture
    task_id, _run_id = _seed_running_task(
        conn,
        {
            "phase": "RUNNING",
            "candidate_commit": "C1",
            "unit_name": "old-unit",
            "unit_pid": 99,
            "unit_started": True,
        },
    )
    new_run = ex.start_new_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_RUNNING,
                "candidate_commit": "C1",
                "unit_name": "old-unit",
                "unit_pid": 99,
                "host_start_time": 1,
                "jsonl_path": "/tmp/old.jsonl",
            },
        ),
    )
    st = ex.pipeline_state(ex.load_run_metadata(conn, new_run))
    assert st.get("candidate_commit") is None
    assert st.get("unit_name") is None
    assert st.get("unit_started") is False
    assert st.get("spawn_pending") is True


def test_resolve_reconcile_candidate_head_equals_base_is_not_candidate():
    state = {
        "phase": ex.PHASE_RUNNING,
        "unit_started": True,
        "base_commit": "abc123",
        "repo_path": "/tmp/repo",
    }
    with patch.object(ex, "git_head_sha", return_value="abc123"):
        candidate, unit_started = ex.resolve_reconcile_candidate(state)
    assert candidate is None
    assert unit_started is True


def test_reconcile_head_equals_base_retries_not_verify():
    state = {
        "phase": ex.PHASE_RUNNING,
        "unit_started": True,
        "base_commit": "abc123",
        "repo_path": "/tmp/repo",
    }
    with patch.object(ex, "git_head_sha", return_value="abc123"):
        candidate, unit_started = ex.resolve_reconcile_candidate(state)
    decision = ex.reconcile_task_state(
        state,
        unit_active=False,
        pid_match=False,
        candidate_commit=candidate,
        unit_started=unit_started,
        attempts_used=1,
        max_attempts=2,
    )
    assert decision.action == "retry"
    assert decision.phase == ex.PHASE_RUNNING


def test_reconcile_head_equals_base_blocks_when_attempts_exhausted():
    state = {
        "phase": ex.PHASE_RUNNING,
        "unit_started": True,
        "base_commit": "abc123",
        "repo_path": "/tmp/repo",
    }
    with patch.object(ex, "git_head_sha", return_value="abc123"):
        candidate, unit_started = ex.resolve_reconcile_candidate(state)
    decision = ex.reconcile_task_state(
        state,
        unit_active=False,
        pid_match=False,
        candidate_commit=candidate,
        unit_started=unit_started,
        attempts_used=2,
        max_attempts=2,
    )
    assert decision.action == "block"
    assert decision.reason == "executor_restarted"
