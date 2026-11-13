"""Poll-timeout local-fallback contract tests for delegate_cursor_agent.

Covers the single fall-through: a Cloud *poll* timeout (GET /runs request
timeout or wall-clock timeout) with an untouched workdir runs ONE local CLI
fallback and never retries Cloud. HTTP 500, interrupts, and a missing API
key keep their existing fail-closed behavior.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.cursor_run_receipts import deterministic_client_agent_id


SESSION_ID = "fallback-session"
TOOL_CALL_ID = "fallback-tool-call"
GET_TIMEOUT_MSG = "Cursor Cloud Agent request timed out: GET /v1/agents/{agent}/runs/run-aaaa"


def _client_id() -> str:
    return deterministic_client_agent_id(SESSION_ID, TOOL_CALL_ID)


def _delegate(workdir: str, *, task: str = "fix the bug", timeout_seconds: int | None = None):
    from tools import cursor_agent_tool

    kwargs: dict = {"session_id": SESSION_ID, "tool_call_id": TOOL_CALL_ID}
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    return cursor_agent_tool.delegate_cursor_agent(task=task, workdir=workdir, **kwargs)


def _make_git_repo(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "-c", "commit.gpgsign=false", "add", ".")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return repo


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"


def _install_cloud(monkeypatch, tmp_path):
    """Patch the cloud seams; return counters shared with the test's stubs."""
    from tools import cursor_agent_tool

    secret = tmp_path / "cursor-cloud.env"
    secret.write_text("CURSOR_API_KEY=test-secret-key\n", encoding="utf-8")
    monkeypatch.setattr(cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", secret)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(
        cursor_agent_tool, "resolve_workdir_origin", lambda workdir: "https://github.com/acme/demo"
    )
    monkeypatch.setattr(cursor_agent_tool, "resolve_workdir_starting_ref", lambda workdir: "main")
    monkeypatch.setattr(cursor_agent_tool, "_emit_progress_notice", lambda message: False)

    counters = {"create": 0, "cancel": 0}
    events: list[str] = []
    client_id = _client_id()

    def _create(payload, api_key):
        counters["create"] += 1
        return (
            {
                "id": client_id,
                "name": "hermes-test",
                "url": f"https://cursor.com/agents/{client_id}",
                "latestRunId": "run-aaaa",
            },
            {"id": "run-aaaa", "agentId": client_id, "status": "CREATING"},
        )

    def _cancel(agent_id, run_id, api_key):
        counters["cancel"] += 1
        events.append("cancel")

    monkeypatch.setattr(cursor_agent_tool, "create_agent_with_timeout_dedupe", _create)
    monkeypatch.setattr(cursor_agent_tool, "cancel_cloud_run", _cancel)
    return counters, events, client_id


def _install_local_runners(
    monkeypatch,
    *,
    cursor_payload=None,
    claude_payload=None,
):
    """Patch the local-runner seams; default payloads succeed and record calls."""
    from tools import cursor_cloud_timeout_fallback as fallback

    calls = {"cursor": [], "claude": []}
    events: list[str] = []

    default_cursor = {
        "success": True,
        "final_report": "local cursor report",
        "delegations": [],
        "duration_seconds": 1.5,
        "session_id": None,
        "log_path": None,
        "error": None,
    }
    default_claude = {
        "success": True,
        "final_report": "local claude report",
        "delegations": [],
        "duration_seconds": 2.5,
        "session_id": "claude-session",
        "log_path": None,
        "error": None,
    }

    def _cursor(**kwargs):
        calls["cursor"].append(kwargs)
        events.append("cursor")
        if cursor_payload is _UNAVAILABLE:
            return None
        return dict(cursor_payload if cursor_payload is not None else default_cursor)

    def _claude(**kwargs):
        calls["claude"].append(kwargs)
        events.append("claude")
        if claude_payload is _UNAVAILABLE:
            return None
        return dict(claude_payload if claude_payload is not None else default_claude)

    monkeypatch.setattr(fallback, "run_local_cursor_cli", _cursor)
    monkeypatch.setattr(fallback, "run_local_claude_cli", _claude)
    return calls, events


class _Unavailable:
    """Marker: the patched runner reports itself unable to start."""


_UNAVAILABLE = _Unavailable()


