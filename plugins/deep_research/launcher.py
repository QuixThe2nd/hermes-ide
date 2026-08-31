"""Durable runner launching for research jobs.

``runner_mode: auto`` picks the first launch path that actually works on this
host, in this order:

user service
    A transient **user service** (``systemd-run --user --unit=…``) with bounded
    ``RuntimeMaxSec`` and ``MemoryMax``. Deliberately NOT ``--scope``: a scope
    would keep the runner inside the gateway's cgroup, so a gateway restart
    would kill the job. A transient service owns its own cgroup and survives.

system service
    When the process runs as root (the usual gateway deployment: a root-owned
    ``hermes-gateway.service`` in system.slice with ``KillMode=mixed``), the
    gateway environment often has no user D-Bus session at all — no
    ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS``, so ``systemd-run --user``
    cannot reach a user manager even when ``/run/user/<uid>/bus`` exists. In
    that case a transient **system service** (``systemd-run`` without
    ``--user``) is used instead: it lives outside the gateway's cgroup, so the
    job survives a gateway restart, and it needs no user session.

fallback
    A detached ``Popen`` (``start_new_session=True`` on POSIX,
    ``windows_detach_popen_kwargs()`` on Windows). Honest reduced durability:
    it survives gateway *process* exit, but not a cgroup-wide supervisor stop
    (exactly what kills it under the root gateway service described above).
    The downgrade and its reason are recorded in ``status.json``.

``runner_mode: systemd`` requires a transient service (either manager scope)
and fails closed — it never silently downgrades to the fallback.

Which manager scope was used is recorded per job (``runner_scope``:
``user``/``system``/``fallback``) so cancel/liveness always address the exact
unit with the matching ``systemctl`` invocation.

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

# Which systemd manager owns the transient unit.
MANAGER_SCOPE_USER = "user"
MANAGER_SCOPE_SYSTEM = "system"
MANAGER_SCOPES = frozenset({MANAGER_SCOPE_USER, MANAGER_SCOPE_SYSTEM})

# A manager probe must answer quickly, not block a `start` for a minute.
PROBE_TIMEOUT_SECONDS = 10

# ``is-system-running`` exit states that still mean "the manager answered".
# Anything else (no output, a bus error, "offline") means unusable.
_MANAGER_ANSWERED_STATES = frozenset(
    {"running", "degraded", "initializing", "starting", "stopping", "maintenance"}
)


class RunnerLaunchError(RuntimeError):
    """The configured runner mode cannot launch. Fail closed — no downgrade."""


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


def probe_runner(args: Sequence[str]) -> Tuple[int, str, str]:
    """Like :func:`default_runner` but bounded short, for manager probes."""
    try:
        proc = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
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
    unit: Optional[str] = None
    pid: Optional[int] = None
    pid_start: Optional[int] = None
    manager_scope: Optional[str] = None
    reason: Optional[str] = None

    def as_status(self) -> Dict[str, Optional]:
        info: Dict[str, Optional] = {
            "runner_mode": self.mode,
            "runner_unit": self.unit,
            "runner_pid": self.pid,
            "runner_pid_start": self.pid_start,
            # The manager that actually owns the runner: user | system | fallback.
            "runner_scope": self.manager_scope or LAUNCH_MODE_FALLBACK,
        }
        if self.reason:
            info["runner_reason"] = self.reason
        return info


# ---------------------------------------------------------------------------
# Manager scopes
# ---------------------------------------------------------------------------


def systemctl_argv(scope: str, *verbs: Any) -> List[str]:
    """Exact ``systemctl`` argv for one manager scope. Never a pattern."""
    if scope not in MANAGER_SCOPES:
        raise ValueError(f"unknown manager scope: {scope!r}")
    prefix = ["systemctl", "--user"] if scope == MANAGER_SCOPE_USER else ["systemctl"]
    return prefix + [str(verb) for verb in verbs]


def manager_reachable(scope: str, *, runner: Optional[Runner] = None) -> bool:
    """True when this scope's service manager answers a real status query.

    A socket existing proves nothing: under a root system-service gateway the
    user sockets can be present while ``systemd-run --user`` has no bus to talk
    to (no ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` in the environment).
    The probe issues the same ``systemctl`` query the launcher would use, bounded
    short, and believes only a real answer from the manager itself.
    """
    if scope not in MANAGER_SCOPES:
        return False
    if sys.platform == "win32" or not hasattr(os, "getuid"):
        return False
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    rc, out, _err = (runner or probe_runner)(systemctl_argv(scope, "is-system-running"))
    if rc == 0:
        return True
    # A non-zero exit that still printed a state word ("degraded", "starting",
    # …) means the manager answered. Connection failures print no state word.
    return (out or "").strip().lower() in _MANAGER_ANSWERED_STATES


def candidate_scopes(*, root: Optional[bool] = None) -> List[str]:
    """Manager scopes to try for a transient service, in order.

    The user manager first — same privileges as the gateway, no root units. The
    system manager only when actually running as root: a non-root system
    transient service needs polkit rights the launcher cannot assume, while a
    root gateway's detached fallback child would be killed by the gateway
    unit's cgroup cleanup on restart.
    """
    if root is None:
        root = hasattr(os, "getuid") and os.getuid() == 0  # windows-footgun: ok — guarded probe
    scopes = [MANAGER_SCOPE_USER]
    if root:
        scopes.append(MANAGER_SCOPE_SYSTEM)
    return scopes


def manager_scope_of(status: Dict[str, Any]) -> str:
    """The manager scope a recorded systemd runner was launched under.

    Statuses written before scopes existed default to the user manager.
    """
    scope = status.get("runner_scope")
    return scope if scope in MANAGER_SCOPES else MANAGER_SCOPE_USER


def _first_line(text: str, limit: int = 200) -> str:
    """Bounded first line of a command's stderr, for status/launch reasons."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()[:limit]


# ---------------------------------------------------------------------------
# Transient service (user or system manager)
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
    scope: str = MANAGER_SCOPE_USER,
) -> List[str]:
    """Exact ``systemd-run`` argv for a transient service in ``scope``.

    Pure function — the exact-argv test asserts against this list.
    """
    if scope not in MANAGER_SCOPES:
        raise ValueError(f"unknown manager scope: {scope!r}")
    cmd: List[str] = [systemd_run]
    if scope == MANAGER_SCOPE_USER:
        cmd.append("--user")
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
    scope: str,
    runtime_max_sec: int,
    memory_max: str,
    hermes_home: Path,
    runner: Runner = default_runner,
) -> LaunchResult:
    """Start the runner as a transient service in ``scope``'s manager.

    Raises :class:`RunnerLaunchError` when the manager refuses, so the caller
    decides whether to try the next scope, fall back, or fail closed — a
    silently-dead runner is never reported as launched.
    """
    env = {
        "HERMES_HOME": str(hermes_home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    unit = unit_name(job_id)
    cmd = build_systemd_run_argv(
        unit=unit,
        scope=scope,
        runtime_max_sec=runtime_max_sec,
        memory_max=memory_max,
        working_directory=repo_root(),
        env=env,
        argv=runner_argv(job_id, hermes_home),
    )
    rc, _out, err = runner(cmd)
    if rc != 0:
        detail = _first_line(err) or f"exit {rc}"
        raise RunnerLaunchError(f"systemd-run ({scope} manager) failed: {detail}")
    pid = _unit_main_pid(unit, scope=scope, runner=runner)
    return LaunchResult(
        mode=LAUNCH_MODE_SYSTEMD,
        unit=unit,
        pid=pid,
        pid_start=pid_start_time(pid) if pid else None,
        manager_scope=scope,
    )


def _unit_main_pid(
    unit: str, *, scope: str = MANAGER_SCOPE_USER, runner: Runner = default_runner
) -> Optional[int]:
    rc, out, _err = runner(
        systemctl_argv(scope, "show", unit, "--property=MainPID", "--value")
    )
    if rc != 0:
        return None
    value = (out or "").strip().splitlines()[0].strip() if out else ""
    return int(value) if value.isdigit() else None


def unit_active(
    unit: str, *, scope: str = MANAGER_SCOPE_USER, runner: Runner = default_runner
) -> Optional[str]:
    """``active``/``activating``/``inactive``/… or ``None`` when unknowable."""
    rc, out, _err = runner(systemctl_argv(scope, "is-active", unit))
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
    reason: Optional[str] = None,
) -> LaunchResult:
    """Detached runner spawn for hosts without a usable transient service."""
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
        reason=reason,
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
    probe: Optional[Runner] = None,
) -> LaunchResult:
    """Start the durable runner according to ``runner_mode``.

    ``auto`` tries a transient user service first, then (when running as root)
    a transient system service outside the gateway's cgroup, and only then an
    honest detached fallback — recording why it downgraded. ``systemd`` requires
    a transient service and raises :class:`RunnerLaunchError` (fail closed,
    never a silent downgrade). ``fallback`` never consults systemd at all.
    """
    runtime_max_sec = timeout_minutes * 60 + RUNTIME_SLACK_SECONDS
    probe = probe or probe_runner

    if runner_mode == LAUNCH_MODE_FALLBACK:
        return fallback_launch(
            job_id=job_id,
            hermes_home=hermes_home,
            log_path=log_path,
            popen=popen,
            reason="runner_mode=fallback (configured)",
        )

    attempts: List[str] = []
    for scope in candidate_scopes():
        if not manager_reachable(scope, runner=probe):
            attempts.append(f"{scope} manager unreachable")
            continue
        try:
            return systemd_launch(
                job_id=job_id,
                scope=scope,
                runtime_max_sec=runtime_max_sec,
                memory_max=memory_max,
                hermes_home=hermes_home,
                runner=runner,
            )
        except RunnerLaunchError as exc:
            attempts.append(str(exc))
    detail = "; ".join(attempts) or "no service manager candidate for this process"

    if runner_mode == LAUNCH_MODE_SYSTEMD:
        # A forced systemd mode means a transient service is REQUIRED.
        raise RunnerLaunchError(
            f"forced runner_mode=systemd could not launch a transient service: {detail}"
        )

    return fallback_launch(
        job_id=job_id,
        hermes_home=hermes_home,
        log_path=log_path,
        popen=popen,
        reason=f"no usable systemd service manager ({detail}); using detached fallback",
    )


def cancel_runner(status: Dict[str, Any], *, runner: Runner = default_runner) -> bool:
    """Stop exactly this job's runner and its descendants.

    systemd: ``systemctl [--user] stop <unit>`` with the manager scope recorded
    at launch, reaping that unit's whole cgroup — scoped to this job's unit
    only, never a pattern or a broad sweep. Fallback: a process-tree kill of
    the recorded PID, guarded against PID reuse.
    """
    unit = status.get("runner_unit")
    if status.get("runner_mode") == LAUNCH_MODE_SYSTEMD and unit:
        rc, _out, _err = runner(systemctl_argv(manager_scope_of(status), "stop", str(unit)))
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
        state = unit_active(str(unit), scope=manager_scope_of(status), runner=runner)
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
