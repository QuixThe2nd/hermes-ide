"""Progress extraction tests for the RUNNING-phase JSONL tail.

``coarse_progress_from_events`` is the anti-spam filter for dev-job chat
updates: it must surface real file/command activity as English-sentence
fuel and stay empty for everything else — especially the stream heartbeats
that used to render as ``RUNNING → RUNNING``.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex


def _claude_tool_use(name: str, tool_input: dict) -> dict:
    """Claude Code / Cursor stream-json assistant turn with one tool_use."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": name,
                    "input": tool_input,
                }
            ],
        },
    }


def test_extracts_repo_relative_file_path_from_edit():
    events = [
        _claude_tool_use(
            "Edit",
            {"file_path": "/ws/repo/plugins/x.py", "old_string": "a", "new_string": "b"},
        )
    ]
    assert ex.coarse_progress_from_events(events, repo_root="/ws/repo") == [
        {"kind": "file_edited", "detail": "plugins/x.py"}
    ]


def test_extracts_real_command_from_bash():
    events = [_claude_tool_use("Bash", {"command": "pytest -q tests/", "description": "run tests"})]
    assert ex.coarse_progress_from_events(events) == [
        {"kind": "command", "detail": "pytest -q tests/"}
    ]


def test_command_list_input_is_joined():
    events = [_claude_tool_use("run_terminal_command", {"command": ["make", "build"]})]
    assert ex.coarse_progress_from_events(events) == [
        {"kind": "command", "detail": "make build"}
    ]


def test_flat_tool_call_event_shape_supported():
    events = [{"type": "tool_call", "tool": "bash", "input": {"command": "make build"}}]
    assert ex.coarse_progress_from_events(events) == [
        {"kind": "command", "detail": "make build"}
    ]


def test_read_tool_counts_as_file_activity():
    events = [_claude_tool_use("Read", {"file_path": "/ws/repo/README.md"})]
    assert ex.coarse_progress_from_events(events, repo_root="/ws/repo") == [
        {"kind": "file_edited", "detail": "README.md"}
    ]


def test_non_file_non_command_tools_are_skipped():
    events = [
        _claude_tool_use("Grep", {"pattern": "stream_activity", "path": "/ws/repo/plugins"}),
        _claude_tool_use("TodoWrite", {"todos": []}),
    ]
    assert ex.coarse_progress_from_events(events, repo_root="/ws/repo") == []


def test_noise_events_produce_nothing_and_no_stream_activity():
    """Text turns, tool results, usage rows, system lines → silence.

    The old implementation fell back to a ``stream_activity`` item for any
    non-empty batch — the direct cause of the RUNNING → RUNNING flood.
    """
    events = [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    ]
    assert ex.coarse_progress_from_events(events) == []


def test_duplicate_kind_detail_pairs_collapse():
    events = [
        _claude_tool_use("Edit", {"file_path": "/r/a.py"}),
        _claude_tool_use("Read", {"file_path": "/r/a.py"}),
        _claude_tool_use("Bash", {"command": "pytest"}),
        _claude_tool_use("Bash", {"command": "pytest"}),
    ]
    assert ex.coarse_progress_from_events(events, repo_root="/r") == [
        {"kind": "file_edited", "detail": "a.py"},
        {"kind": "command", "detail": "pytest"},
    ]


def test_checkpoint_event_maps_to_checkpoint_kind():
    events = [{"type": "checkpoint", "message": "attempt budget reset"}]
    assert ex.coarse_progress_from_events(events) == [
        {"kind": "checkpoint", "detail": "attempt budget reset"}
    ]


def test_detail_is_sanitized_for_chat_sentences():
    events = [_claude_tool_use("Bash", {"command": "echo `whoami`\nrm -rf /tmp/x"})]
    items = ex.coarse_progress_from_events(events)
    assert items == [{"kind": "command", "detail": "echo 'whoami'"}]


def test_long_detail_is_truncated():
    events = [_claude_tool_use("Bash", {"command": "x" * 200})]
    detail = ex.coarse_progress_from_events(events)[0]["detail"]
    assert len(detail) <= 80
    assert detail.endswith("…")


# ---------------------------------------------------------------------------
# _phase_running integration: silence without activity, dedup across ticks
# ---------------------------------------------------------------------------


def _dev_phase_payloads(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dev_phase'"
        " ORDER BY id",
        (task_id,),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


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


def _running_task(tmp_path):
    from hermes_cli import kanban_db as kb

    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn, title="t", body="{}", workspace_kind="scratch", board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    jsonl = logs / f"attempt-{run.id}.jsonl"
    jsonl.write_text("", encoding="utf-8")
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
    return conn, task_id, run, jsonl, meta


def _prime_active(executor, task_id, run, jsonl):
    """Register the in-memory ActiveTask before JSONL lines appear.

    Mirrors the live flow: the executor registers the task when the unit
    starts (file empty), then each tick tails whatever grew since
    ``last_jsonl_size``.
    """
    active = ex.ActiveTask(task_id, run.id, ex.PHASE_RUNNING)
    active.last_jsonl_size = jsonl.stat().st_size
    active.last_jsonl_growth_at = time.time()
    executor._active[task_id] = active


def _tick_running(executor, conn, task_id, run, meta):
    with patch.object(executor, "_is_active", return_value=(True, "active")):
        executor._phase_running(
            conn, task_id, run.id, meta, ex.pipeline_state(meta)
        )


def _make_executor():
    return ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })


def test_phase_running_records_nothing_without_tool_activity(kanban_home, tmp_path):
    conn, task_id, run, jsonl, meta = _running_task(tmp_path)
    executor = _make_executor()
    _prime_active(executor, task_id, run, jsonl)
    with jsonl.open("a", encoding="utf-8") as fh:
        for ev in (
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hm"}]}},
            {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ):
            fh.write(json.dumps(ev) + "\n")

    _tick_running(executor, conn, task_id, run, meta)

    assert _dev_phase_payloads(conn, task_id) == []
    conn.close()


def test_phase_running_records_progress_then_dedupes_across_ticks(
    kanban_home, tmp_path,
):
    conn, task_id, run, jsonl, meta = _running_task(tmp_path)
    executor = _make_executor()
    _prime_active(executor, task_id, run, jsonl)
    edit = _claude_tool_use(
        "Edit", {"file_path": f"{tmp_path}/repo/plugins/dev_pipeline/executor.py"}
    )
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(edit) + "\n")

    _tick_running(executor, conn, task_id, run, meta)
    payloads = _dev_phase_payloads(conn, task_id)
    assert payloads == [
        {
            "phase": ex.PHASE_RUNNING,
            "kind": "file_edited",
            "detail": "plugins/dev_pipeline/executor.py",
        }
    ]

    # Next tick tails the SAME edit again — a re-Read of an already-reported
    # file must not re-fire.
    meta = ex.load_run_metadata(conn, run.id)
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(edit) + "\n")
    _tick_running(executor, conn, task_id, run, meta)
    assert _dev_phase_payloads(conn, task_id) == payloads

    saved = ex.load_run_metadata(conn, run.id)
    assert "plugins/dev_pipeline/executor.py" in str(
        ex.pipeline_state(saved).get("progress_seen")
    )
    conn.close()
