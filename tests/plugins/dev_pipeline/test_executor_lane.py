"""Lane-aware prompt and attempt CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _contract() -> dict:
    return {
        "task_summary": "add feature",
        "lane_hint": "cursor",
        "estimated_minutes": 10,
        "allowed_paths": ["src"],
        "acceptance_commands": ["pytest -q"],
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


def test_cursor_prompt_delegates_to_subagents():
    prompt = ex.build_attempt_prompt("do work", _contract(), lane="cursor-bounded")
    assert "implementer" in prompt
    assert "reviewer" in prompt
    assert "composer" not in prompt.lower()
    assert "grok" not in prompt.lower()


def test_claude_prompt_direct_implementation_and_checkpoints():
    prompt = ex.build_attempt_prompt("do work", _contract(), lane="claude-endurance")
    assert "checkpoint" in prompt.lower()
    assert "do not push" in prompt.lower()
    assert "acceptance_commands" in prompt
    assert "implementer" not in prompt
    assert "reviewer" not in prompt
    assert "composer" not in prompt.lower()
    assert "grok" not in prompt.lower()
    assert "structured final summary" in prompt


def test_repair_prompt_threads_lane():
    results = [
        ex.CommandResult(
            command="pytest",
            exit_code=1,
            output_path=Path("/tmp/log"),
            output_preview="fail",
        )
    ]
    claude_repair = ex.build_repair_prompt(
        "fix", _contract(), results, "diff", lane="claude-endurance"
    )
    assert "checkpoint" in claude_repair.lower()
    assert "implementer" not in claude_repair


def test_routing_cursor_lane_persisted(kanban_home):
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
            "contract": _contract(),
        },
    )
    executor._phase_routing(conn, task_id, run.id, meta, ex.pipeline_state(meta))
    saved = ex.pipeline_state(ex.load_run_metadata(conn, run.id))
    assert saved.get("lane") == "cursor-bounded"
    conn.close()


def test_run_attempt_cli_claude_branch(kanban_home, tmp_path):
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
    logs.mkdir()
    meta = ex.merge_pipeline_state(
        {},
        {
            "repo_path": str(repo),
            "logs_root": str(logs),
            "attempt_prompt": "implement feature",
        },
    )
    ex.save_run_metadata(conn, run.id, meta)
    conn.close()

    with patch.object(ex, "resolve_claude_binary", return_value="/bin/claude-glm"):
        with patch(
            "plugins.dev_pipeline.executor.run_agent_cli",
            return_value=(None, str(logs / f"attempt-{run.id}.jsonl"), "", 0.0, 0),
        ) as run_cli:
            with pytest.raises(SystemExit) as exc:
                ex.run_attempt_cli(task_id, run.id, lane="claude-endurance")
            assert exc.value.code == 0

    run_cli.assert_called_once()
    cmd = run_cli.call_args[0][0]
    assert cmd[0] == "/bin/claude-glm"
    assert "-p" in cmd
    assert "--model" not in cmd  # wrapper pins the model itself
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--verbose" in cmd
    assert cmd[-1] == "implement feature"
