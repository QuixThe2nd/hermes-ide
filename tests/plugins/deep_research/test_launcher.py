"""Launcher: manager scopes, exact systemd argv, targeted cancel, PID guards.

Covers TASK.md test areas 5 (exact process/systemd argv, timeout and per-job
unit isolation) and 6 (cancel only the target job), plus the correction-pass
requirements: a real bounded manager probe, user/system transient services,
root auto selection, forced-systemd fail-closed, and the fallback reason.
"""

from __future__ import annotations

import subprocess
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


def fake_popen_spawn(spawned: dict, pid: int = 97531):
    """A Popen stand-in that records the spawn instead of running it."""

    def fake_popen(argv, **kwargs):
        spawned["argv"] = list(argv)
        spawned["kwargs"] = kwargs
        proc = type("_Proc", (), {"pid": pid})
        return proc()

    return fake_popen


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

    def test_transient_system_service_argv_omits_user(self, job: tuple[str, Path]) -> None:
        job_id, _directory = job
        argv = launcher.build_systemd_run_argv(
            unit=launcher.unit_name(job_id),
            scope=launcher.MANAGER_SCOPE_SYSTEM,
            runtime_max_sec=900,
            memory_max="2G",
            working_directory=Path("/repo"),
            env={"HERMES_HOME": "/home/dr"},
            argv=["/usr/bin/python3", "-m", "plugins.deep_research.runner", "--job", job_id],
        )
        # A root gateway gets a SYSTEM transient service: same unit, same
        # bounds, just no `--user` flag (no user D-Bus session exists there).
        assert argv[0] == "systemd-run"
        assert "--user" not in argv
        assert "--scope" not in argv
        assert f"--unit={launcher.unit_name(job_id)}" in argv
        assert "--property=RuntimeMaxSec=900" in argv
        assert "--property=MemoryMax=2G" in argv
        assert "--property=OOMScoreAdjust=500" in argv
        assert "--working-directory=/repo" in argv

    def test_launch_result_records_scope_and_unit(self, job: tuple[str, Path]) -> None:
        job_id, _directory = job
        result = launcher.LaunchResult(
            mode="systemd", unit=launcher.unit_name(job_id), manager_scope="system"
        )
        status = result.as_status()
        assert status["runner_unit"] == f"hermes-research-{job_id}"
        assert status["runner_mode"] == "systemd"
        assert status["runner_scope"] == "system"
        assert "runner_reason" not in status

    def test_fallback_result_records_scope_and_reason(self) -> None:
        result = launcher.LaunchResult(mode="fallback", reason="no usable manager")
        status = result.as_status()
        assert status["runner_scope"] == "fallback"
        assert status["runner_reason"] == "no usable manager"

    def test_unit_name_is_per_job_and_validated(self) -> None:
        assert launcher.unit_name("rj_0123456789ab") == "hermes-research-rj_0123456789ab"
        for bad in ("../../etc", "rj_X", "other job"):
            with pytest.raises(ValueError):
                launcher.unit_name(bad)


# ---------------------------------------------------------------------------
# Transient-unit environment handoff
# ---------------------------------------------------------------------------


class TestSystemdEnvHandoff:
    def test_hermes_bin_is_passed_through_to_the_transient_unit(self, job, monkeypatch) -> None:
        # resolve_worker_argv() honors $HERMES_BIN; the runner inside the unit
        # must resolve the same binary, so the override has to survive the
        # handoff. No live systemd daemon is touched — the runner is recorded.
        job_id, _directory = job
        monkeypatch.setenv("HERMES_BIN", "/opt/fake/hermes")
        recorded = RecordingRunner()
        launcher.systemd_launch(
            job_id=job_id,
            scope=launcher.MANAGER_SCOPE_USER,
            runtime_max_sec=1500,
            memory_max="2G",
            hermes_home=Path("/home/dr"),
            runner=recorded,
        )
        argv = recorded.calls[0]
        assert argv[0] == "systemd-run" and argv[1] == "--user"
        assert "--setenv=HERMES_BIN=/opt/fake/hermes" in argv
        assert "--setenv=HERMES_HOME=/home/dr" in argv

    def test_hermes_bin_absent_when_not_set(self, job, monkeypatch) -> None:
        job_id, _directory = job
        monkeypatch.delenv("HERMES_BIN", raising=False)
        recorded = RecordingRunner()
        launcher.systemd_launch(
            job_id=job_id,
            scope=launcher.MANAGER_SCOPE_USER,
            runtime_max_sec=1500,
            memory_max="2G",
            hermes_home=Path("/home/dr"),
            runner=recorded,
        )
        assert not any(part.startswith("--setenv=HERMES_BIN=") for part in recorded.calls[0])

    def test_build_argv_carries_env_through_verbatim(self, job) -> None:
        job_id, _directory = job
        argv = launcher.build_systemd_run_argv(
            unit=launcher.unit_name(job_id),
            runtime_max_sec=900,
            memory_max="2G",
            working_directory=Path("/repo"),
            env={"HERMES_HOME": "/home/dr", "HERMES_BIN": "/opt/fake/hermes"},
            argv=["/usr/bin/python3"],
        )
        assert "--setenv=HERMES_BIN=/opt/fake/hermes" in argv
        assert "--setenv=HERMES_HOME=/home/dr" in argv


