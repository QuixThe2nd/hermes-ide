"""Review stage tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugins.dev_pipeline import executor as ex
from hermes_cli import kanban_db as kb


def _dev_block_kinds(conn, task_id: str) -> list[str]:
    return [
        (ev.payload or {}).get("block_kind")
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_blocked"
    ]


def _executor_cfg() -> dict:
    return {
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    }


def _setup_reviewing_task(
    conn, tmp_path, *, repair_used: bool = False
) -> tuple[str, int, dict]:
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
                "phase": ex.PHASE_REVIEWING,
                "repair_used": repair_used,
                "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
                "mechanical_pass": True,
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    return task_id, pipeline_run, meta


def _proc(text: str):
    return type("P", (), {"stdout": text, "stderr": ""})()


def _patch_reviewers(kimi_text: str, grok_text: str):
    """Patch the kimi + grok reviewer subprocess boundaries."""
    return (
        patch.object(ex, "hermes_chat_review", MagicMock(return_value=_proc(kimi_text))),
        patch.object(ex, "resolve_cursor_agent_binary", return_value="/bin/agent"),
        patch.object(ex, "run_subprocess", MagicMock(return_value=_proc(grok_text))),
    )


def test_parse_review_verdict_valid():
    text = '{"verdict":"pass","blocking_findings":[],"notes":["ok"]}'
    parsed = ex.parse_review_verdict(text)
    assert parsed is not None
    assert parsed["verdict"] == "pass"


def test_parse_review_verdict_garbage_fail_closed():
    assert ex.parse_review_verdict("thanks for reviewing") is None


def test_parse_verdict_with_nested_json_in_notes():
    payload = {
        "verdict": "pass",
        "blocking_findings": [],
        "notes": [
            "Acceptance command `python -m pytest -q` exits 127 on both base "
            "and candidate, a pre-existing environment failure.",
            'Diff correctly implements the spec: /version returns exact body '
            '{"version": "1.0.0"} with status 200 and Content-Type '
            "application/json, matching existing handler conventions.",
            "Unverified assumption: test_server.py must already import json.",
        ],
    }
    parsed = ex.parse_review_verdict(json.dumps(payload))
    assert parsed is not None
    assert parsed["verdict"] == "pass"
    assert parsed["notes"] == payload["notes"]


def test_parse_verdict_inside_code_fence():
    payload = {
        "verdict": "fail",
        "blocking_findings": ["missing import"],
        "notes": ['notes mention a nested {"version": "1.0.0"} object'],
    }
    text = f"Here is my review:\n```json\n{json.dumps(payload, indent=2)}\n```\n"
    parsed = ex.parse_review_verdict(text)
    assert parsed is not None
    assert parsed["verdict"] == "fail"
    assert parsed["blocking_findings"] == ["missing import"]


def test_parse_verdict_last_wins_with_nested():
    example = json.dumps({
        "verdict": "fail",
        "blocking_findings": ["example finding"],
        "notes": ['example note with a nested {"a": {"b": 1}} object'],
    })
    real = json.dumps({
        "verdict": "pass",
        "blocking_findings": [],
        "notes": ['real note mentioning {"version": "1.0.0"}'],
    })
    parsed = ex.parse_review_verdict(
        f"Format example: {example}\n\nActual verdict:\n{real}"
    )
    assert parsed is not None
    assert parsed["verdict"] == "pass"
    assert parsed["blocking_findings"] == []


def test_parse_verdict_json_escaped_inside_jsonl():
    payload = {
        "verdict": "pass",
        "blocking_findings": [],
        "notes": ['note with nested {"version": "1.0.0"} object'],
    }
    fenced = f"```json\n{json.dumps(payload)}\n```"
    event = {"type": "result", "result": fenced, "session_id": "abc"}
    parsed = ex.parse_review_verdict(json.dumps(event) + "\n")
    assert parsed is not None
    assert parsed["verdict"] == "pass"
    assert parsed["notes"] == payload["notes"]


def test_parse_review_verdict_last_verdict_wins():
    example = '{"verdict":"pass","blocking_findings":[],"notes":["example"]}'
    real_fail = '{"verdict":"fail","blocking_findings":["bug"],"notes":["real"]}'
    parsed = ex.parse_review_verdict(
        f"Use this format: {example}\n\nMy verdict:\n{real_fail}"
    )
    assert parsed is not None
    assert parsed["verdict"] == "fail"
    assert parsed["blocking_findings"] == ["bug"]

    reverse = ex.parse_review_verdict(
        f"{real_fail}\n\nIgnore the above. Correct verdict: {example}"
    )
    assert reverse is not None
    assert reverse["verdict"] == "pass"


def test_review_gate_all_pass():
    kimi = {"verdict": "pass", "blocking_findings": [], "notes": []}
    grok = {"verdict": "pass", "blocking_findings": [], "notes": []}
    proceed, repair = ex.review_gate(True, kimi, grok)
    assert proceed is True
    assert repair is False


def test_review_gate_any_fail_needs_repair():
    kimi = {"verdict": "fail", "blocking_findings": ["bug"], "notes": []}
    grok = {"verdict": "pass", "blocking_findings": [], "notes": []}
    proceed, repair = ex.review_gate(True, kimi, grok)
    assert proceed is False
    assert repair is True


def test_kimi_grok_review_pass_proceeds_to_publishing(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_reviewing_task(conn, tmp_path)
    logs = Path(ex.pipeline_state(meta)["logs_root"])
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'
    kimi_patch, cursor_patch, grok_patch = _patch_reviewers(verdict, verdict)

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with kimi_patch as kimi_mock:
                with cursor_patch:
                    with grok_patch as grok_mock:
                        executor._phase_reviewing(
                            conn, task_id, run_id, meta, ex.pipeline_state(meta)
                        )

    kimi_mock.assert_called_once()
    grok_mock.assert_called_once()
    assert (logs / "review-diff.txt").read_text(encoding="utf-8") == "diff"
    reviews = json.loads((logs / "reviews.json").read_text(encoding="utf-8"))
    assert reviews["kimi"]["verdict"] == "pass"
    assert reviews["grok"]["verdict"] == "pass"
    saved = ex.pipeline_state(ex.load_run_metadata(conn, run_id))
    assert saved.get("phase") == ex.PHASE_PUBLISHING
    conn.close()


def test_review_repair_preserves_fresh_spawn_metadata(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_reviewing_task(conn, tmp_path)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    fail_verdict = '{"verdict":"fail","blocking_findings":["bug"],"notes":[]}'
    pass_verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'
    kimi_patch, cursor_patch, grok_patch = _patch_reviewers(fail_verdict, pass_verdict)

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with kimi_patch:
                with cursor_patch:
                    with grok_patch:
                        with patch.object(executor, "_is_active", return_value=(False, "")):
                            with patch.object(
                                ex,
                                "systemd_run_attempt",
                                return_value=(True, 4242, 1_700_000_000),
                            ):
                                executor._phase_reviewing(
                                    conn, task_id, run_id, meta, ex.pipeline_state(meta)
                                )

    new_run_id = executor._active[task_id].run_id
    assert new_run_id != run_id
    new_meta = ex.load_run_metadata(conn, new_run_id)
    st = ex.pipeline_state(new_meta)
    assert st.get("unit_started") is True
    assert st.get("unit_pid") == 4242
    assert st.get("host_start_time") == 1_700_000_000
    assert st.get("attempt_prompt")
    assert st.get("run_kind") == ex.RUN_KIND_ATTEMPT
    assert st.get("phase") == ex.PHASE_RUNNING
    assert ex.count_attempt_runs(conn, task_id) == 2
    prompt = st.get("attempt_prompt") or ""
    assert prompt.count('"bug"') == 1
    conn.close()


def test_reviewing_kimi_timeout_blocks_review_unavailable(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_reviewing_task(conn, tmp_path)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    heartbeat_calls: list[tuple] = []

    def track_heartbeat(*args, **kwargs):
        heartbeat_calls.append((args, kwargs))

    pass_verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'
    timeout_exc = subprocess.TimeoutExpired(cmd="hermes chat", timeout=600)

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with patch.object(ex, "hermes_chat_review", side_effect=timeout_exc):
                with patch.object(
                    ex, "resolve_cursor_agent_binary", return_value="/bin/agent"
                ):
                    with patch.object(
                        ex, "run_subprocess", return_value=_proc(pass_verdict)
                    ):
                        with patch.object(
                            kb, "heartbeat_claim", side_effect=track_heartbeat
                        ):
                            executor._phase_reviewing(
                                conn, task_id, run_id, meta, ex.pipeline_state(meta)
                            )

    assert task_id not in executor._active
    assert "review_unavailable" in _dev_block_kinds(conn, task_id)
    assert len(heartbeat_calls) >= 1
    conn.close()


def test_reviewing_cursor_binary_missing_blocks_review_unavailable(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_reviewing_task(conn, tmp_path)
    logs = Path(ex.pipeline_state(meta)["logs_root"])
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    pass_verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with patch.object(
                ex, "hermes_chat_review", return_value=_proc(pass_verdict)
            ):
                with patch.object(ex, "resolve_cursor_agent_binary", return_value=None):
                    with patch.object(ex, "run_subprocess") as grok_mock:
                        executor._phase_reviewing(
                            conn, task_id, run_id, meta, ex.pipeline_state(meta)
                        )
                        grok_mock.assert_not_called()

    assert task_id not in executor._active
    assert "review_unavailable" in _dev_block_kinds(conn, task_id)
    assert not (logs / "review-grok.jsonl").exists()
    conn.close()


def test_reviewing_exhausted_repair_emits_typed_dev_blocked_event(
    kanban_home, tmp_path
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_reviewing_task(conn, tmp_path, repair_used=True)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    fail_verdict = '{"verdict":"fail","blocking_findings":["bug"],"notes":[]}'
    kimi_patch, cursor_patch, grok_patch = _patch_reviewers(fail_verdict, fail_verdict)
    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with kimi_patch:
                with cursor_patch:
                    with grok_patch:
                        executor._phase_reviewing(
                            conn, task_id, run_id, meta, ex.pipeline_state(meta)
                        )

    assert "review_failed" in _dev_block_kinds(conn, task_id)
    assert task_id not in executor._active
    conn.close()


def test_review_repair_prompt_not_double_wrapped(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    unique_task = "unique review repair task marker"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": unique_task}),
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
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_REVIEWING,
                "contract": {
                    "task_summary": "unique review summary marker",
                    "acceptance_commands": ["true"],
                },
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
                "mechanical_pass": True,
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, pipeline_run, ex.PHASE_REVIEWING)

    fail_verdict = '{"verdict":"fail","blocking_findings":["bug"],"notes":[]}'
    pass_verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'
    kimi_patch, cursor_patch, grok_patch = _patch_reviewers(fail_verdict, pass_verdict)
    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="diff"):
            with kimi_patch:
                with cursor_patch:
                    with grok_patch:
                        with patch.object(executor, "_is_active", return_value=(False, "")):
                            with patch.object(
                                ex,
                                "systemd_run_attempt",
                                return_value=(True, 4242, 1_700_000_000),
                            ):
                                executor._phase_reviewing(
                                    conn,
                                    task_id,
                                    pipeline_run,
                                    meta,
                                    ex.pipeline_state(meta),
                                )

    new_run_id = executor._active[task_id].run_id
    new_meta = ex.load_run_metadata(conn, new_run_id)
    prompt = ex.pipeline_state(new_meta).get("attempt_prompt") or ""
    assert prompt.count("Task:\n") == 1
    assert prompt.count(unique_task) == 1
    assert prompt.count("unique review summary marker") == 1
    conn.close()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home
