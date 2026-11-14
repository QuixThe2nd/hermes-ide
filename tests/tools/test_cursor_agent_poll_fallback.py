"""Edge contracts for Cursor Cloud poll-timeout local recovery."""

from __future__ import annotations

import subprocess


def test_workspace_status_timeout_is_bounded_and_caller_visible(monkeypatch, tmp_path):
    from tools import cursor_agent_poll_fallback as fallback

    captured = {}

    def _timeout(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=kwargs["timeout"],
            output="partial status",
            stderr="status still running",
        )

    monkeypatch.setattr(fallback.subprocess, "run", _timeout)
    observation = fallback.observe_workdir_git_status(str(tmp_path))

    assert captured["argv"] == ["git", "status", "--porcelain=v1"]
    assert captured["cwd"] == str(tmp_path)
    assert 0 < captured["timeout"] <= 5
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert observation["state"] == "timeout"
    assert observation["returncode"] is None
    assert observation["stdout"] == "partial status"
    assert observation["stderr"] == "status still running"
    report = fallback.format_workspace_observation(observation)
    assert "timeout" in report
    assert "partial status" in report
    assert "status still running" in report
