"""Phase machine and fail-closed block kind tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from plugins.dev_pipeline import executor as ex


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


def _phase_names(conn, task_id: str) -> list[str]:
    phases = []
    for ev in kb.list_events(conn, task_id):
        if ev.kind == "dev_phase" and ev.payload:
            phases.append(ev.payload.get("phase"))
    return phases


def test_planning_records_phase_transitions(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"repo": "/tmp/r", "branch": "main", "task": "do thing"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_PLANNING)

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
                executor._phase_planning(conn, task_id, run.id, {}, {})
                executor._phase_routing(
                    conn,
                    task_id,
                    run.id,
                    ex.load_run_metadata(conn, run.id),
                    ex.pipeline_state(ex.load_run_metadata(conn, run.id)),
                )

    assert ex.PHASE_ROUTING in _phase_names(
        conn, task_id
    ) or ex.PHASE_PREPARING in _phase_names(conn, task_id)
    conn.close()


@pytest.mark.parametrize(
    "block_kind,planning_return",
    [
        ("plan_invalid", (None, "plan_invalid", [])),
        ("planning_unavailable", (None, "planning_unavailable", [])),
    ],
)
def test_planning_fail_closed_block_kinds(kanban_home, block_kind, planning_return):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"repo": "/tmp/r", "branch": "main", "task": "do thing"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_PLANNING)

    with patch.object(ex, "clone_repo", return_value=(True, "/tmp/r")):
        with patch.object(ex, "build_repo_summary", return_value="summary"):
            with patch.object(ex, "run_planning", return_value=planning_return):
                executor._phase_planning(conn, task_id, run.id, {}, {})

    assert block_kind in _dev_block_kinds(conn, task_id)
    conn.close()


def test_routing_broad_lane_persists_claude_endurance(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body="{}",
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_ROUTING)
    meta = ex.merge_pipeline_state(
        {},
        {
            "contract": {
                "task_summary": "x",
                "lane_hint": "broad",
                "estimated_minutes": 10,
                "allowed_paths": ["a"],
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
        },
    )
    executor._phase_routing(conn, task_id, run.id, meta, ex.pipeline_state(meta))
    saved = ex.pipeline_state(ex.load_run_metadata(conn, run.id))
    assert saved.get("lane") == "claude-endurance"
    assert saved.get("phase") == ex.PHASE_PREPARING
    conn.close()


def test_review_unavailable_blocks(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "x"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    meta = ex.merge_pipeline_state(
        {},
        {
            "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
            "repo_path": str(repo),
            "logs_root": str(logs),
            "base_commit": "aaa",
            "candidate_commit": "bbb",
            "mechanical_pass": True,
        },
    )
    ex.save_run_metadata(conn, run.id, meta)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_REVIEWING)

    with patch.object(ex, "unified_diff", return_value="diff"):
        with patch.object(ex, "hermes_chat_review") as mock_kimi:
            mock_kimi.return_value = type(
                "P", (), {"stdout": "not json", "stderr": ""}
            )()
            with patch.object(ex, "resolve_cursor_agent_binary", return_value=None):
                executor._phase_reviewing(
                    conn, task_id, run.id, meta, ex.pipeline_state(meta)
                )

    assert "review_unavailable" in _dev_block_kinds(conn, task_id)
    conn.close()


def test_secret_in_diff_blocks(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    meta = {
        "dev_pipeline": {
            "contract": {"task_summary": "x"},
            "repo_path": str(repo),
            "dev_branch": "hermes-dev/t1",
            "base_commit": "a",
            "candidate_commit": "b",
            "attempt_history": [],
            "verification": {},
            "reviews": {},
            "logs_root": str(tmp_path / "logs"),
        }
    }
    ok, _msg, kind = ex.publish_pr(
        task_id="t1",
        task_text="task",
        contract={"task_summary": "x"},
        repo_dir=repo,
        branch="hermes-dev/t1",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="token ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        gh_fn=lambda *_a, **_k: ex.run_subprocess(["true"]),
        git_fn=lambda *_a, **_k: type(
            "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    assert ok is False
    assert kind == "secret_in_diff"
