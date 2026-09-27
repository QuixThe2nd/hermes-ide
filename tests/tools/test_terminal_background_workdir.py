"""Explicit background workdir must fail closed when the path is missing."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.tools.test_notify_on_complete import _silent_bg_harness


def test_background_missing_workdir_fails_closed_without_spawn(monkeypatch, tmp_path):
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    spawned = []

    def tracking_spawn(**kwargs):
        spawned.append(kwargs)
        raise AssertionError("spawn_local must not run for a missing workdir")

    monkeypatch.setattr(
        "tools.process_registry.process_registry.spawn_local", tracking_spawn
    )
    missing = tmp_path / "does-not-exist"
    try:
        result = json.loads(
            tt.terminal_tool(
                command="pwd",
                background=True,
                workdir=str(missing),
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert spawned == []
    assert result["exit_code"] == 1
    assert result["status"] == "error"
    assert "does not exist" in result["error"]
    assert str(missing) in result["error"]
    assert result.get("cwd") == str(missing)


def test_background_existing_workdir_spawns_and_reports_cwd(monkeypatch, tmp_path):
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    captured = {}

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="proc_wd_ok",
            pid=99,
            notify_on_complete=False,
            watcher_platform="",
            watcher_chat_id="",
            watcher_user_id="",
            watcher_user_name="",
            watcher_thread_id="",
            watcher_message_id="",
            watcher_interval=0,
        )

    monkeypatch.setattr(
        "tools.process_registry.process_registry.spawn_local", fake_spawn
    )
    try:
        result = json.loads(
            tt.terminal_tool(
                command="pwd",
                background=True,
                workdir=str(tmp_path),
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert captured.get("cwd") == str(tmp_path)
    assert result["session_id"] == "proc_wd_ok"
    assert result["cwd"] == str(tmp_path)
    assert result["exit_code"] == 0


def test_explicit_local_workdir_error_skips_non_local(tmp_path):
    from tools.terminal_tool import _explicit_local_workdir_error

    missing = str(tmp_path / "nope")
    assert _explicit_local_workdir_error(
        missing, env_type="docker", default_cwd=str(tmp_path)
    ) is None
    assert _explicit_local_workdir_error(
        None, env_type="local", default_cwd=str(tmp_path)
    ) is None
    assert _explicit_local_workdir_error(
        str(tmp_path), env_type="local", default_cwd=str(tmp_path)
    ) is None
    err = _explicit_local_workdir_error(
        missing, env_type="local", default_cwd=str(tmp_path)
    )
    assert err is not None and "does not exist" in err
