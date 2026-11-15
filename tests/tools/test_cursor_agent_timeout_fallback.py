"""Cloud run-poll timeout: workdir disk state reporting + opt-in local Claude fallback.

Regression coverage for the "Cursor Cloud Agent run-poll times out with zero
disk writes" papercut: on a wall-clock timeout the parent must learn the local
workdir's git state, and only an explicit ``delegation.cursor_timeout_fallback``
opt-in (with a clean workdir) may retry the task on the local Claude CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# This interpreter's venv lives under the real ~/.hermes, and pytest's
# assert-rewrite hook os.makedirs()es the venv __pycache__ on first exec of
# lazily imported modules (anyio via mcp.server.stdio during test setup) —
# tripping the conftest real-home guard. Disable bytecode writes for the
# rest of the process at collection time (equivalent to -B); cached pycs
# are still used, nothing is written.
sys.dont_write_bytecode = True

import pytest


def _delegate(task, workdir, **kwargs):
    kwargs.setdefault("session_id", "fallback-session")
    kwargs.setdefault("tool_call_id", "fallback-call")
    from tools import cursor_agent_tool
    return cursor_agent_tool.delegate_cursor_agent(task=task, workdir=workdir, **kwargs)


def _git(*args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _clean_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A committed repo with a bare origin, pushed — clean working tree."""
    repo = tmp_path / name
    repo.mkdir()
    bare = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.example", cwd=repo)
    _git("config", "user.name", "tester", cwd=repo)
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git("add", "keep.txt", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    return repo


def _install_cloud_timeout_path(monkeypatch, tmp_path, *, session_id, tool_call_id):
    """Wire the cloud happy path up to create, then make the poll time out."""
    from tools import cursor_agent_tool
    from tools.cursor_run_receipts import deterministic_client_agent_id

    secret = tmp_path / "cursor-cloud.env"
    secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(
        cursor_agent_tool, "resolve_workdir_origin", lambda workdir: "https://github.com/acme/demo"
    )
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main")
    monkeypatch.setattr(cursor_agent_tool, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cursor_agent_tool, "_emit_progress_notice", lambda message: None)
    client_id = deterministic_client_agent_id(session_id, tool_call_id)
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: (
            {"id": client_id, "name": "hermes-test", "latestRunId": "run-t"},
            {"id": "run-t", "agentId": client_id, "status": "CREATING"},
        ),
    )
    monkeypatch.setattr(
        cursor_agent_tool,
        "poll_cloud_run",
        lambda **kwargs: {
            "id": "run-t",
            "agentId": client_id,
            "status": "CANCELLED",
            "_local_error": "timeout",
            "_workdir_disk_state": "clean",
        },
    )
    return client_id


# --- _workdir_disk_state -----------------------------------------------------


def test_workdir_disk_state_clean_dirty_unknown(tmp_path):
    from tools.cursor_agent_tool import _workdir_disk_state

    repo = _clean_repo(tmp_path)
    assert _workdir_disk_state(str(repo)) == "clean"

    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    assert _workdir_disk_state(str(repo)) == "dirty"

    assert _workdir_disk_state(str(tmp_path / "does-not-exist")) == "unknown"


# --- poll_cloud_run timeout path --------------------------------------------