def test_get_run_timeout_falls_through_to_local_cursor_cli(monkeypatch, tmp_path):
    """GET /runs timeout through the real poll: one create, one poll, local run."""
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    counters, cloud_events, client_id = _install_cloud(monkeypatch, tmp_path)
    calls, runner_events = _install_local_runners(monkeypatch)
    get_calls: list[str] = []

    def _http(method, path, **kwargs):
        if method == "GET" and "/runs/" in path:
            get_calls.append(path)
            raise TimeoutError(f"Cursor Cloud Agent request timed out: {method} {path}")
        raise AssertionError(f"unexpected fake cloud request: {method} {path}")

    monkeypatch.setattr(cursor_agent_tool, "_http_request", _http)
    monkeypatch.setattr(cursor_agent_tool, "_check_interrupted", lambda: False)

    result = json.loads(_delegate(str(repo), timeout_seconds=120))

    assert result["success"] is True, result.get("error")
    assert result["final_report"] == "local cursor report"
    assert result["cloud_timeout"] is True
    assert result["do_not_retry_cloud"] is True
    assert result["workdir_untouched"] is True
    assert result["workdir_git_status"] == ""
    assert result["fallback"] == "local_cursor_cli"
    assert "scratch clone untouched" in result["note"]
    assert counters["create"] == 1
    assert len(get_calls) == 1  # poll not retried
    assert counters["cancel"] == 1
    # cancel fires before the local writer starts
    assert cloud_events + runner_events == ["cancel", "cursor"]
    assert len(calls["cursor"]) == 1
    assert calls["cursor"][0]["timeout_seconds"] == 120  # fresh clamped budget
    assert calls["cursor"][0]["workdir"] == str(repo)
    assert calls["claude"] == []
    assert "test-secret-key" not in json.dumps(result)


def test_wall_clock_timeout_falls_through_without_second_create(monkeypatch, tmp_path):
    """poll_cloud_run returning _local_error=timeout: fallback, no cloud retry."""
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    counters, cloud_events, _client = _install_cloud(monkeypatch, tmp_path)
    calls, runner_events = _install_local_runners(monkeypatch)

    poll_calls: list[dict] = []

    def _poll(**kwargs):
        poll_calls.append(kwargs)
        return {"id": "run-aaaa", "agentId": _client, "status": "RUNNING", "_local_error": "timeout"}

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo), timeout_seconds=300))

    assert result["success"] is True, result.get("error")
    assert result["fallback"] == "local_cursor_cli"
    assert result["cloud_timeout"] is True
    assert result["do_not_retry_cloud"] is True
    assert result["workdir_untouched"] is True
    assert counters["create"] == 1
    assert len(poll_calls) == 1
    assert len(calls["cursor"]) == 1
    assert calls["cursor"][0]["timeout_seconds"] == 300  # fresh budget, not a leftover
    assert calls["claude"] == []
    assert cloud_events + runner_events == ["cancel", "cursor"]


