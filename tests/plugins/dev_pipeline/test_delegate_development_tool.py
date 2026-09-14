"""Tests for delegate_development tool."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from plugins.dev_pipeline.pipeline import compute_idempotency_key
from plugins.dev_pipeline import tool as dpt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _parse_result(raw: str) -> dict:
    return json.loads(raw)


def test_creates_task_with_correct_fields(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub") as mock_sub:
        raw = dpt.delegate_development(
            repo=str(git_repo),
            task="Add widget",
            branch="main",
        )
        mock_sub.assert_called_once()

    result = _parse_result(raw)
    assert result["success"] is True
    assert result["deduplicated"] is False
    assert result["board"] == "dev"

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        assert task is not None
        assert task.workspace_kind == "scratch"
        assert task.max_retries == 2
        assert task.idempotency_key == compute_idempotency_key(
            str(git_repo), "main", "Add widget"
        )
        body = json.loads(task.body)
        assert body["repo"] == str(git_repo)
        assert body["branch"] == "main"
        assert body["task"] == "Add widget"
        assert "submitted_at" in body
    finally:
        conn.close()


def test_dedup_returns_existing_open_task(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        first = _parse_result(
            dpt.delegate_development(repo=str(git_repo), task="Add widget")
        )
        second = _parse_result(
            dpt.delegate_development(repo=str(git_repo), task="Add widget")
        )

    assert first["success"] is True
    assert second["success"] is True
    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]


def test_completed_task_does_not_block_resubmit(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    idem = compute_idempotency_key(str(git_repo), "main", "Add widget")

    kb.create_board("dev")
    conn = kb.connect(board="dev")
    try:
        old_id = kb.create_task(
            conn,
            title="old",
            body="{}",
            workspace_kind="scratch",
            idempotency_key=idem,
            board="dev",
        )
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?",
            (old_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with patch.object(dpt, "_maybe_register_notify_sub"):
        result = _parse_result(
            dpt.delegate_development(repo=str(git_repo), task="Add widget")
        )

    assert result["success"] is True
    assert result["deduplicated"] is False
    assert result["task_id"] != old_id


def test_invalid_repo_rejected(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    raw = dpt.delegate_development(
        repo=str(tmp_path / "missing"),
        task="nope",
    )
    result = _parse_result(raw)
    assert result["success"] is False
    assert result["task_id"] is None


def test_relative_local_repo_rejected(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    import os

    rel = os.path.relpath(str(git_repo), start=os.getcwd())
    raw = dpt.delegate_development(repo=rel, task="nope")
    result = _parse_result(raw)
    assert result["success"] is False
    assert "absolute" in (result.get("message") or "").lower()


def test_check_fn_reflects_cursor_binary(monkeypatch):
    monkeypatch.setattr(
        "plugins.dev_pipeline.tool.check_cursor_agent_requirements",
        lambda: False,
    )
    assert dpt.check_dev_pipeline_requirements() is False

    monkeypatch.setattr(
        "plugins.dev_pipeline.tool.check_cursor_agent_requirements",
        lambda: True,
    )
    assert dpt.check_dev_pipeline_requirements() is True


def test_notify_sub_registration_is_best_effort(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)

    with patch.object(dpt.kb, "add_notify_sub", side_effect=RuntimeError("boom")):
        raw = dpt.delegate_development(repo=str(git_repo), task="Add widget")

    result = _parse_result(raw)
    assert result["success"] is True


def test_plan_mode_none_defaults_to_consult_body(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        raw = dpt.delegate_development(
            repo=str(git_repo),
            task="Add widget",
            plan_mode=None,
        )
    result = _parse_result(raw)
    assert result["success"] is True

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        body = json.loads(task.body)
        assert "plan_mode" not in body
    finally:
        conn.close()


def test_plan_mode_debate_normalized_and_stored(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        raw = dpt.delegate_development(
            repo=str(git_repo),
            task="Big refactor",
            plan_mode="DEBATE",
        )
    result = _parse_result(raw)
    assert result["success"] is True

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        body = json.loads(task.body)
        assert body["plan_mode"] == "debate"
    finally:
        conn.close()


def test_plan_mode_garbage_rejected(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    raw = dpt.delegate_development(
        repo=str(git_repo),
        task="Add widget",
        plan_mode="roundtable",
    )
    result = _parse_result(raw)
    assert result["success"] is False
    assert result["message"] == "plan_mode must be 'consult' or 'debate'"


def test_open_pr_missing_omits_key_from_body(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        raw = dpt.delegate_development(repo=str(git_repo), task="Add widget")
    result = _parse_result(raw)
    assert result["success"] is True

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        body = json.loads(task.body)
        assert "open_pr" not in body
    finally:
        conn.close()


def test_open_pr_true_omits_key_from_body(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        raw = dpt.delegate_development(
            repo=str(git_repo), task="Add widget", open_pr=True
        )
    result = _parse_result(raw)
    assert result["success"] is True

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        body = json.loads(task.body)
        assert "open_pr" not in body
    finally:
        conn.close()


def test_open_pr_false_stored_in_body(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    with patch.object(dpt, "_maybe_register_notify_sub"):
        raw = dpt.delegate_development(
            repo=str(git_repo), task="Add widget", open_pr=False
        )
    result = _parse_result(raw)
    assert result["success"] is True

    conn = kb.connect(board="dev")
    try:
        task = kb.get_task(conn, result["task_id"])
        body = json.loads(task.body)
        assert body["open_pr"] is False
    finally:
        conn.close()


def test_open_pr_non_boolean_rejected(kanban_home, git_repo, monkeypatch):
    monkeypatch.setattr(dpt, "check_dev_pipeline_requirements", lambda: True)
    raw = dpt.delegate_development(
        repo=str(git_repo), task="Add widget", open_pr="yes"
    )
    result = _parse_result(raw)
    assert result["success"] is False
    assert result["message"] == "open_pr must be a boolean"


def test_parked_tool_not_registered_via_plugin_discovery(tmp_path, monkeypatch):
    """PARKED 2026-08-28 — delegate_development is not a live tool.

    Real loader, real manifest: the plugin still loads (status tool, executor
    hook), but the delegate_development registration stays commented out in
    ``plugins/dev_pipeline/__init__.py`` until the user unparks it.
    """
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_root / "plugins"))
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()

    assert "delegate_development" not in mgr._plugin_tool_names
    assert "dev_pipeline_status" in mgr._plugin_tool_names


class TestRepoUrlCredentialGuard:
    def test_https_url_with_userinfo_rejected(self):
        from plugins.dev_pipeline.pipeline import validate_repo_input

        ok, err = validate_repo_input("https://user:secret-token@github.com/org/repo.git")
        assert not ok
        assert "credentials" in err

    def test_https_url_with_bare_username_rejected(self):
        from plugins.dev_pipeline.pipeline import validate_repo_input

        ok, err = validate_repo_input("https://token@github.com/org/repo.git")
        assert not ok

    def test_plain_https_url_accepted(self):
        from plugins.dev_pipeline.pipeline import validate_repo_input

        ok, err = validate_repo_input("https://github.com/org/repo.git")
        assert ok, err
