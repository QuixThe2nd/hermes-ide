"""systemd scope resolution for executor↔systemd interactions.

Contracts (2026-08-24 incident: a non-root executor built bare system-scope
commands and every spawn died on polkit ``Access denied``):

- ONE scope resolution feeds every systemctl / systemd-run call.
- user scope prepends ``--user``; system scope keeps the historical bare
  argv byte-for-byte.
- precedence: ``dev_pipeline.systemd_scope`` in config.yaml >
  ``DEV_PIPELINE_SYSTEMD_SCOPE`` > euid auto-detection.
- a spawn failure (scope misresolution included) blocks the task through
  the existing warning-and-block path — never a crash.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.dev_pipeline import executor as ex
from plugins.dev_pipeline.pipeline import (
    get_dev_pipeline_config,
    normalize_systemd_scope,
)
from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _no_scope_env(monkeypatch):
    """Keep the ambient env from pinning scope; tests set it deliberately."""
    monkeypatch.delenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, raising=False)


@pytest.fixture
def no_config(monkeypatch):
    """Pin the config tier to "nothing configured" for direct unit tests."""
    monkeypatch.setattr(ex, "get_dev_pipeline_config", lambda: {})


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _proc(*, rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class _ArgvRecorder:
    """run_subprocess double: records argv, returns scripted results."""

    def __init__(self, results):
        self.calls: list[list[str]] = []
        self._results = list(results)

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        return self._results.pop(0)


def _hermes_home_config() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "config.yaml"


def _write_scope_config(raw: str, *, salt: int = 0) -> None:
    config = _hermes_home_config()
    config.unlink(missing_ok=True)
    # load_config() caches on (mtime_ns, size) and filesystem mtimes tick at
    # kernel-granularity (~4ms here), so back-to-back same-size writes can
    # share a signature — vary the size to keep each write a cache miss.
    pad = "#" * (4 + salt) + "\n"
    config.write_text(
        f"dev_pipeline:\n  systemd_scope: {raw}\n{pad}", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Scope value normalization + config accessor
# ---------------------------------------------------------------------------


def test_normalize_systemd_scope_contracts():
    assert normalize_systemd_scope("user") == "user"
    assert normalize_systemd_scope("USER") == "user"
    assert normalize_systemd_scope(" system ") == "system"
    # Unrecognized values read as unset — a typo must not silently pin scope.
    assert normalize_systemd_scope("banana") is None
    assert normalize_systemd_scope("") is None
    assert normalize_systemd_scope(None) is None
    assert normalize_systemd_scope(0) is None


def test_config_accessor_validates_systemd_scope():
    # Nothing configured in the isolated HERMES_HOME → executor tiers decide.
    assert get_dev_pipeline_config()["systemd_scope"] is None

    for salt, (raw, expected) in enumerate(
        (("user", "user"), ("SYSTEM", "system"), ("banana", None))
    ):
        _write_scope_config(raw, salt=salt)
        assert get_dev_pipeline_config()["systemd_scope"] == expected, raw


# ---------------------------------------------------------------------------
# Precedence: config > env > euid auto-detect
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
def test_config_tier_beats_env_and_euid(monkeypatch):
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert ex.resolve_systemd_scope({"systemd_scope": "system"}) == "system"

    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "system")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ex.resolve_systemd_scope({"systemd_scope": "user"}) == "user"


@pytest.mark.linux_only
def test_env_tier_beats_euid_auto_detect(monkeypatch, no_config):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "system")
    assert ex.resolve_systemd_scope() == "system"

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    assert ex.resolve_systemd_scope() == "user"


@pytest.mark.linux_only
def test_euid_auto_detect(monkeypatch, no_config):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert ex.resolve_systemd_scope() == "user"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ex.resolve_systemd_scope() == "system"


@pytest.mark.linux_only
def test_invalid_config_value_falls_through_to_lower_tiers(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    # "banana" reads as unset → env tier decides.
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    assert ex.resolve_systemd_scope({"systemd_scope": "banana"}) == "user"


@pytest.mark.linux_only
def test_scope_resolution_survives_config_failure(monkeypatch):
    def boom():
        raise RuntimeError("config.yaml exploded")

    monkeypatch.setattr(ex, "get_dev_pipeline_config", boom)
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    assert ex.resolve_systemd_scope() == "user"

    monkeypatch.delenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ex.resolve_systemd_scope() == "system"


@pytest.mark.linux_only
def test_config_yaml_beats_env_and_euid_end_to_end(monkeypatch):
    """Real config.yaml through the canonical loader, not a patched dict."""
    _write_scope_config("system")
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert ex.resolve_systemd_scope() == "system"


# ---------------------------------------------------------------------------
# argv contracts
# ---------------------------------------------------------------------------


def test_user_scope_prepends_user_to_systemctl_argv(monkeypatch, no_config):
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    recorder = _ArgvRecorder([_proc(out="active"), _proc(), _proc(out="42")])

    with patch.object(ex, "run_subprocess", side_effect=recorder):
        ex.systemctl_is_active("hermes-dev-t-1")
        ex.systemctl_stop("hermes-dev-t-1")
        ex.systemctl_show("hermes-dev-t-1", "MainPID")

    assert recorder.calls == [
        ["systemctl", "--user", "is-active", "hermes-dev-t-1"],
        ["systemctl", "--user", "stop", "hermes-dev-t-1"],
        ["systemctl", "--user", "show", "hermes-dev-t-1", "-pMainPID", "--value"],
    ]


def test_system_scope_systemctl_argv_is_byte_identical(monkeypatch, no_config):
    """System scope must keep the historical bare argv — no extra flags."""
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "system")
    recorder = _ArgvRecorder([_proc(out="inactive"), _proc(), _proc()])

    with patch.object(ex, "run_subprocess", side_effect=recorder):
        ex.systemctl_is_active("hermes-dev-t-1")
        ex.systemctl_stop("hermes-dev-t-1")
        ex.systemctl_show("hermes-dev-t-1", "ExecMainStatus")

    assert recorder.calls == [
        ["systemctl", "is-active", "hermes-dev-t-1"],
        ["systemctl", "stop", "hermes-dev-t-1"],
        ["systemctl", "show", "hermes-dev-t-1", "-pExecMainStatus", "--value"],
    ]


def _attempt_spawn() -> _ArgvRecorder:
    # systemd-run succeeds, then the MainPID lookup answers 4242.
    recorder = _ArgvRecorder([_proc(out="Running as unit"), _proc(out="4242")])
    with patch.object(ex, "run_subprocess", side_effect=recorder):
        with patch.object(ex, "get_host_start_time", return_value=12345):
            ok, pid, start = ex.systemd_run_attempt(
                unit="hermes-dev-t-1",
                runtime_max_sec=1800,
                working_directory=Path("/ws/repo"),
                env={"K": "V"},
                argv=["cmd", "arg"],
            )
    assert (ok, pid, start) == (True, 4242, 12345)
    return recorder


def test_user_scope_systemd_run_argv(monkeypatch, no_config):
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    recorder = _attempt_spawn()
    assert recorder.calls[0] == [
        "systemd-run",
        "--user",
        "--unit=hermes-dev-t-1",
        "--property=RuntimeMaxSec=1800",
        "--property=MemoryMax=6G",
        "--property=OOMScoreAdjust=500",
        "--working-directory=/ws/repo",
        "--setenv=K=V",
        "cmd",
        "arg",
    ]
    # The post-spawn PID lookup goes through the same scope seam.
    assert recorder.calls[1] == [
        "systemctl",
        "--user",
        "show",
        "hermes-dev-t-1",
        "-pMainPID",
        "--value",
    ]


def test_system_scope_systemd_run_argv_is_byte_identical(monkeypatch, no_config):
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "system")
    recorder = _attempt_spawn()
    assert recorder.calls[0] == [
        "systemd-run",
        "--unit=hermes-dev-t-1",
        "--property=RuntimeMaxSec=1800",
        "--property=MemoryMax=6G",
        "--property=OOMScoreAdjust=500",
        "--working-directory=/ws/repo",
        "--setenv=K=V",
        "cmd",
        "arg",
    ]
    assert recorder.calls[1] == [
        "systemctl",
        "show",
        "hermes-dev-t-1",
        "-pMainPID",
        "--value",
    ]


# ---------------------------------------------------------------------------
# Spawn failure → clean warning-and-block (the incident's terminal state)
# ---------------------------------------------------------------------------


def test_spawn_failure_blocks_task_with_warning(
    kanban_home, tmp_path, monkeypatch, caplog, no_config
):
    monkeypatch.setenv(ex.DEV_PIPELINE_SYSTEMD_SCOPE_ENV, "user")
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement x"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run_id = kb.latest_run(conn, task_id).id
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    meta = ex.merge_pipeline_state(
        {},
        {
            "phase": ex.PHASE_RUNNING,
            "contract": {"task_summary": "x"},
            "repo_path": str(repo),
            "logs_root": str(logs),
            "run_kind": ex.RUN_KIND_ATTEMPT,
        },
    )
    ex.save_run_metadata(conn, run_id, meta)
    executor = ex.DevExecutor({
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    })
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_RUNNING)
    unit = ex.unit_name(task_id, run_id)

    # Every systemd call fails the way an unreachable user manager does.
    denied = _proc(rc=1, err="Failed to start transient service unit: Access denied")
    systemd_calls: list[list[str]] = []

    def failing_run(args, **_kwargs):
        systemd_calls.append(list(args))
        return denied

    with patch.object(ex, "run_subprocess", side_effect=failing_run):
        with caplog.at_level(logging.WARNING, logger=ex.logger.name):
            executor._spawn_attempt(
                conn, task_id, run_id, meta, ex.pipeline_state(meta)
            )

    # The spawn really went out user-scoped…
    run_call = next(c for c in systemd_calls if c[0] == "systemd-run")
    assert run_call[1] == "--user"
    # …and the failure produced the existing clean warning-and-block shape.
    assert f"systemd-run failed for {unit}" in caplog.text
    assert "Access denied" in caplog.text
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dev_blocked'",
        (task_id,),
    ).fetchall()
    assert events, "spawn failure must record a dev_blocked event"
    payload = json.loads(events[-1]["payload"])
    assert payload["block_kind"] == "infra_broken"
    assert payload["reason"] == "failed to spawn attempt unit"
    assert task_id not in executor._active
    conn.close()
