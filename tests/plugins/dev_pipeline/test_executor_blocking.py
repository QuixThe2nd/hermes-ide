"""Post-attempt blocking and writer-fencing tests."""

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


def _setup_pipeline_task(
    conn, tmp_path, *, phase: str = ex.PHASE_REVIEWING
) -> tuple[str, int]:
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement x"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt done",
        metadata={"dev_pipeline": {"candidate_commit": "bbb"}},
    )
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": phase,
                "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
                "mechanical_pass": True,
            },
        ),
    )
    return task_id, pipeline_run


def test_review_unavailable_blocks_after_attempt_end(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path)
    meta = ex.load_run_metadata(conn, run_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    with patch.object(ex, "unified_diff", return_value="safe diff"):
        with patch.object(ex, "hermes_chat_review") as mock_kimi:
            mock_kimi.return_value = type(
                "P", (), {"stdout": "garbage", "stderr": ""}
            )()
            with patch.object(ex, "resolve_cursor_agent_binary", return_value=None):
                executor._phase_reviewing(
                    conn, task_id, run_id, meta, ex.pipeline_state(meta)
                )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    conn.close()


def test_secret_in_diff_blocks_after_attempt_end(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path, phase=ex.PHASE_PUBLISHING)
    meta = ex.load_run_metadata(conn, run_id)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_PUBLISHING)

    secret_diff = "+token ghp_abcdefghijklmnopqrstuvwxyz1234567890\n"
    with patch.object(
        ex, "publish_pr", return_value=(False, "findings", "secret_in_diff")
    ):
        executor._phase_publishing(conn, task_id, run_id, meta, ex.pipeline_state(meta))

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    conn.close()


def test_reviewing_secret_scan_before_writing_artifacts(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path)
    meta = ex.load_run_metadata(conn, run_id)
    logs = Path(ex.pipeline_state(meta)["logs_root"])
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    with patch.object(
        ex, "unified_diff", return_value="+ghp_secretleak123456789012345678901234\n"
    ):
        with patch.object(ex, "hermes_chat_review") as mock_kimi:
            executor._phase_reviewing(
                conn, task_id, run_id, meta, ex.pipeline_state(meta)
            )
            mock_kimi.assert_not_called()

    assert (logs / "secret-scan-quarantine.json").is_file()
    assert not (logs / "review-kimi.raw").exists()
    conn.close()


def test_spawn_refuses_when_other_task_unit_active(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body='{"task":"x"}',
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run1 = kb.latest_run(conn, task_id)
    run2 = ex.start_new_run(
        conn, task_id, metadata={"dev_pipeline": {"phase": "RUNNING"}}
    )
    meta = ex.load_run_metadata(conn, run2)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })

    active_unit = ex.unit_name(task_id, run1.id)

    def fake_active(unit):
        return unit == active_unit, "active"

    with patch.object(executor, "_is_active", side_effect=fake_active):
        with patch.object(ex, "systemd_run_attempt") as mock_run:
            executor._spawn_attempt(conn, task_id, run2, meta, ex.pipeline_state(meta))
            mock_run.assert_not_called()
    meta_after = ex.load_run_metadata(conn, run2)
    assert ex.pipeline_state(meta_after).get("spawn_pending") is True
    assert ex.pipeline_state(meta_after).get("unit_started") is False
    conn.close()


def test_refused_spawn_tick_does_not_classify_completed(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body='{"task":"x"}',
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run_id = kb.latest_run(conn, task_id).id
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "spawn_pending": True,
            "unit_started": False,
            "unit_name": ex.unit_name(task_id, run_id),
            "repo_path": str(repo),
            "logs_root": str(logs),
            "run_kind": ex.RUN_KIND_ATTEMPT,
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

    with patch.object(ex, "any_task_unit_active", return_value=(True, "foreign-unit")):
        with patch.object(ex, "systemd_run_attempt") as mock_systemd:
            with patch.object(executor, "_finish_attempt") as mock_finish:
                executor._phase_running(
                    conn, task_id, run_id, meta, ex.pipeline_state(meta)
                )
                mock_systemd.assert_not_called()
                mock_finish.assert_not_called()
    meta_after = ex.load_run_metadata(conn, run_id)
    assert ex.pipeline_state(meta_after).get("spawn_pending") is True
    assert ex.pipeline_state(meta_after).get("unit_started") is False
    conn.close()


def test_refused_repair_spawn_preserves_prompt_on_reentry(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run1 = kb.latest_run(conn, task_id)
    run2 = ex.start_new_run(
        conn, task_id, metadata={"dev_pipeline": {"phase": "RUNNING"}}
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "contract": {"task_summary": "summary"},
            "repo_path": str(repo),
            "logs_root": str(logs),
        },
    )
    ex.save_run_metadata(conn, run2, meta)
    meta = ex.load_run_metadata(conn, run2)

    repair_marker = "verification repair context marker xyz"
    repair_prompt = ex.build_attempt_prompt(
        "fix bug",
        {"task_summary": "summary"},
        repair_context=repair_marker,
    )

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run2, ex.PHASE_RUNNING)

    active_unit = ex.unit_name(task_id, run1.id)

    def fake_active(unit):
        return unit == active_unit, "active"

    with patch.object(executor, "_is_active", side_effect=fake_active):
        with patch.object(ex, "systemd_run_attempt") as mock_run:
            executor._spawn_attempt(
                conn,
                task_id,
                run2,
                meta,
                ex.pipeline_state(meta),
                prompt_override=repair_prompt,
            )
            mock_run.assert_not_called()

    meta_refused = ex.load_run_metadata(conn, run2)
    st_refused = ex.pipeline_state(meta_refused)
    assert st_refused.get("spawn_pending") is True
    assert repair_marker in (st_refused.get("attempt_prompt") or "")

    with patch.object(executor, "_is_active", return_value=(False, "")):
        with patch.object(ex, "any_task_unit_active", return_value=(False, "")):
            with patch.object(
                ex,
                "systemd_run_attempt",
                return_value=(True, 9999, 1_700_000_000),
            ):
                executor._phase_running(
                    conn,
                    task_id,
                    run2,
                    meta_refused,
                    ex.pipeline_state(meta_refused),
                )

    meta_respawn = ex.load_run_metadata(conn, run2)
    prompt = ex.pipeline_state(meta_respawn).get("attempt_prompt") or ""
    assert repair_marker in prompt
    conn.close()


def test_external_block_stops_all_task_units(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body='{"task":"x"}',
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run1 = kb.latest_run(conn, task_id)
    run2 = ex.start_new_run(
        conn, task_id, metadata={"dev_pipeline": {"phase": "RUNNING"}}
    )
    run3 = ex.start_new_run(
        conn, task_id, metadata={"dev_pipeline": {"phase": "RUNNING"}}
    )
    unit1 = ex.unit_name(task_id, run1.id)
    unit2 = ex.unit_name(task_id, run2)
    unit3 = ex.unit_name(task_id, run3)
    active_units = {unit1, unit2, unit3}

    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run3, ex.PHASE_RUNNING)
    stopped: list[str] = []

    def fake_active(unit):
        return unit in active_units, "active"

    def fake_stop(unit):
        stopped.append(unit)
        active_units.discard(unit)
        return True

    with patch.object(executor, "_is_active", side_effect=fake_active):
        with patch.object(executor, "_stop", side_effect=fake_stop):
            executor._handle_external_block(conn, task_id)

    assert set(stopped) == {unit1, unit2, unit3}
    conn.close()