# ---------------------------------------------------------------------------
# Launch path selection (auto / systemd / fallback)
# ---------------------------------------------------------------------------


class TestLaunchSelection:
    def _launch(
        self,
        job,
        monkeypatch,
        *,
        reachable,
        scopes,
        runner=None,
        spawned=None,
        runner_mode="auto",
    ):
        probes: list[str] = []
        monkeypatch.setattr(
            launcher,
            "manager_reachable",
            lambda scope, *, runner=None: probes.append(scope) or reachable(scope),
        )
        monkeypatch.setattr(launcher, "candidate_scopes", lambda **_kw: scopes)
        popen = fake_popen_spawn({} if spawned is None else spawned)
        return (
            launcher.launch(
                job_id=job[0],
                timeout_minutes=20,
                memory_max="2G",
                hermes_home=job[1].parent.parent,
                log_path=job[1] / "runner.out",
                runner_mode=runner_mode,
                runner=runner or RecordingRunner(),
                popen=popen,
            ),
            probes,
        )

    def test_auto_prefers_the_user_manager(self, job, monkeypatch) -> None:
        spawned: dict = {}
        result, probes = self._launch(
            job, monkeypatch,
            reachable=lambda scope: scope == "user", scopes=["user", "system"],
            runner=RecordingRunner(), spawned=spawned,
        )
        assert result.mode == "systemd" and result.manager_scope == "user"
        assert result.as_status()["runner_scope"] == "user"
        assert probes == ["user"]  # the system manager was never consulted
        assert spawned == {}  # nothing was spawned detached

    def test_auto_root_uses_a_system_service_when_user_manager_is_unusable(self, job, monkeypatch) -> None:
        # The production gateway shape: root service, no user D-Bus session.
        job_id, _directory = job
        recorded = RecordingRunner()
        spawned: dict = {}
        result, probes = self._launch(
            job, monkeypatch,
            reachable=lambda scope: scope == "system", scopes=["user", "system"],
            runner=recorded, spawned=spawned,
        )
        assert result.mode == "systemd" and result.manager_scope == "system"
        assert probes == ["user", "system"]  # user tried first, honestly rejected
        assert result.unit == f"hermes-research-{job_id}"
        run_argv = recorded.calls[0]
        assert run_argv[0] == "systemd-run" and "--user" not in run_argv
        assert "--scope" not in run_argv
        # Same hard bounds as the user path: 20 min budget + documented slack.
        assert "--property=RuntimeMaxSec=1500" in run_argv
        assert "--property=MemoryMax=2G" in run_argv
        tail = run_argv[run_argv.index("--") + 1 :]
        assert "--job" in tail and job_id in tail
        assert "brief" not in " ".join(tail)
        # Nothing ran detached, and the unit PID came from the system manager.
        assert spawned == {}
        assert recorded.calls[1][:3] == ["systemctl", "show", result.unit]
        assert result.as_status()["runner_scope"] == "system"

    def test_auto_falls_back_with_an_honest_reason(self, job, monkeypatch) -> None:
        job_id, directory = job
        spawned: dict = {}
        result, _probes = self._launch(
            job, monkeypatch,
            reachable=lambda scope: False, scopes=["user", "system"],
            spawned=spawned,
        )
        assert result.mode == "fallback" and result.unit is None
        assert result.reason and "user manager unreachable" in result.reason
        assert "system manager unreachable" in result.reason
        status = result.as_status()
        assert status["runner_scope"] == "fallback"
        assert "detached fallback" in status["runner_reason"]
        assert spawned["argv"][spawned["argv"].index("--job") + 1] == job_id

    def test_auto_falls_back_when_systemd_run_itself_fails(self, job, monkeypatch) -> None:
        job_id, directory = job
        recorded = RecordingRunner(results={"systemd-run": (1, "", "Failed to start transient service unit")})
        spawned: dict = {}
        result, _probes = self._launch(
            job, monkeypatch,
            reachable=lambda scope: scope == "user", scopes=["user"],
            runner=recorded, spawned=spawned,
        )
        assert result.mode == "fallback"
        # The downgrade reason names the actual failure, not a guess.
        assert "systemd-run (user manager) failed" in result.reason
        assert "Failed to start transient service unit" in result.reason
        assert spawned["argv"][spawned["argv"].index("--job") + 1] == job_id
        # Detached: the child starts its own session on POSIX.
        assert spawned["kwargs"].get("start_new_session") is True
        # The capture file is created private, never world-readable.
        assert (directory / "runner.out").stat().st_mode & 0o777 == 0o600

    def test_forced_systemd_fails_closed_without_a_manager(self, job, monkeypatch) -> None:
        spawned: dict = {}
        with pytest.raises(launcher.RunnerLaunchError) as excinfo:
            self._launch(
                job, monkeypatch,
                reachable=lambda scope: False, scopes=["user", "system"],
                runner_mode="systemd", spawned=spawned,
            )
        message = str(excinfo.value)
        # Fail closed: never a silent downgrade to the detached fallback.
        assert "forced runner_mode=systemd" in message
        assert "user manager unreachable" in message and "system manager unreachable" in message
        assert spawned == {}

    def test_forced_systemd_fails_closed_when_the_run_fails(self, job, monkeypatch) -> None:
        recorded = RecordingRunner(results={"systemd-run": (1, "", "Failed to start transient service unit")})
        spawned: dict = {}
        with pytest.raises(launcher.RunnerLaunchError) as excinfo:
            self._launch(
                job, monkeypatch,
                reachable=lambda scope: True, scopes=["user", "system"],
                runner=recorded, runner_mode="systemd", spawned=spawned,
            )
        assert "systemd-run (user manager) failed" in str(excinfo.value)
        assert spawned == {}  # no fallback spawn behind a forced mode

    def test_runner_mode_forces_fallback_without_consulting_systemd(self, job, monkeypatch) -> None:
        job_id, directory = job
        # A healthy systemd must be IGNORED when the config says fallback.
        recorded = RecordingRunner()
        spawned: dict = {}
        monkeypatch.setattr(
            launcher, "manager_reachable", lambda *a, **k: pytest.fail("probe must not run")
        )
        result = launcher.launch(
            job_id=job_id,
            timeout_minutes=5,
            memory_max="2G",
            hermes_home=directory.parent.parent,
            log_path=directory / "runner.out",
            runner_mode="fallback",
            runner=recorded,
            popen=fake_popen_spawn(spawned),
        )
        assert result.mode == "fallback"
        assert recorded.calls == []  # systemd-run was never consulted
        assert result.reason == "runner_mode=fallback (configured)"


