"""Tests for dev_pipeline_status tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from plugins.dev_pipeline import executor as ex
from plugins.dev_pipeline import tool as dpt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parse_result(raw: str) -> dict:
    return json.loads(raw)


def test_known_task_returns_phase_and_history(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    try:
        task_id = kb.create_task(
            conn,
            title="status probe",
            body=json.dumps({"repo": "/tmp/r", "branch": "main", "task": "x"}),
            workspace_kind="scratch",
            board="dev",
        )
        kb.claim_task(conn, task_id, claimer="dev-executor")
        run = kb.latest_run(conn, task_id)
        assert run is not None
        ex.save_run_metadata(
            conn,
            run.id,
            ex.merge_pipeline_state({}, {"phase": ex.PHASE_RUNNING, "run_kind": "attempt"}),
        )
        ex.record_dev_phase(conn, task_id, run.id, ex.PHASE_PLANNING)
        ex.record_dev_phase(conn, task_id, run.id, ex.PHASE_RUNNING)
    finally:
        conn.close()

    raw = dpt.dev_pipeline_status(task_id=task_id)
    result = _parse_result(raw)
    assert result["success"] is True
    assert result["task_id"] == task_id
    assert result["board"] == "dev"
    assert result["status"] == "running"
    assert result["phase"] == ex.PHASE_RUNNING
    assert len(result["phase_history"]) == 2
    assert result["phase_history"][0]["phase"] == ex.PHASE_PLANNING
    assert result["phase_history"][1]["phase"] == ex.PHASE_RUNNING
    assert result["logs_dir"].endswith(task_id)
    assert result["run_id"] == run.id


def test_unknown_task_returns_failure(kanban_home):
    raw = dpt.dev_pipeline_status(task_id="missing-task")
    result = _parse_result(raw)
    assert result["success"] is False
    assert "unknown task" in result["message"]


def test_list_mode_excludes_archived_tasks(kanban_home):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    try:
        active_id = kb.create_task(
            conn,
            title="active",
            body="{}",
            workspace_kind="scratch",
            board="dev",
        )
        archived_id = kb.create_task(
            conn,
            title="archived",
            body="{}",
            workspace_kind="scratch",
            board="dev",
        )
        kb.archive_task(conn, archived_id)
    finally:
        conn.close()

    result = _parse_result(dpt.dev_pipeline_status())
    assert result["success"] is True
    assert result["board"] == "dev"
    ids = {row["id"] for row in result["tasks"]}
    assert active_id in ids
    assert archived_id not in ids
    by_id = {row["id"]: row for row in result["tasks"]}
    assert by_id[active_id]["title"] == "active"
    assert by_id[active_id]["status"] == "ready"
    assert "created_at" in by_id[active_id]
