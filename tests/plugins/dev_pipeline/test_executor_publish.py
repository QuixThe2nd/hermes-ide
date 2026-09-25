"""PR idempotency and publish body tests."""

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


def test_find_existing_pr_returns_number(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    gh = MagicMock(
        return_value=type(
            "P", (), {"returncode": 0, "stdout": '[{"number": 42}]', "stderr": ""}
        )()
    )
    assert ex.find_existing_pr("hermes-dev/t1", repo_dir=repo, gh_fn=gh) == 42
    gh.assert_called_once()
    _args, kwargs = gh.call_args
    assert kwargs.get("cwd") == repo


def test_publish_pr_all_gh_calls_pass_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    recorded: list[tuple[list[str], dict]] = []
    list_calls = {"count": 0}

    def gh(args, **kwargs):
        recorded.append((list(args), dict(kwargs)))
        if args[0:2] == ["pr", "list"]:
            list_calls["count"] += 1
            if list_calls["count"] == 1:
                return type(
                    "P",
                    (),
                    {"returncode": 0, "stdout": '[{"number": 7}]', "stderr": ""},
                )()
            return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if args[0:2] == ["pr", "comment"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[0:2] == ["pr", "view"]:
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"url":"https://example/pr/7"}',
                    "stderr": "",
                },
            )()
        if args[0:2] == ["pr", "create"]:
            return type(
                "P",
                (),
                {"returncode": 0, "stdout": "https://example/pr/new", "stderr": ""},
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    git_ok = lambda *_a, **_k: type(
        "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
    )()
    common = dict(
        task_id="t1",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t1",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=git_ok,
    )

    ok, _url, kind = ex.publish_pr(**common)
    assert ok is True
    assert kind == ""
    existing_calls = list(recorded)
    assert any(a[0:2] == ["pr", "comment"] for a, _k in existing_calls)
    assert not any(a[0:2] == ["pr", "create"] for a, _k in existing_calls)
    recorded.clear()

    ok, _url, kind = ex.publish_pr(**common)
    assert ok is True
    create_calls = list(recorded)
    assert any(a[0:2] == ["pr", "create"] for a, _k in create_calls)
    assert not any(a[0:2] == ["pr", "comment"] for a, _k in create_calls)

    for _args, kwargs in existing_calls + create_calls:
        assert kwargs.get("cwd") == repo


def test_find_existing_pr_returns_number_legacy():
    gh = MagicMock(
        return_value=type(
            "P", (), {"returncode": 0, "stdout": '[{"number": 42}]', "stderr": ""}
        )()
    )
    assert ex.find_existing_pr("hermes-dev/t1", gh_fn=gh) == 42


def test_publish_pr_existing_comments_instead_of_create(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def gh(args, **kwargs):
        calls.append(list(args))
        if args[0:2] == ["pr", "list"]:
            return type(
                "P", (), {"returncode": 0, "stdout": '[{"number": 7}]', "stderr": ""}
            )()
        if args[0:2] == ["pr", "comment"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[0:2] == ["pr", "view"]:
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"url":"https://example/pr/7"}',
                    "stderr": "",
                },
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    ok, url, kind = ex.publish_pr(
        task_id="t1",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t1",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=lambda *_a, **_k: type(
            "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    assert ok is True
    assert "7" in url or "example" in url
    assert any(c[0:2] == ["pr", "comment"] for c in calls)
    assert not any(c[0:2] == ["pr", "create"] for c in calls)


def test_publish_pr_creates_when_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def gh(args, **kwargs):
        calls.append(list(args))
        if args[0:2] == ["pr", "list"]:
            return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if args[0:2] == ["pr", "create"]:
            return type(
                "P",
                (),
                {"returncode": 0, "stdout": "https://example/pr/new", "stderr": ""},
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    ok, url, kind = ex.publish_pr(
        task_id="t99",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t99",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=lambda *_a, **_k: type(
            "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    assert ok is True
    assert any(c[0:2] == ["pr", "create"] for c in calls)


def test_pr_body_contains_job_marker():
    body = ex.build_pr_body(
        task_id="t1",
        task_text="do work",
        contract={"task_summary": "s"},
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
    )
    assert "<!-- hermes-dev-job:t1 -->" in body


def _setup_publishing_task(conn, tmp_path) -> tuple[str, int, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "ship it", "open_pr": False}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_PUBLISHING,
                "contract": {"task_summary": "x"},
                "repo_path": str(repo),
                "logs_root": str(logs),
                "dev_branch": "hermes-dev/t1",
                "base_commit": "aaa",
                "candidate_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    return task_id, pipeline_run, meta


def test_publishing_open_pr_false_skips_publish_pr(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_publishing_task(conn, tmp_path)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_PUBLISHING)

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "unified_diff", return_value="safe diff"):
            with patch.object(
                ex, "publish_pr", side_effect=AssertionError("publish_pr called")
            ):
                executor._phase_publishing(
                    conn, task_id, run_id, meta, ex.pipeline_state(meta)
                )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "done"
    assert "open_pr=false" in (task.result or "")
    phases = [
        ev.payload
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_phase"
    ]
    assert any(p.get("pr_skipped") is True for p in phases if isinstance(p, dict))
    assert task_id not in executor._active
    conn.close()