# ---------------------------------------------------------------------------
# Manager connectivity probe
# ---------------------------------------------------------------------------


class TestManagerProbe:
    def test_probe_requires_binaries(self, monkeypatch) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
        assert launcher.manager_reachable("user") is False
        assert launcher.manager_reachable("system") is False

    def test_probe_believes_a_manager_that_answers(self) -> None:
        # rc 0 = "running"; rc 1 with a real state word still means it answered.
        for rc, out in ((0, "running\n"), (1, "degraded\n"), (1, "starting\n")):
            assert launcher.manager_reachable("user", runner=lambda _a: (rc, out, "")) is True

    def test_probe_rejects_connection_failures(self) -> None:
        # Socket existence is not availability: a root gateway has
        # /run/user/0/bus present but no bus environment to reach it.
        for rc, out, err in (
            (1, "", "Failed to connect to bus: No medium found"),
            (1, "\n", "Failed to connect to bus: $XDG_RUNTIME_DIR is not set"),
            (1, "offline\n", ""),
            (124, "", "timeout"),
        ):
            assert launcher.manager_reachable("user", runner=lambda _a: (rc, out, err)) is False
            assert launcher.manager_reachable("system", runner=lambda _a: (rc, out, err)) is False

    def test_probe_issues_the_real_systemctl_query(self) -> None:
        for scope, expected in (("system", ["systemctl", "is-system-running"]),
                                ("user", ["systemctl", "--user", "is-system-running"])):
            recorded = RecordingRunner(results={"systemctl": (0, "running\n", "")})
            assert launcher.manager_reachable(scope, runner=recorded) is True
            assert recorded.calls == [expected]

    def test_probe_rejects_unknown_scopes(self) -> None:
        assert launcher.manager_reachable("glob") is False

    def test_candidate_scopes_root_and_non_root(self, monkeypatch) -> None:
        monkeypatch.setattr(launcher.os, "getuid", lambda: 0)
        assert launcher.candidate_scopes() == ["user", "system"]
        monkeypatch.setattr(launcher.os, "getuid", lambda: 1000)
        assert launcher.candidate_scopes() == ["user"]
        # Explicit override still wins (used by callers that already know).
        assert launcher.candidate_scopes(root=True) == ["user", "system"]
        assert launcher.candidate_scopes(root=False) == ["user"]


