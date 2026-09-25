"""Heartbeat daemon scope tests."""

from __future__ import annotations

import json
import threading
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


def _executor_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
        "heartbeat_interval_seconds": 0.05,
    }
    cfg.update(overrides)
    return cfg


def test_heartbeat_scope_fires_during_blocking_verify(kanban_home, tmp_path):
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

    block_started = threading.Event()
    block_release = threading.Event()
    heartbeat_during_block = threading.Event()
    heartbeat_calls: list[tuple] = []
    real_heartbeat = kb.heartbeat_claim

    def track_heartbeat(hb_conn, tid, **kwargs):
        heartbeat_calls.append((tid, kwargs))
        if block_started.is_set() and not block_release.is_set():
            heartbeat_during_block.set()
        return real_heartbeat(hb_conn, tid, **kwargs)

    fail = ex.CommandResult(
        command="pytest",
        exit_code=0,
        output_path=Path("/tmp/log"),
    )

    def blocking_verification(*_args, **_kwargs):
        block_started.set()
        assert heartbeat_during_block.wait(timeout=2.0)
        block_release.set()
        return [fail]

    with patch.object(ex, "git_command"):
        with patch.object(ex, "git_head_sha", return_value="bbb"):
            with patch.object(
                ex, "run_verification", side_effect=blocking_verification
            ):
                with patch.object(kb, "heartbeat_claim", side_effect=track_heartbeat):
                    executor._phase_verifying(
                        conn,
                        task_id,
                        pipeline_run,
                        meta,
                        ex.pipeline_state(meta),
                    )

    assert block_started.is_set()
    assert block_release.is_set()
    assert heartbeat_calls
    assert not any(
        t.name.startswith("dev-hb-") and t.is_alive() for t in threading.enumerate()
    )
    conn.close()
