"""Contract tests for hash-verified artifact egress on FINISHED Cursor runs.

The papercut: a cloud run with status FINISHED was reported success even
when its writeup named commits that exist neither on GitHub nor as hashed
local files. Success now requires a hash-verified artifact manifest — a
matching My Machines worker checkout on this host, or a complete run
stream snapshot whose bytes we hash ourselves.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools import cursor_artifact_egress
from tools.cursor_artifact_egress import (
    STREAM_FETCH_TIMEOUT_SECONDS,
    hashes_match,
    recover_artifact_for_receipt,
    verify_finished_egress,
)

AGENT_ID = "agent-egress-1"
RUN_ID = "run-egress-1"
STREAM_URL_SUFFIX = f"/v1/agents/{AGENT_ID}/runs/{RUN_ID}/stream"


class _FetchSpy:
    """Records the single read-only stream GET the egress path may issue."""

    def __init__(self, body: bytes = b"") -> None:
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, *, api_key, timeout):
        self.calls.append(
            {"method": "GET", "url": url, "api_key": api_key, "timeout": timeout}
        )
        return self.body

    @property
    def gets(self) -> int:
        return sum(1 for call in self.calls if call["method"] == "GET")

    @property
    def posts(self) -> int:
        return sum(1 for call in self.calls if call["method"] == "POST")


def _no_roots(monkeypatch) -> None:
    monkeypatch.setattr(
        cursor_artifact_egress, "discover_checkout_roots", lambda *a, **k: []
    )


def _receipt(**extra) -> dict:
    base = {
        "cloud_agent_id": AGENT_ID,
        "cloud_run_id": RUN_ID,
        "source_base_sha": "0" * 40,
    }
    base.update(extra)
    return base


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_worker_checkout(tmp_path: Path):
    """A committed worker checkout with one changed file after its base."""
    repo = tmp_path / "worker-checkout"
    repo.mkdir(parents=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "worker@example.invalid")
    _run_git(repo, "config", "user.name", "Worker")
    rel = "src/main.py"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("print('base')\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "base")
    base_sha = _run_git(repo, "rev-parse", "HEAD")
    content = "print('worker result')\n"
    (repo / rel).write_text(content, encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "worker changes")
    head_sha = _run_git(repo, "rev-parse", "HEAD")
    tree_sha = _run_git(repo, "rev-parse", "HEAD^{tree}")
    return repo, base_sha, head_sha, tree_sha, rel, content


def _sse(*events) -> bytes:
    return ("".join(f"data: {json.dumps(event)}\n\n" for event in events)).encode(
        "utf-8"
    )


def _edit_event(rel: str, content: str) -> dict:
    return {
        "callId": "call-1",
        "name": "edit_file",
        "status": "completed",
        "args": {"path": f"/workspace/{rel}"},
        "result": {
            "success": {"path": f"/workspace/{rel}", "afterFullFileContent": content}
        },
    }


def _complete_stream(rel: str = "src/app.py", content: str = "x = 1\n") -> bytes:
    return _sse(_edit_event(rel, content), {"type": "turn-ended"})


# ---------------------------------------------------------------------------
# 1. FINISHED writeup naming commits is not success without an artifact
# ---------------------------------------------------------------------------


def test_finished_writeup_naming_commits_is_not_success(monkeypatch, tmp_path):
    from tools import cursor_agent_tool

    _no_roots(monkeypatch)
    monkeypatch.setattr(
        cursor_artifact_egress,
        "_http_get_stream",
        _FetchSpy(b""),
    )
    writeup = (
        "Implemented the feature in commits 03edaeab81 and 4076fcd543; "
        "tests updated and passing."
    )
    run = {
        "id": RUN_ID,
        "agentId": AGENT_ID,
        "status": "FINISHED",
        "result": writeup,
        "durationMs": 1000,
    }

    result_json, success, outcome, _status = cursor_agent_tool._build_cloud_tool_result_from_run(
        agent={"id": AGENT_ID, "latestRunId": RUN_ID},
        run=run,
        duration_seconds=1.0,
        log_path=None,
        attempt_id="attempt-1",
        task_text="do work",
        receipt=_receipt(),
        api_key="key",
    )

    assert success is False
    assert outcome == "failed"
    payload = json.loads(result_json)
    assert payload["success"] is False
    assert "missing artifact egress" in payload["error"]
    assert "artifact_manifest" not in payload
    assert payload["final_report"] == writeup


def test_finished_without_checkout_or_stream_fails_sibling_level(monkeypatch):
    _no_roots(monkeypatch)
    spy = _FetchSpy(b"")

    result = verify_finished_egress(_receipt(), api_key="key", fetch_stream=spy)

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert result["manifest"] is None


# ---------------------------------------------------------------------------
# 2. Disk: injected worker checkout verifies against git
# ---------------------------------------------------------------------------


def test_disk_checkout_manifest_verifies(tmp_path, monkeypatch):
    repo, base_sha, head_sha, tree_sha, rel, content = _make_worker_checkout(tmp_path)
    spy = _FetchSpy(b"")
    monkeypatch.setattr(cursor_artifact_egress, "_http_get_stream", spy)

    result = verify_finished_egress(
        _receipt(source_base_sha=base_sha),
        api_key="key",
        checkout_roots=[str(repo)],
    )

    assert result["success"] is True, result.get("error")
    manifest = result["manifest"]
    assert manifest is not None
    assert manifest["checkout_path"] == str(repo.resolve())
    assert manifest["source_base"] == base_sha
    assert manifest["head_sha"] == head_sha
    assert manifest["tree_sha"] == tree_sha
    assert manifest["source"] == "disk"
    assert manifest["files"] == [
        {"path": rel, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    ]
    # Disk verification never touches the network.
    assert spy.calls == []


def test_disk_dirty_worktree_fails_closed(tmp_path, monkeypatch):
    repo, base_sha, *_rest = _make_worker_checkout(tmp_path)
    (repo / "src" / "main.py").write_text("print('dirty')\n", encoding="utf-8")

    result = verify_finished_egress(
        _receipt(source_base_sha=base_sha),
        api_key="key",
        checkout_roots=[str(repo)],
    )

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]


def test_disk_ambiguous_roots_fail_closed(tmp_path):
    repo_a, base_sha, *_ = _make_worker_checkout(tmp_path / "a")
    (tmp_path / "a").mkdir(exist_ok=True)
    repo_b, *_ = _make_worker_checkout(tmp_path / "b")

    result = verify_finished_egress(
        _receipt(source_base_sha=base_sha),
        api_key="key",
        checkout_roots=[str(repo_a), str(repo_b)],
    )

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]


def test_disk_never_guesses_caller_workdir(tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        cursor_artifact_egress,
        "discover_checkout_roots",
        lambda *a, **k: [str(workdir)],
    )
    monkeypatch.setattr(cursor_artifact_egress, "_http_get_stream", _FetchSpy(b""))

    result = verify_finished_egress(_receipt(source_base_sha="1" * 40), api_key="key")

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert result["manifest"] is None


# ---------------------------------------------------------------------------
# 3. Stream fallback: one GET, no POST, long timeout
# ---------------------------------------------------------------------------


def test_truncated_stream_fails_with_single_get_and_long_timeout(monkeypatch):
    _no_roots(monkeypatch)
    snapshot_only = _sse(_edit_event("src/app.py", "x = 1\n"))
    spy = _FetchSpy(snapshot_only)

    result = verify_finished_egress(_receipt(), api_key="key", fetch_stream=spy)

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert result["manifest"] is None
    assert spy.gets == 1
    assert spy.posts == 0
    assert spy.calls[0]["url"].endswith(STREAM_URL_SUFFIX)
    assert spy.calls[0]["timeout"] > 120
    assert STREAM_FETCH_TIMEOUT_SECONDS > 120


def test_missing_cloud_ids_perform_zero_http(monkeypatch):
    _no_roots(monkeypatch)
    spy = _FetchSpy(b"")

    result = verify_finished_egress(
        _receipt(cloud_agent_id="", cloud_run_id=""),
        api_key="key",
        fetch_stream=spy,
    )

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert spy.calls == []


# ---------------------------------------------------------------------------
# 4. Complete stream: untyped snapshot shape, hash gate mandatory
# ---------------------------------------------------------------------------


def test_complete_stream_success_and_hash_gate(tmp_path, monkeypatch):
    _no_roots(monkeypatch)
    rel = "src/app.py"
    content = "def main():\n    return 2\n"
    body = _complete_stream(rel, content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    forced_false = _FetchSpy(body)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cursor_artifact_egress, "hashes_match", lambda *a, **k: False)
        denied = verify_finished_egress(
            _receipt(), api_key="key", fetch_stream=forced_false
        )
    assert denied["success"] is False
    assert "missing artifact egress" in denied["error"]
    assert denied["manifest"] is None

    allowed = verify_finished_egress(
        _receipt(), api_key="key", fetch_stream=_FetchSpy(body)
    )
    assert allowed["success"] is True, allowed.get("error")
    manifest = allowed["manifest"]
    assert manifest is not None
    assert manifest["source"] == "stream"
    assert manifest["checkout_path"] is None
    assert manifest["files"] == [{"path": rel, "sha256": digest}]
    canonical = f"{rel}\n{digest}\n".encode("utf-8")
    assert manifest["tree_sha"] == hashlib.sha256(canonical).hexdigest()


def test_hashes_match_is_consulted_by_real_hasher():
    content = "payload bytes\n"
    files = [
        {"path": "a.txt", "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    ]
    assert hashes_match(files, {"a.txt": content}) is True
    assert hashes_match(files, {"a.txt": "tampered\n"}) is False
    assert hashes_match([], {}) is False


def test_turn_ended_with_only_diff_is_not_success(monkeypatch):
    _no_roots(monkeypatch)
    diff_only = _sse(
        {
            "callId": "call-2",
            "name": "edit_file",
            "status": "completed",
            "args": {"path": "/workspace/src/app.py"},
            "result": {"success": {"path": "/workspace/src/app.py", "diff": "+x = 1"}},
        },
        {"type": "turn-ended"},
    )

    result = verify_finished_egress(
        _receipt(), api_key="key", fetch_stream=_FetchSpy(diff_only)
    )

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]


def test_stream_cut_mid_line_is_not_success(monkeypatch):
    _no_roots(monkeypatch)
    complete = _complete_stream()
    cut = complete[: -len("\n\ndata: {\"type\":\"turn-ended\"}\n\n")] + b"data: {\"type\":"

    result = verify_finished_egress(_receipt(), api_key="key", fetch_stream=_FetchSpy(cut))

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]


def test_later_snapshot_wins_per_path(monkeypatch):
    _no_roots(monkeypatch)
    later = "v2 content\n"
    body = _sse(
        _edit_event("src/app.py", "v1 content\n"),
        _edit_event("src/app.py", later),
        {"type": "turn-ended"},
    )

    result = verify_finished_egress(_receipt(), api_key="key", fetch_stream=_FetchSpy(body))

    assert result["success"] is True, result.get("error")
    manifest = result["manifest"]
    assert manifest is not None
    files = manifest["files"]
    assert files == [
        {"path": "src/app.py", "sha256": hashlib.sha256(later.encode("utf-8")).hexdigest()}
    ]


# ---------------------------------------------------------------------------
# 5. Receipt recovery: read-only, no create, no POST
# ---------------------------------------------------------------------------


def _write_receipt(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "run.receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_recovery_with_cloud_ids_issues_no_post_and_no_create(monkeypatch, tmp_path):
    _no_roots(monkeypatch)
    from tools import cursor_agent_tool

    spy = _FetchSpy(b"")
    monkeypatch.setattr(cursor_artifact_egress, "_http_get_stream", spy)
    creates: list = []
    monkeypatch.setattr(
        cursor_agent_tool,
        "create_agent_with_timeout_dedupe",
        lambda payload, api_key: creates.append(payload) or ({}, {}),
    )
    receipt_path = _write_receipt(tmp_path, _receipt())

    result = recover_artifact_for_receipt(receipt_path, api_key="key")

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert creates == []
    assert spy.posts == 0
    assert spy.gets == 1
    assert spy.calls[0]["url"].endswith(STREAM_URL_SUFFIX)


def test_recovery_without_cloud_ids_performs_zero_http(monkeypatch, tmp_path):
    _no_roots(monkeypatch)
    spy = _FetchSpy(b"")
    monkeypatch.setattr(cursor_artifact_egress, "_http_get_stream", spy)
    receipt_path = _write_receipt(
        tmp_path, _receipt(cloud_agent_id="", cloud_run_id="")
    )

    result = recover_artifact_for_receipt(receipt_path, api_key="key")

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert spy.calls == []


def test_recovery_from_missing_file_fails_without_http(monkeypatch, tmp_path):
    _no_roots(monkeypatch)
    spy = _FetchSpy(b"")
    monkeypatch.setattr(cursor_artifact_egress, "_http_get_stream", spy)

    result = recover_artifact_for_receipt(
        tmp_path / "absent.receipt.json", api_key="key"
    )

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
    assert spy.calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"data: ",
        b"\n",
        b"data: not-json\n\n",
        b"data: {\"type\":\"turn-ended\"}",  # no trailing newline
    ],
)
def test_incomplete_bodies_are_never_success(monkeypatch, body):
    _no_roots(monkeypatch)

    result = verify_finished_egress(_receipt(), api_key="key", fetch_stream=_FetchSpy(body))

    assert result["success"] is False
    assert "missing artifact egress" in result["error"]