# ---------------------------------------------------------------------------
# Targeted cancel (area 6)
# ---------------------------------------------------------------------------


class TestCancelScoping:
    def test_cancel_user_unit_uses_the_user_manager(self, job: tuple[str, Path]) -> None:
        _job_id, directory = job
        other = "hermes-research-rj_ffffffffffff"
        status = {
            "runner_mode": "systemd",
            "runner_unit": "hermes-research-rj_aaaaaaaaaaaa",
            "runner_scope": "user",
        }
        recorded = RecordingRunner(results={"systemctl": (0, "", "")})
        assert launcher.cancel_runner(status, runner=recorded) is True
        argv = recorded.calls[0]
        assert argv == ["systemctl", "--user", "stop", status["runner_unit"]]
        assert other not in argv
        assert "*" not in " ".join(argv)  # never a glob/pattern
        # Exactly one stop command was issued — no broad sweep.
        assert len(recorded.calls) == 1

    def test_cancel_system_unit_uses_the_system_manager(self, job: tuple[str, Path]) -> None:
        _job_id, _directory = job
        unit = "hermes-research-rj_bbbbbbbbbbbb"
        status = {"runner_mode": "systemd", "runner_unit": unit, "runner_scope": "system"}
        recorded = RecordingRunner(results={"systemctl": (0, "", "")})
        assert launcher.cancel_runner(status, runner=recorded) is True
        # The matching manager for that exact unit — no --user, no pattern.
        assert recorded.calls == [["systemctl", "stop", unit]]

    def test_cancel_legacy_status_defaults_to_the_user_manager(self) -> None:
        unit = "hermes-research-rj_cccccccccccc"
        status = {"runner_mode": "systemd", "runner_unit": unit}  # pre-scope status
        recorded = RecordingRunner(results={"systemctl": (0, "", "")})
        assert launcher.cancel_runner(status, runner=recorded) is True
        assert recorded.calls == [["systemctl", "--user", "stop", unit]]

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

    def test_unit_liveness_queries_the_matching_manager(self, job: tuple[str, Path]) -> None:
        job_id, _ = job
        unit = f"hermes-research-{job_id}"
        user = RecordingRunner(results={"systemctl": (0, "active\n", "")})
        assert launcher.runner_alive(
            {"runner_mode": "systemd", "runner_unit": unit, "runner_scope": "user"}, runner=user
        ) is True
        assert user.calls == [["systemctl", "--user", "is-active", unit]]

        system = RecordingRunner(results={"systemctl": (0, "active\n", "")})
        assert launcher.runner_alive(
            {"runner_mode": "systemd", "runner_unit": unit, "runner_scope": "system"}, runner=system
        ) is True
        assert system.calls == [["systemctl", "is-active", unit]]

    def test_legacy_systemd_status_defaults_to_user_liveness(self, job: tuple[str, Path]) -> None:
        job_id, _ = job
        unit = f"hermes-research-{job_id}"
        recorded = RecordingRunner(results={"systemctl": (3, "inactive\n", "")})
        assert launcher.runner_alive({"runner_mode": "systemd", "runner_unit": unit}, runner=recorded) is False
        assert recorded.calls == [["systemctl", "--user", "is-active", unit]]

    def test_unit_liveness_unknown_is_none(self, job: tuple[str, Path]) -> None:
        job_id, _ = job
        status = {"runner_mode": "systemd", "runner_unit": f"hermes-research-{job_id}"}
        runner = RecordingRunner(results={"systemctl": (1, "garbage", "boom")})
        assert launcher.runner_alive(status, runner=runner) is None

    def test_own_pid_is_alive_and_reuse_guarded(self) -> None:
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], start_new_session=True)
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