def test_http_500_does_not_fall_through(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    _counters, _events, _client = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(monkeypatch)

    def _poll(**kwargs):
        raise cursor_agent_tool.CursorCloudError(
            "Cursor Cloud Agent API error (GET /v1/agents/bc-1/runs/run-1): HTTP 500"
        )

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is False
    assert "HTTP 500" in result["error"]
    assert calls["cursor"] == []
    assert calls["claude"] == []
    assert "cloud_timeout" not in result
    assert "do_not_retry_cloud" not in result


def test_missing_api_key_still_fails_closed(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(
        cursor_agent_tool, "CURSOR_CLOUD_ENV_PATH", tmp_path / "absent.env"
    )
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    calls, _events = _install_local_runners(monkeypatch)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is False
    assert "CURSOR_API_KEY is missing" in result["error"]
    assert "no silent fallback" in result["error"]
    assert calls["cursor"] == []
    assert calls["claude"] == []


def test_workdir_changed_suppresses_local_fallback(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    counters, _events, client_id = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(monkeypatch)

    def _poll(**kwargs):
        # Something wrote to the tree while the cloud run was in flight.
        (repo / "sneaky.txt").write_text("changed during run\n", encoding="utf-8")
        raise cursor_agent_tool.CursorCloudError(GET_TIMEOUT_MSG.format(agent=client_id))

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is False
    assert result["cloud_timeout"] is True
    assert result["do_not_retry_cloud"] is True
    assert result["workdir_untouched"] is False
    assert "sneaky.txt" in result["workdir_git_status"]
    assert result["fallback"] is None
    assert "scratch clone untouched" in result["note"]
    assert "do not retry cloud" in result["error"].lower()
    assert calls["cursor"] == []
    assert calls["claude"] == []
    assert counters["create"] == 1
    # The concurrent write survived — no clobbering.
    assert (repo / "sneaky.txt").exists()


def test_cursor_runner_unavailable_falls_through_to_claude(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    counters, _events, _client = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(
        monkeypatch, cursor_payload=_UNAVAILABLE
    )

    def _poll(**kwargs):
        raise cursor_agent_tool.CursorCloudError(GET_TIMEOUT_MSG.format(agent=_client))

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is True, result.get("error")
    assert result["final_report"] == "local claude report"
    assert result["fallback"] == "local_claude_cli"
    assert result["workdir_untouched"] is True
    assert result["do_not_retry_cloud"] is True
    assert len(calls["cursor"]) == 1
    assert len(calls["claude"]) == 1
    assert calls["claude"][0]["tool_call_id"] == TOOL_CALL_ID
    assert calls["claude"][0]["task"] == "fix the bug"
    assert calls["claude"][0]["workdir"] == str(repo)
    assert counters["create"] == 1  # cloud not retried


def test_both_runners_unavailable_fails_without_claiming_success(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    counters, _events, client_id = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(
        monkeypatch, cursor_payload=_UNAVAILABLE, claude_payload=_UNAVAILABLE
    )

    def _poll(**kwargs):
        raise cursor_agent_tool.CursorCloudError(GET_TIMEOUT_MSG.format(agent=client_id))

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is False
    assert result["cloud_timeout"] is True
    assert result["do_not_retry_cloud"] is True
    assert result["fallback"] is None
    assert result["workdir_untouched"] is True
    assert "scratch clone untouched" in result["note"]
    assert "no local fallback runner" in result["error"]
    assert len(calls["cursor"]) == 1
    assert len(calls["claude"]) == 1
    assert counters["create"] == 1


def test_git_status_inspection_failure_does_not_fall_through(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    plain = tmp_path / "plain"
    plain.mkdir()
    _counters, _events, _client = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(monkeypatch)

    def _poll(**kwargs):
        raise cursor_agent_tool.CursorCloudError(GET_TIMEOUT_MSG.format(agent=_client))

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(plain)))

    assert result["success"] is False
    assert result["cloud_timeout"] is True
    assert result["do_not_retry_cloud"] is True
    assert result["workdir_untouched"] is False
    assert result["fallback"] is None
    assert "could not be inspected" in result["error"]
    assert calls["cursor"] == []
    assert calls["claude"] == []


def test_interrupt_does_not_fall_through(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    repo = _make_git_repo(tmp_path)
    _counters, _events, client_id = _install_cloud(monkeypatch, tmp_path)
    calls, _runner_events = _install_local_runners(monkeypatch)

    def _poll(**kwargs):
        return {"id": "run-aaaa", "agentId": client_id, "status": "CANCELLED", "_local_error": "interrupted"}

    monkeypatch.setattr(cursor_agent_tool, "poll_cloud_run", _poll)

    result = json.loads(_delegate(str(repo)))

    assert result["success"] is False
    assert result["error"] == "interrupted"
    assert "cloud_timeout" not in result
    assert calls["cursor"] == []
    assert calls["claude"] == []


def test_local_cursor_argv_omits_api_key_and_worktree(monkeypatch, tmp_path):
    from tools import cursor_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(cursor_agent_tool, "preflight_worker_auth", lambda binary, env: None)
    monkeypatch.setenv("CURSOR_API_KEY", "leak-check-secret")

    captured: dict = {}

    def _run_agent_cli(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        captured["workdir"] = kwargs["workdir"]
        captured["timeout_seconds"] = kwargs["timeout_seconds"]
        return (None, str(tmp_path / "fake.log"), "done locally", 1.0, 0)

    monkeypatch.setattr(fallback, "run_agent_cli", _run_agent_cli)

    payload = fallback.run_local_cursor_cli(
        task="fix it", workdir=str(repo), model="composer-2.5", timeout_seconds=240
    )

    assert payload["success"] is True
    assert payload["final_report"] == "done locally"
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/agent"
    assert cmd[1:6] == ["-p", "--force", "--trust", "--output-format", "text"]
    assert cmd[6:8] == ["--model", "composer-2.5"]
    assert cmd[-1] == cursor_agent_tool.NO_PUSH_PROMPT_PREFIX + "fix it"
    assert "--api-key" not in cmd
    assert "--worktree" not in cmd
    assert not any("leak-check-secret" in part for part in cmd)
    env = captured["env"]
    assert env.get("CURSOR_API_KEY") is None
    assert not any(value == "leak-check-secret" for value in env.values())
    assert captured["workdir"] == str(repo)
    assert captured["timeout_seconds"] == 240

    # Without a pinned model the argv drops --model entirely.
    fallback.run_local_cursor_cli(
        task="fix it", workdir=str(repo), model=None, timeout_seconds=240
    )
    assert captured["cmd"] == [
        "/usr/bin/agent",
        "-p",
        "--force",
        "--trust",
        "--output-format",
        "text",
        cursor_agent_tool.NO_PUSH_PROMPT_PREFIX + "fix it",
    ]


def test_local_cursor_runner_maps_run_outcome(monkeypatch, tmp_path):
    from tools import cursor_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")
    monkeypatch.setattr(cursor_agent_tool, "preflight_worker_auth", lambda binary, env: None)

    def _run_agent_cli(cmd, **kwargs):
        return ("timeout", str(tmp_path / "fake.log"), "partial output", 3.0, None)

    monkeypatch.setattr(fallback, "run_agent_cli", _run_agent_cli)

    payload = fallback.run_local_cursor_cli(
        task="fix it", workdir=str(repo), model=None, timeout_seconds=240
    )
    assert payload["success"] is False
    assert payload["error"] == "timeout"
    assert payload["final_report"] == "partial output"


def test_cursor_runner_missing_binary_returns_none(monkeypatch, tmp_path):
    from tools import cursor_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: None)
    assert (
        fallback.run_local_cursor_cli(
            task="x", workdir=str(tmp_path), model=None, timeout_seconds=60
        )
        is None
    )


def test_cursor_runner_auth_failure_returns_none(monkeypatch, tmp_path):
    from tools import cursor_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    monkeypatch.setattr(cursor_agent_tool, "resolve_cursor_agent_binary", lambda: "/usr/bin/agent")

    def _refuse(binary, env):
        raise cursor_agent_tool.CursorCloudError(
            "Cursor My Machines worker is not authenticated; run agent login"
        )

    monkeypatch.setattr(cursor_agent_tool, "preflight_worker_auth", _refuse)
    assert (
        fallback.run_local_cursor_cli(
            task="x", workdir=str(tmp_path), model=None, timeout_seconds=60
        )
        is None
    )


def test_claude_runner_missing_binary_returns_none(monkeypatch):
    from tools import claude_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    monkeypatch.setattr(claude_agent_tool, "resolve_claude_binary", lambda model=None: None)
    assert (
        fallback.run_local_claude_cli(
            task="x",
            workdir="/tmp",
            timeout_seconds=60,
            hermes_session_id="s",
            tool_call_id="c",
        )
        is None
    )


def test_claude_runner_forwards_to_delegate_claude_agent(monkeypatch, tmp_path):
    from tools import claude_agent_tool
    from tools import cursor_cloud_timeout_fallback as fallback

    captured: dict = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "final_report": "claude done"})

    monkeypatch.setattr(claude_agent_tool, "resolve_claude_binary", lambda model=None: "/usr/bin/claude-glm")
    monkeypatch.setattr(claude_agent_tool, "delegate_claude_agent", _fake_delegate)

    payload = fallback.run_local_claude_cli(
        task="the task",
        workdir=str(tmp_path),
        timeout_seconds=90,
        hermes_session_id="sess-1",
        tool_call_id="call-1",
    )

    assert payload["success"] is True
    assert payload["final_report"] == "claude done"
    assert captured["task"] == "the task"
    assert captured["workdir"] == str(tmp_path)
    assert captured["timeout_seconds"] == 90
    assert captured["background"] is False
    assert captured["session_id"] == "sess-1"
    assert captured["tool_call_id"] == "call-1:cloud-timeout-fallback"