def test_poll_timeout_attaches_workdir_disk_state(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    cancelled: list[str] = []
    monkeypatch.setattr(cursor_agent_tool, "_http_request", lambda *a, **k: {"status": "RUNNING"})
    monkeypatch.setattr(cursor_agent_tool, "_check_interrupted", lambda: False)
    monkeypatch.setattr(
        cursor_agent_tool,
        "cancel_cloud_run",
        lambda agent_id, run_id, api_key: cancelled.append((agent_id, run_id)),
    )
    repo = _clean_repo(tmp_path)

    run = cursor_agent_tool.poll_cloud_run(
        agent_id="bc-1",
        run_id="run-1",
        api_key="test-secret-key",
        timeout_seconds=1,
        started_mono=time.monotonic() - 10,
        workdir=str(repo),
    )
    assert run["_local_error"] == "timeout"
    assert run["_workdir_disk_state"] == "clean"
    assert cancelled == [("bc-1", "run-1")]

    no_workdir = cursor_agent_tool.poll_cloud_run(
        agent_id="bc-1",
        run_id="run-2",
        api_key="test-secret-key",
        timeout_seconds=1,
        started_mono=time.monotonic() - 10,
    )
    assert no_workdir["_local_error"] == "timeout"
    assert "_workdir_disk_state" not in no_workdir


# --- result builder ----------------------------------------------------------


def test_result_builder_timeout_dirty_mentions_disk_state():
    from tools.cursor_agent_tool import _build_cloud_tool_result_from_run

    result_json, success, outcome, _status = _build_cloud_tool_result_from_run(
        agent={"id": "bc-1"},
        run={
            "id": "run-t",
            "agentId": "bc-1",
            "status": "CANCELLED",
            "_local_error": "timeout",
            "_workdir_disk_state": "dirty",
        },
        duration_seconds=5.0,
        log_path=None,
    )
    result = json.loads(result_json)
    assert success is False
    assert outcome == "timeout"
    assert "dirty" in result["error"]
    assert "uncommitted changes" in result["error"]


# --- fallback ----------------------------------------------------------------


@pytest.fixture
def claude_spy(monkeypatch):
    from tools import claude_agent_tool

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return json.dumps({"success": True, "final_report": "did the thing"})

    monkeypatch.setattr(claude_agent_tool, "delegate_claude_agent", _spy)
    return calls


def test_fallback_enabled_clean_workdir_runs_claude(monkeypatch, tmp_path, claude_spy):
    from tools import cursor_agent_tool
    from tools.cursor_run_receipts import read_receipt, receipt_path_for_binding

    session_id, tool_call_id = "fallback-on", "call-on"
    client_id = _install_cloud_timeout_path(
        monkeypatch, tmp_path, session_id=session_id, tool_call_id=tool_call_id
    )
    monkeypatch.setattr(cursor_agent_tool, "_get_cursor_timeout_fallback", lambda: True)
    repo = _clean_repo(tmp_path)

    result = json.loads(
        _delegate(
            "fallback task",
            str(repo.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )

    assert result["success"] is True, result.get("error")
    assert result["fallback"] == "claude_local"
    assert result["cloud_timeout_fallback"] is True
    assert result["final_report"] == "did the thing"
    assert result["agent_id"] == client_id
    assert result["run_id"] == "run-t"
    assert len(claude_spy) == 1
    assert claude_spy[0]["task"] == "fallback task"
    assert claude_spy[0]["workdir"] == str(repo.resolve())
    assert claude_spy[0]["permission_mode"] == "acceptEdits"
    assert claude_spy[0]["background"] is False

    receipt = read_receipt(receipt_path_for_binding(session_id, tool_call_id))
    assert receipt is not None
    assert receipt["outcome"] == "success"


def test_fallback_disabled_by_default(monkeypatch, tmp_path, claude_spy):
    from tools import cursor_agent_tool

    session_id, tool_call_id = "fallback-off", "call-off"
    _install_cloud_timeout_path(
        monkeypatch, tmp_path, session_id=session_id, tool_call_id=tool_call_id
    )
    repo = _clean_repo(tmp_path)

    result = json.loads(
        _delegate(
            "no fallback",
            str(repo.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )

    assert result["success"] is False
    assert "timed out" in result["error"]
    assert "clean" in result["error"]
    assert claude_spy == []


def test_fallback_skipped_when_workdir_dirty(monkeypatch, tmp_path, claude_spy):
    from tools import cursor_agent_tool

    session_id, tool_call_id = "fallback-dirty", "call-dirty"
    client_id = _install_cloud_timeout_path(
        monkeypatch, tmp_path, session_id=session_id, tool_call_id=tool_call_id
    )
    monkeypatch.setattr(cursor_agent_tool, "_get_cursor_timeout_fallback", lambda: True)
    monkeypatch.setattr(
        cursor_agent_tool,
        "poll_cloud_run",
        lambda **kwargs: {
            "id": "run-t",
            "agentId": client_id,
            "status": "CANCELLED",
            "_local_error": "timeout",
            "_workdir_disk_state": "dirty",
        },
    )
    repo = _clean_repo(tmp_path)
    (repo / "untracked.txt").write_text("local edit\n", encoding="utf-8")

    result = json.loads(
        _delegate(
            "dirty workdir",
            str(repo.resolve()),
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )

    assert result["success"] is False
    assert "dirty" in result["error"]
    assert claude_spy == []
