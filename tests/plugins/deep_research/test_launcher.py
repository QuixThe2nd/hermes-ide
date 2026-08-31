"""Launcher: exact systemd argv, unit isolation, targeted cancel, PID guards.

Covers TASK.md test areas 5 (exact process/systemd argv, timeout and per-job
unit isolation) and 6 (cancel only the target job).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from plugins.deep_research import jobs, launcher


class RecordingRunner:
    """Callable standing in for subprocess/systemctl, capturing argv lists."""

    def __init__(self, *, results=None) -> None:
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args):
        argv = [str(part) for part in args]
        self.calls.append(argv)
        default = (0, "active\n", "")
        return self.results.get(argv[0], default)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture()
def job(home: Path) -> tuple[str, Path]:
    created = jobs.create_job(
        brief="brief",
        research_questions=None,
        timeout_minutes=20,
        max_parallel=1,
        worker_profile="researcher",
        hermes_home=home,
    )
    return created["job_id"], created["dir"]


# ---------------------------------------------------------------------------
# Exact systemd argv (area 5)
# ---------------------------------------------------------------------------


class TestSystemdArgv:
    def test_transient_user_service_argv(self, job: tuple[str, Path]) -> None:
        job_id, _directory = job
        argv = launcher.build_systemd_run_argv(
            unit=launcher.unit_name(job_id),
            runtime_max_sec=1500,
            memory_max="2G",
            working_directory=Path("/repo"),
            env={"HERMES_HOME": "/home/dr", "PYTHONUNBUFFERED": "1"},
            argv=["/usr/bin/python3", "-m", "plugins.deep_research.runner", "--job", job_id],
        )
        # A transient USER service, never --scope (a scope would live inside
        # the gateway's cgroup and die with the gateway).
        assert argv[0] == "systemd-run" and argv[1] == "--user"
        assert "--scope" not in argv
        assert "--collect" in argv
        assert f"--unit={launcher.unit_name(job_id)}" in argv
        assert "--property=RuntimeMaxSec=1500" in argv
        assert "--property=MemoryMax=2G" in argv
        assert "--property=OOMScoreAdjust=500" in argv
        assert "--working-directory=/repo" in argv
        # Env handoff is explicit and minimal; no secrets.
        assert "--setenv=HERMES_HOME=/home/dr" in argv
        assert "--setenv=PYTHONUNBUFFERED=1" in argv
        assert argv[argv.index("--") + 1 :] == [
            "/usr/bin/python3",
            "-m",
            "plugins.deep_research.runner",
            "--job",
            job_id,
        ]

    def test_runtime_budget_includes_timeout_plus_slack(self, job: tuple[str, Path]) -> None:
        job_id, _directory = job
        result = launcher.LaunchResult(mode="systemd", unit=launcher.unit_name(job_id), pid=None, pid_start=None)
        assert result.as_status()["runner_unit"] == f"hermes-research-{job_id}"

    def test_unit_name_is_per_job_and_validated(self) -> None:
        assert launcher.unit_name("rj_0123456789ab") == "hermes-research-rj_0123456789ab"
        for bad in ("../../etc", "rj_X", "other job"):
            with pytest.raises(ValueError):
                launcher.unit_name(bad)

    def test_launch_builds_systemd_command_when_available(self, job: tuple[str, Path], monkeypatch) -> None:
        job_id, directory = job
        recorded = RecordingRunner()

        def fake_available(*, runner=None) -> bool:
            return True

        monkeypatch.setattr(launcher, "systemd_user_available", fake_available)
        result = launcher.launch(
            job_id=job_id,
            timeout_minutes=20,
            memory_max="2G",
            hermes_home=directory.parent.parent,
            log_path=directory / "runner.out",
            runner_mode="auto",
            runner=recorded,
        )
        assert result.mode == "systemd"
        assert result.unit == f"hermes-research-{job_id}"
        run_argv = recorded.calls[0]
        assert run_argv[:2] == ["systemd-run", "--user"]
        # 20 min budget + the documented synthesis slack.
        assert "--property=RuntimeMaxSec=1500" in run_argv
        # The runner argv carries no brief and no shell string.
        tail = run_argv[run_argv.index("--") + 1 :]
        assert "--job" in tail and job_id in tail
        assert "brief" not in " ".join(tail)
        # Liveness probe uses the same unit, nothing broader.
        assert recorded.calls[1][0] == "systemctl" and recorded.calls[1][3] == result.unit

    def test_launch_falls_back_when_systemd_fails(self, job: tuple[str, Path], monkeypatch) -> None:
        job_id, directory = job
        recorded = RecordingRunner(results={"systemd-run": (1, "", "no user bus")})

        monkeypatch.setattr(launcher, "systemd_user_available", lambda *, runner=None: True)
        spawned: dict = {}

        def fake_popen(argv, **kwargs):
            spawned["argv"] = list(argv)
            spawned["kwargs"] = kwargs

            class _Proc:
                pid = 97531

            return _Proc()

        result = launcher.launch(
            job_id=job_id,
            timeout_minutes=5,
            memory_max="2G",
            hermes_home=directory.parent.parent,
            log_path=directory / "runner.out",
            runner_mode="auto",
            runner=recorded,
            popen=fake_popen,
        )
        assert result.mode == "fallback" and result.unit is None
        assert spawned["argv"][spawned["argv"].index("--job") + 1] == job_id
        assert spawned["kwargs"]["cwd"] == str(launcher.repo_root())
        # Detached: the child starts its own session on POSIX.
        assert spawned["kwargs"].get("start_new_session") is True
        # The capture file is created private, never world-readable.
        assert (directory / "runner.out").stat().st_mode & 0o777 == 0o600

    def test_runner_mode_forces_fallback(self, job: tuple[str, Path], monkeypatch) -> None:
        job_id, directory = job
        # A healthy systemd must be IGNORED when the config says fallback.
        monkeypatch.setattr(launcher, "systemd_user_available", lambda *, runner=None: True)
        recorded = RecordingRunner()

        def fake_popen(argv, **kwargs):
            class _Proc:
                pid = 97531

            return _Proc()

        result = launcher.launch(
            job_id=job_id,
            timeout_minutes=5,
            memory_max="2G",
            hermes_home=directory.parent.parent,
            log_path=directory / "runner.out",
            runner_mode="fallback",
            runner=recorded,
            popen=fake_popen,
        )
        assert result.mode == "fallback"
        assert recorded.calls == []  # systemd-run was never consulted


# ---------------------------------------------------------------------------
# Targeted cancel (area 6)
# ---------------------------------------------------------------------------


class TestCancelScoping:
    def test_cancel_stops_only_this_unit(self, job: tuple[str, Path]) -> None:
        _job_id, directory = job
        other = "hermes-research-rj_ffffffffffff"
        status = {
            "runner_mode": "systemd",
            "runner_unit": "hermes-research-rj_aaaaaaaaaaaa",
        }
        recorded = RecordingRunner(results={"systemctl": (0, "", "")})
        assert launcher.cancel_runner(status, runner=recorded) is True
        argv = recorded.calls[0]
        assert argv[:3] == ["systemctl", "--user", "stop"]
        assert argv[3] == status["runner_unit"]
        assert other not in argv
        # Exactly one stop command was issued — no broad sweep.
        assert len(recorded.calls) == 1

    def test_cancel_without_unit_targets_recorded_pid_tree(self, job: tuple[str, Path], monkeypatch) -> None:
        _job_id, _directory = job
        killed: list[tuple[int, object]] = []

        class FakeProc:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def children(self, recursive=True):
                return [FakeProc(self.pid + 1)]

            def terminate(self):
                killed.append((self.pid, "term"))

            def kill(self):
                killed.append((self.pid, "kill"))

        monkeypatch.setattr(launcher, "_pid_alive", lambda pid, expected_start=None: True)
        monkeypatch.setattr("psutil.Process", lambda pid: FakeProc(pid))
        monkeypatch.setattr("psutil.wait_procs", lambda procs, timeout=None: (list(procs), []))
        status = {"runner_mode": "fallback", "runner_pid": 555000, "runner_pid_start": None}
        assert launcher.cancel_runner(status, runner=RecordingRunner()) is True
        assert [pid for pid, _sig in killed] == [555001, 555000]

    def test_cancel_refuses_when_pid_was_recycled(self, monkeypatch) -> None:
        # A recorded pid whose start time no longer matches must not be killed.
        monkeypatch.setattr(launcher, "pid_start_time", lambda pid: 999)
        status = {"runner_mode": "fallback", "runner_pid": 555000, "runner_pid_start": 111}
        assert launcher.cancel_runner(status) is True  # treated as already gone
        # And liveness reports dead rather than following the recycled pid.
        assert launcher.runner_alive(status) is False

    def test_cancel_nothing_recorded(self) -> None:
        assert launcher.cancel_runner({"runner_mode": "fallback"}) is False


# ---------------------------------------------------------------------------
# Liveness + PID reuse guards
# ---------------------------------------------------------------------------


class TestLiveness:
    def test_unit_liveness_mapped_from_systemctl(self, job: tuple[str, Path]) -> None:
        job_id, _ = job
        status = {"runner_mode": "systemd", "runner_unit": f"hermes-research-{job_id}"}
        for state, expected in (
            ("active\n", True),
            ("activating\n", True),
            ("inactive\n", False),
            ("failed\n", False),
        ):
            runner = RecordingRunner(results={"systemctl": (3, state, "")})
            assert launcher.runner_alive(status, runner=runner) is expected

    def test_unit_liveness_unknown_is_none(self, job: tuple[str, Path]) -> None:
        job_id, _ = job
        status = {"runner_mode": "systemd", "runner_unit": f"hermes-research-{job_id}"}
        runner = RecordingRunner(results={"systemctl": (1, "garbage", "boom")})
        assert launcher.runner_alive(status, runner=runner) is None

    def test_own_pid_is_alive_and_reuse_guarded(self) -> None:
        marker = threading.Event()
        child = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 5"], start_new_session=True
        )
        try:
            start = launcher.pid_start_time(child.pid)
            assert isinstance(start, int)
            status = {"runner_mode": "fallback", "runner_pid": child.pid, "runner_pid_start": start}
            assert launcher.runner_alive(status) is True
            # A mismatched recorded start time means the pid was recycled.
            assert launcher.runner_alive({**status, "runner_pid_start": start + 1}) is False
        finally:
            child.terminate()
            child.wait(timeout=10)
            marker.set()
        # After reaping, the same recorded tuple must read dead — even if the
        # OS has already handed the number to something else.
        assert launcher.runner_alive(status) is False

    def test_start_time_guard_survives_missing_proc(self) -> None:
        assert launcher.pid_start_time(999_999_999) is None


# ---------------------------------------------------------------------------
# Worker binary resolution
# ---------------------------------------------------------------------------


class TestWorkerArgv:
    def test_hermes_bin_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_BIN", "/opt/fake/hermes")
        assert launcher.resolve_worker_argv() == ["/opt/fake/hermes"]

    def test_argv_is_a_list_never_a_shell_string(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("HERMES_BIN", raising=False)
        argv = launcher.runner_argv("rj_0123456789ab", tmp_path)
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)
        assert argv[argv.index("--job") + 1] == "rj_0123456789ab"
        assert "&&" not in argv and "|" not in argv


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------


class TestAvailabilityProbe:
    def test_missing_binaries_means_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
        assert launcher.systemd_user_available() is False
