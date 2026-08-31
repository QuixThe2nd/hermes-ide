"""Durable runner launching for research jobs.

Two modes:

``systemd``
    A transient **user service** (``systemd-run --user --unit=…``) with bounded
    ``RuntimeMaxSec`` and ``MemoryMax``. Deliberately NOT ``--scope``: a scope
    would keep the runner inside the gateway's cgroup, so a gateway restart
    would kill the job. A transient service owns its own cgroup and survives.

``fallback``
    A detached ``Popen`` (``start_new_session=True`` on POSIX,
    ``windows_detach_popen_kwargs()`` on Windows). Honest reduced durability:
    it survives gateway *process* exit, but not a cgroup-wide supervisor stop.
    The mode is recorded in ``status.json`` so ``status``/``result`` can say so.

All commands are argv lists. Nothing user-controlled is ever interpolated into
a command line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from plugins.deep_research.jobs import DIR_MODE as JOBS_DIR_MODE
from plugins.deep_research.jobs import FILE_MODE as JOBS_FILE_MODE

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]

UNIT_PREFIX = "hermes-research"
# Slack on top of the operator's job budget so synthesis and publish fit inside
# the unit's hard RuntimeMaxSec.
RUNTIME_SLACK_SECONDS = 300

LAUNCH_MODE_SYSTEMD = "systemd"
LAUNCH_MODE_FALLBACK = "fallback"


def default_runner(args: Sequence[str]) -> Tuple[int, str, str]:
    """Run a command, returning ``(rc, stdout, stderr)``. Never raises."""
    try:
        proc = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def repo_root() -> Path:
    """The Hermes checkout this plugin ships in (``python -m`` needs it on sys.path)."""
    return Path(__file__).resolve().parents[2]


def resolve_worker_argv() -> List[str]:
    """The Hermes CLI used for worker sessions.

    ``$HERMES_BIN`` is the established override seam (same resolution order as
    the kanban dispatcher) and is what the integration test uses to point the
    runner at a fake researcher executable.
    """
    override = os.environ.get("HERMES_BIN")
    if override:
        return [override]
    which = shutil.which("hermes")
    if which:
        return [which]
    return [sys.executable, "-m", "hermes_cli.main"]


def runner_argv(job_id: str, hermes_home: Path) -> List[str]:
    """argv of the durable job runner (never contains user content)."""
    return [
        sys.executable,
        "-m",
        "plugins.deep_research.runner",
        "--job",
        job_id,
        "--hermes-home",
        str(hermes_home),
    ]


def unit_name(job_id: str) -> str:
    from plugins.deep_research.jobs import is_canonical_job_id  # local: stable under reshuffles

    if not is_canonical_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return f"{UNIT_PREFIX}-{job_id}"


@dataclass
class LaunchResult:
    mode: str
    unit: Optional[str]
    pid: Optional[int]
    pid_start: Optional[int]

    def as_status(self) -> Dict[str, Optional]:
        return {
            "runner_mode": self.mode,
            "runner_unit": self.unit,
            "runner_pid": self.pid,
            "runner_pid_start": self.pid_start,
        }


# ---------------------------------------------------------------------------
# systemd availability
# ---------------------------------------------------------------------------


def systemd_user_available(*, runner: Runner = default_runner) -> bool:
    """True when a systemd **user** manager can accept transient units.

    ``shutil.which`` alone is insufficient — under a system service there may be
    no user D-Bus session, so every ``systemd-run --user`` would fail. Probe the
    user manager's control socket (either the session bus or systemd's private
    socket) the way ``hermes_cli/gateway.py`` does.
    """
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    if sys.platform == "win32" or not hasattr(os, "getuid"):
        return False
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"  # windows-footgun: ok — POSIX probe
    runtime = Path(xdg)
    for candidate in (runtime / "bus", runtime / "systemd" / "private"):
        try:
            if candidate.exists():
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Transient user service
# ---------------------------------------------------------------------------


def build_systemd_run_argv(
    *,
    unit: str,
    runtime_max_sec: int,
    memory_max: str,
    working_directory: Path,
    env: Dict[str, str],
    argv: Sequence[str],
    systemd_run: str = "systemd-run",
) -> List[str]:
    """Exact ``systemd-run`` argv for a transient user service.

    Pure function — the exact-argv test asserts against this list.
    """
    cmd: List[str] = [systemd_run, "--user"]
    cmd.extend(
        [
            f"--unit={unit}",
            "--collect",
            f"--property=RuntimeMaxSec={int(runtime_max_sec)}",
            f"--property=MemoryMax={memory_max}",
            "--property=OOMScoreAdjust=500",
            f"--working-directory={working_directory}",
        ]
    )
    for key in sorted(env):
        cmd.append(f"--setenv={key}={env[key]}")
    cmd.append("--")
    cmd.extend(str(part) for part in argv)
    return cmd


def systemd_launch(
    *,
    job_id: str,
    runtime_max_sec: int,
    memory_max: str,
    hermes_home: Path,
    runner: Runner = default_runner,
) -> Optional[LaunchResult]:
    """Start the runner as a transient user service. ``None`` if it failed."""
    env = {
        "HERMES_HOME": str(hermes_home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    cmd = build_systemd_run_argv(
        unit=unit_name(job_id),
        runtime_max_sec=runtime_max_sec,
        memory_max=memory_max,
        working_directory=repo_root(),
        env=env,
        argv=runner_argv(job_id, hermes_home),
    )
    rc, _out, err = runner(cmd)
    if rc != 0:
        return None
    unit = unit_name(job_id)
    pid = _unit_main_pid(unit, runner=runner)
    return LaunchResult(
        mode=LAUNCH_MODE_SYSTEMD,
        unit=unit,
        pid=pid,
        pid_start=pid_start_time(pid) if pid else None,
    )


def _unit_main_pid(unit: str, *, runner: Runner = default_runner) -> Optional[int]:
    rc, out, _err = runner(["systemctl", "--user", "show", unit, "--property=MainPID", "--value"])
    if rc != 0:
        return None
    value = (out or "").strip().splitlines()[0].strip() if out else ""
    return int(value) if value.isdigit() else None


def unit_active(unit: str, *, runner: Runner = default_runner) -> Optional[str]:
    """``active``/``activating``/``inactive``/… or ``None`` when unknowable."""
    rc, out, _err = runner(["systemctl", "--user", "is-active", unit])
    if rc == 0:
        return "active"
    state = (out or "").strip().lower()
    # Unknown unit: systemd prints "inactive" with rc 3 for a --collect unit
    # that already exited — that is a *known* dead state, not an error.
    if state in ("active", "activating", "inactive", "failed", "deactivating"):
        return state
    return None


# ---------------------------------------------------------------------------
# Fallback (detached Popen)
# ---------------------------------------------------------------------------


def fallback_launch(
    *,
    job_id: str,
    hermes_home: Path,
    log_path: Path,
    env: Optional[Dict[str, str]] = None,
    popen: Optional[Callable[..., "subprocess.Popen"]] = None,
) -> LaunchResult:
    """Detached runner spawn for hosts without a usable user systemd."""
    from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

    child_env = dict(os.environ if env is None else env)
    child_env["HERMES_HOME"] = str(hermes_home)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    child_env.pop("HERMES_TUI", None)

    log_path.parent.mkdir(mode=JOBS_DIR_MODE, parents=True, exist_ok=True)
    argv = runner_argv(job_id, hermes_home)
    spawn = popen or subprocess.Popen
    # O_CREAT with an explicit 0o600 so the capture file is never world-readable
    # even in the instant between creation and chmod.
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, JOBS_FILE_MODE)
    try:
        proc = spawn(  # noqa: S603 — fixed argv list, no shell
            argv,
            cwd=str(repo_root()),
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            env=child_env,
            **windows_detach_popen_kwargs(),
        )
    finally:
        # The child holds its own dup of the descriptor; drop ours immediately.
        os.close(fd)
    return LaunchResult(
        mode=LAUNCH_MODE_FALLBACK,
        unit=None,
        pid=proc.pid,
        pid_start=pid_start_time(proc.pid),
    )


# ---------------------------------------------------------------------------
# Launch / cancel / liveness
# ---------------------------------------------------------------------------


def launch(
    *,
    job_id: str,
    timeout_minutes: int,
    memory_max: str,
    hermes_home: Path,
    log_path: Path,
    runner_mode: str = "auto",
    runner: Runner = default_runner,
    popen: Optional[Callable[..., "subprocess.Popen"]] = None,
) -> LaunchResult:
    """Start the durable runner, preferring systemd and falling back detached."""
    runtime_max_sec = timeout_minutes * 60 + RUNTIME_SLACK_SECONDS
    if runner_mode != LAUNCH_MODE_FALLBACK and systemd_user_available(runner=runner):
        result = systemd_launch(
            job_id=job_id,
            runtime_max_sec=runtime_max_sec,
            memory_max=memory_max,
            hermes_home=hermes_home,
            runner=runner,
        )
        if result is not None:
            return result
    return fallback_launch(
        job_id=job_id, hermes_home=hermes_home, log_path=log_path, popen=popen
    )


def cancel_runner(status: Dict[str, Any], *, runner: Runner = default_runner) -> bool:
    """Stop exactly this job's runner and its descendants.

    systemd: ``systemctl --user stop <unit>`` reaps the whole cgroup (including
    double-forked descendants) — scoped to this job's unit only. Fallback: a
    process-tree kill of the recorded PID, guarded against PID reuse. Never a
    broad kill of other Hermes processes.
    """
    unit = status.get("runner_unit")
    if status.get("runner_mode") == LAUNCH_MODE_SYSTEMD and unit:
        rc, _out, _err = runner(["systemctl", "--user", "stop", str(unit)])
        return rc == 0

    pid = status.get("runner_pid")
    if isinstance(pid, int) and pid > 0:
        return _kill_tree(pid, expected_start=status.get("runner_pid_start"))
    return False


def runner_alive(status: Dict[str, Any], *, runner: Runner = default_runner) -> Optional[bool]:
    """Is this job's runner still running? ``None`` when it cannot be determined."""
    if status.get("runner_mode") == LAUNCH_MODE_SYSTEMD:
        unit = status.get("runner_unit")
        if not unit:
            return None
        state = unit_active(str(unit), runner=runner)
        if state is None:
            return None
        return state in ("active", "activating")

    pid = status.get("runner_pid")
    if isinstance(pid, int) and pid > 0:
        return _pid_alive(pid, expected_start=status.get("runner_pid_start"))
    return None


