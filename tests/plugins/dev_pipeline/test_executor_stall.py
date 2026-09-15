"""Stall detection tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex


def test_detect_stall_no_jsonl_growth(tmp_path):
    jsonl = tmp_path / "attempt.jsonl"
    jsonl.write_text('{"type":"start"}\n', encoding="utf-8")
    size = jsonl.stat().st_size
    now = 1000.0
    stalled, new_size, growth_at = ex.detect_stall(
        unit_active=True,
        jsonl_path=jsonl,
        last_size=size,
        last_growth_at=now - 700,
        now=now,
        stall_seconds=600,
    )
    assert stalled is True
    assert new_size == size


def test_detect_stall_resets_on_growth(tmp_path):
    jsonl = tmp_path / "attempt.jsonl"
    jsonl.write_text("a\n", encoding="utf-8")
    first = jsonl.stat().st_size
    jsonl.write_text("a\nb\n", encoding="utf-8")
    second = jsonl.stat().st_size
    stalled, new_size, growth_at = ex.detect_stall(
        unit_active=True,
        jsonl_path=jsonl,
        last_size=first,
        last_growth_at=100.0,
        now=200.0,
    )
    assert stalled is False
    assert new_size == second


def test_running_phase_stall_stops_unit_and_classifies(kanban_home, tmp_path):
    from hermes_cli import kanban_db as kb

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
    logs = tmp_path / "logs"
    logs.mkdir()
    jsonl = logs / f"attempt-{run.id}.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "unit_started": True,
            "run_kind": ex.RUN_KIND_ATTEMPT,
            "unit_name": f"hermes-dev-{task_id}-{run.id}",
            "jsonl_path": str(jsonl),
            "repo_path": str(tmp_path / "repo"),
            "base_commit": "aaa",
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
    active = ex.ActiveTask(task_id, run.id, ex.PHASE_RUNNING)
    active.last_jsonl_size = jsonl.stat().st_size
    active.last_jsonl_growth_at = time.time() - 700
    executor._active[task_id] = active

    stop_calls = []

    with patch.object(executor, "_is_active", return_value=(True, "active")):
        with patch.object(
            executor, "_stop", side_effect=lambda u: stop_calls.append(u) or True
        ):
            with patch.object(executor, "_finish_attempt") as fin:
                executor._phase_running(
                    conn, task_id, run.id, meta, ex.pipeline_state(meta)
                )
                fin.assert_called_once()
                assert fin.call_args.kwargs.get("classification_hint") == "stalled"
    assert stop_calls
    conn.close()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from pathlib import Path

    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home