# ---------------------------------------------------------------------------
# PID helpers (reuse-guarded)
# ---------------------------------------------------------------------------


def pid_start_time(pid: int) -> Optional[int]:
    """Linux boot-relative start time of ``pid`` — the PID-reuse guard."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:  # windows-footgun: ok — POSIX guard below
            fields = handle.read().rsplit(") ", 1)[-1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int, *, expected_start: Any = None) -> bool:
    if expected_start is not None:
        current = pid_start_time(pid)
        if current is None or current != expected_start:
            return False  # dead, or the number was recycled onto another process
    try:
        os.kill(pid, 0)  # windows-footgun: ok — liveness probe inside a POSIX-only branch
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _kill_tree(pid: int, *, expected_start: Any = None) -> bool:
    """Terminate ``pid`` and its descendants. Ported shape of the repo's tree-kill."""
    if not _pid_alive(pid, expected_start=expected_start):
        return True
    try:
        import psutil

        parent = psutil.Process(pid)
        try:
            targets = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            targets = []
        targets.append(parent)
        import signal

        for proc in targets:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        gone, alive = psutil.wait_procs(targets, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return True
    except ImportError:
        pass
    except OSError:
        return False
    # No psutil: best-effort single-process signal.
    try:
        import signal

        os.kill(pid, signal.SIGTERM)  # windows-footgun: ok — fallback path with psutil absent
        return True
    except (OSError, ProcessLookupError):
        return False
