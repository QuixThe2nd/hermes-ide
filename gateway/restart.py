"""Shared gateway restart constants and supervisor detection helpers."""

import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping

from hermes_cli.config import DEFAULT_CONFIG

# EX_TEMPFAIL from sysexits.h — used to ask the service manager to restart
# the gateway after a graceful drain/reload path completes.
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75

# EX_CONFIG from sysexits.h — fatal configuration error (e.g. token
# collision, no messaging platforms).  The s6 finish script translates
# this into exit 125 (permanent failure) so the supervisor stops
# restarting the gateway.  See #51228.
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78


def is_global_startup_conflict(error_code: str | None) -> bool:
    """Return True when an adapter's fatal error is a single-writer ownership conflict.

    ``BasePlatformAdapter._acquire_platform_lock`` emits ``{scope}_lock``
    with ``retryable=True`` on purpose: a *mid-run* reconnect must be able to
    recover once the live holder exits or a stale record is cleared (#54167).
    At startup, though, a live foreign holder is a configuration conflict —
    two gateways cannot poll one bot token — so the startup router must not
    treat that flag as "transient blip, retry-queue forever".  This matches by
    error CODE only (the ``{scope}_lock`` / ``lock_conflict`` families every
    adapter emits for scoped-lock and identity conflicts), never by message
    text.
    """
    code = (error_code or "").strip().lower()
    if not code:
        return False
    return code == "lock_conflict" or code.endswith("_lock")

# Set by ``hermes gateway run --external-supervisor``. Unlike systemd's
# INVOCATION_ID and launchd's XPC_SERVICE_NAME, this survives wrappers that
# intentionally replace the child environment (for example ``sudo env -i``).
EXTERNAL_GATEWAY_SUPERVISOR_ENV = "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_drain_timeout"]
)
DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT = float(
    DEFAULT_CONFIG["gateway"]["signal_interrupt_grace_timeout"]
)
DEFAULT_GATEWAY_POST_INTERRUPT_GRACE_TIMEOUT = 5.0

# LEGACY default for ``agent.restart_after_turn_timeout``. The in-band
# restart (``/restart``, SIGUSR1, self-restart from a child CLI) waits for
# active turns to finish *before* ``stop()`` begins — with NO cap: a
# user-requested restart never forces while work remains active. The value
# (and this constant) are retained only so old configs keep parsing and
# CLI-side observers can size their advisory wait budget
# (``resolve_restart_exit_wait_budget``); nothing may use it as a deadline
# to force a restart. See #77184.
DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_after_turn_timeout"]
)

# Cron-only floor under the ``stop()`` drain. ``restart_drain_timeout``
# defaults to 0 because interrupting a *chat* turn is cheap and recoverable:
# the user is told the gateway is restarting and the session is pre-marked
# resume_pending. An interrupted *cron* run has neither property — nobody is
# waiting on it, it lands in jobs.json as a permanent failure, and a recurring
# job just waits for its next schedule — so a zero-second drain silently
# destroys work. See #82161.
DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["cron_drain_timeout"]
)

# Seconds of the shutdown watchdog leash held back for the work that still has
# to happen after the drain returns: interrupt agents, kill tool subprocesses,
# mark in-flight jobs interrupted, disconnect adapters. Waiting for cron past
# that point trades a job that is killed *and recorded* for one that is
# SIGKILLed mid-write and stays wedged at ``last_status=running`` forever.
CRON_DRAIN_CLEANUP_RESERVE_S = 10.0

# systemd TimeoutStopSec headroom after the stop-path drain budget, and the
# floor used when that budget is still the default immediate (0s) chat drain.
# Keep these in lockstep with generate_systemd_unit() / #94759.
SYSTEMD_STOP_HEADROOM_S = 30.0
SYSTEMD_TIMEOUT_STOP_SEC_FLOOR = 60.0


def is_gateway_supervisor_process(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether this gateway process is owned by a supervisor."""
    env = os.environ if environ is None else environ
    if env.get("INVOCATION_ID"):
        return True
    if env.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    xpc_service = env.get("XPC_SERVICE_NAME", "")
    if xpc_service and xpc_service != "0":
        return True
    return str(env.get(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def detached_restart_spawn_blocked(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether detached restart spawns are forbidden in this process.

    ``HERMES_TEST_ISOLATION`` is the hermetic-conftest marker (see
    ``hermes_state._TEST_ISOLATION_MARKER_ENV``): its presence declares this
    process tree a test run, and a test run must not leave its sandbox by
    spawning detached gateway/restart helpers that outlive the test. Every
    detached launcher consults this predicate before its latch or spawn and
    no-ops with a warning when it holds. Any nonblank value counts — the
    marker's value is the isolation root, not a boolean.
    """
    env = os.environ if environ is None else environ
    return bool(str(env.get("HERMES_TEST_ISOLATION", "")).strip())


# ── standalone detached restart watcher ──────────────────────────────────────
# One watcher contract, shared by POSIX and Windows: wait for the old gateway
# PID to be PROVEN absent — with no deadline — then relaunch the gateway
# through its direct entry point (``sys.executable -m gateway.run`` from the
# project root). The watcher must never shell out to ``hermes gateway
# restart``, ``hermes gateway --replace``, or any service manager: those are
# the legacy CLI paths this in-process restart flow must not enter, and a
# replacement launched while the old PID is merely *thought* gone would race
# the dying process for platform locks and ports.


# Win32 wait/probe result codes for the PID liveness check below. The ONLY
# value that proves a process absent is WAIT_OBJECT_0 (handle signaled, the
# process has exited). WAIT_TIMEOUT means it is running; WAIT_FAILED
# (0xFFFFFFFF) means the wait itself errored — an unknown, which must read
# as LIVE: this predicate's False is the detached watcher's sole authority
# to launch a replacement, so a failed probe returning "absent" would race
# a still-live gateway for its platform locks and ports.
WIN32_WAIT_OBJECT_0 = 0x0
WIN32_WAIT_TIMEOUT = 0x102
WIN32_WAIT_FAILED = 0xFFFFFFFF
# GetLastError() when OpenProcess cannot even address the PID slot — the
# one OpenProcess failure that means "no such process". Everything else
# (ERROR_ACCESS_DENIED 5 on a protected process, ...) is unknown ⇒ live.
WIN32_ERROR_INVALID_PARAMETER = 87


def _win32_wait_result_means_live(result: int) -> bool:
    """Fail-closed reading of ``WaitForSingleObject(handle, 0)``.

    Only ``WAIT_OBJECT_0`` (the handle signaled — the process exited) is
    proof of absence. ``WAIT_TIMEOUT`` is a live process, ``WAIT_FAILED``
    is a failed probe, and any other/unexpected value is ignorance; all of
    them mean "live" to the caller. The old ``== 0x102`` comparison read
    ``WAIT_FAILED`` (and any non-timeout value) as absent — exactly the
    fail-open this function exists to prevent.
    """
    return int(result) != WIN32_WAIT_OBJECT_0


def _windows_pid_alive_fail_closed(pid: int, kernel32) -> bool:
    """Windows arm of :func:`pid_alive_fail_closed`, with kernel32 injected.

    ``kernel32`` is a seam so tests can drive real Win32 result codes
    (``WAIT_FAILED`` included) deterministically on any host — the same
    platform-logic-as-data pattern as ``hidden_windows_child_options``.
    """
    handle = kernel32.OpenProcess(0x1000 | 0x100000, False, int(pid))
    if not handle:
        # ERROR_INVALID_PARAMETER (87): the PID slot cannot even be
        # addressed — the process is gone. Anything else (access denied
        # on a protected process, ...) is unknown, i.e. still live.
        return int(kernel32.GetLastError()) != WIN32_ERROR_INVALID_PARAMETER
    try:
        return _win32_wait_result_means_live(
            kernel32.WaitForSingleObject(handle, 0)
        )
    finally:
        kernel32.CloseHandle(handle)


def pid_alive_fail_closed(pid: int) -> bool:
    """Return whether *pid* is live — or cannot be proven absent.

    The watcher's only authority to launch a replacement is this returning
    False, so every unknown reads as live: ``ProcessLookupError`` (ESRCH) is
    the single "absent" answer, ``PermissionError`` (EPERM) means the process
    exists under another credentials context, and any other ``OSError`` from
    the probe is ignorance, never proof of death. Elapsed time is never
    consulted — the caller has no deadline to expire.

    On Windows ``os.kill(pid, 0)`` is not a liveness no-op (it maps to
    ``GenerateConsoleCtrlEvent``, bpo-14484), so the check goes through the
    Win32 handle-based existence probe instead, with the same fail-closed
    reading of its errors: only ``WAIT_OBJECT_0`` proves absence;
    ``WAIT_TIMEOUT``, ``WAIT_FAILED``, and every other result stay live.
    """
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.restype = ctypes.c_uint
        k32.GetLastError.restype = ctypes.c_uint
        return _windows_pid_alive_fail_closed(pid, k32)
    try:
        os.kill(int(pid), 0)  # windows-footgun: ok — POSIX-only branch; the os.name == "nt" branch above returns through the Win32 handle probe before reaching this line (bpo-14484)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def wait_for_pid_exit(
    pid: int,
    *,
    poll_s: float = 0.2,
    alive=None,
    sleep=None,
) -> None:
    """Block until *pid* is proven absent — with no deadline, ever.

    Elapsed time never permits the wait to end while the PID is live or its
    state is unknown: the loop runs until :func:`pid_alive_fail_closed`
    returns False, however long that takes. ``alive``/``sleep`` are
    dependency-injection seams for deterministic tests (they default to the
    real probe and ``time.sleep``).
    """
    if alive is None:
        alive = pid_alive_fail_closed
    if sleep is None:
        sleep = time.sleep
    while alive(pid):
        sleep(poll_s)


def spawn_replacement_gateway(
    project_root: str,
    *,
    popen=subprocess.Popen,
):
    """Launch the replacement gateway directly: ``sys.executable -m
    gateway.run`` from *project_root*.

    ``-m`` puts the working directory at the head of ``sys.path``, so the
    repository's ``gateway.run`` entry point resolves without touching the
    ``hermes`` CLI, service managers, or anything else that could route this
    back into a legacy restart path. The child inherits the watcher's
    environment — already built without ``_HERMES_GATEWAY`` — and detaches
    per platform through the existing safe detach helpers (a new session on
    POSIX, the no-breakaway creation flags on Windows).
    """
    argv = [sys.executable, "-m", "gateway.run"]
    kwargs: dict = {
        "cwd": project_root,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        from hermes_cli._subprocess_compat import (
            windows_detach_flags_without_breakaway,
        )

        kwargs["creationflags"] = windows_detach_flags_without_breakaway()
    else:
        kwargs["start_new_session"] = True
    return popen(argv, **kwargs)


def run_detached_restart_watcher(
    pid: int,
    project_root: str,
    *,
    poll_s: float = 0.2,
) -> None:
    """The standalone detached watcher's body: wait out the old PID, replace.

    Runs in its own detached process (spawned by
    ``GatewayRunner._launch_detached_restart_watcher`` via a tiny ``-c``
    bootstrap that imports this function). It returns only after the exact
    old PID was proven absent and the replacement gateway was launched.
    """
    wait_for_pid_exit(pid, poll_s=poll_s)
    spawn_replacement_gateway(project_root)


def is_container_restart_context() -> bool:
    """Return whether the gateway is running inside a container for restart
    routing purposes (Docker/Podman ⇒ the detached setsid path dies with the
    cgroup; exit-75 service restart is the only viable path).

    Extracted from the inline probe in the /restart handler so tests can mock
    container detection hermetically — a real ``/.dockerenv`` on a
    containerized CI runner otherwise flips the routing under the test.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def user_restart_via_service() -> bool:
    """Route a user-initiated restart (``/restart``, the ``restart`` tool) via
    the service-manager exit path?

    True when a service manager (systemd/launchd/s6) supervises the gateway or
    the gateway runs inside a Docker/Podman container: exit with code 75 so the
    supervisor / container restart policy relaunches us. The detached
    subprocess approach (setsid + bash) doesn't work under systemd (KillMode
    kills the cgroup) or Docker (tini exits when the gateway dies, taking the
    detached helper with it).
    """
    return is_gateway_supervisor_process() or is_container_restart_context()


def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout, falling back to the shared default."""
    try:
        value = float(raw) if str(raw or "").strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)


def parse_restart_after_turn_timeout(raw: object) -> float:
    """Parse the legacy after-turn value, falling back to the default.

    Kept for backward compatibility so existing configs (including ``0``,
    the legacy immediate-drain setting) keep loading. The value is
    NON-AUTHORITATIVE for restart progress: the in-band restart wait is
    unbounded, and no value — ``0`` included — may act as a deadline that
    advances a restart into ``stop()`` while active work remains.
    """
    if raw is None:
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    return max(0.0, value)


def parse_cron_drain_timeout(raw: object) -> float:
    """Parse the cron-only drain floor, falling back to the shared default.

    ``0`` is a deliberate opt-out — cron work is then interrupted on the same
    budget as chat work, the pre-#82161 behaviour — and must not fall through
    to the default, unlike empty/missing input.
    """
    if raw is None:
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    return max(0.0, value)


def resolve_cron_drain_budget(
    drain_timeout: float,
    cron_drain_timeout: float,
    *,
    watchdog_delay: float,
    elapsed: float = 0.0,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
) -> float:
    """Seconds the shutdown drain may spend waiting on in-flight cron work.

    The configured floor is clamped to what this process can actually honour.
    The shutdown watchdog hard-exits at ``watchdog_delay`` and the service
    manager's ``TimeoutStopSec`` is sized from the full stop budget (drain
    vs cron floor + cleanup reserve, plus headroom — see
    ``resolve_systemd_timeout_stop_sec``), so waiting past that leash
    (minus ``cleanup_reserve_s`` for the teardown that follows the drain)
    would swap a cleanly-interrupted job for a SIGKILL that leaves it
    wedged mid-run — strictly worse than the bug being fixed.

    Never returns less than ``drain_timeout``: the cron floor only ever
    extends the wait, so an operator who deliberately configured a long
    ``restart_drain_timeout`` keeps it.
    """

    def _seconds(value: object, fallback: float = 0.0) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback

    drain = _seconds(drain_timeout)
    floor = _seconds(cron_drain_timeout)
    if floor <= 0.0:
        return drain
    ceiling = (
        _seconds(watchdog_delay)
        - _seconds(elapsed)
        - _seconds(cleanup_reserve_s, CRON_DRAIN_CLEANUP_RESERVE_S)
    )
    return max(drain, min(floor, ceiling))


def resolve_systemd_timeout_stop_sec(
    drain_timeout: float,
    cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
    *,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
    headroom_s: float = SYSTEMD_STOP_HEADROOM_S,
    floor_s: float = SYSTEMD_TIMEOUT_STOP_SEC_FLOOR,
) -> int:
    """Seconds systemd ``TimeoutStopSec`` must cover the full stop budget.

    ``restart_drain_timeout`` is only the chat-turn interrupt budget (default
    0). The stop path may wait longer for in-flight cron work —
    ``cron_drain_timeout`` plus ``cleanup_reserve_s`` — before it even starts
    interrupting. Sizing the unit from drain alone lets systemd SIGKILL an
    in-budget drain (#94759).

    A zero ``cron_drain_timeout`` is a deliberate opt-out and does not extend
    the budget. Non-numeric inputs degrade to 0 rather than raising.
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    drain = _seconds(drain_timeout)
    cron = _seconds(cron_drain_timeout)
    reserve = _seconds(cleanup_reserve_s)
    headroom = _seconds(headroom_s)
    floor = _seconds(floor_s)
    cron_budget = (cron + reserve) if cron > 0.0 else 0.0
    stop_budget = max(drain, cron_budget)
    return int(max(floor, stop_budget + headroom))


def resolve_restart_exit_wait_budget(
    drain_timeout: float,
    after_turn_timeout: float,
    *,
    headroom: float = 15.0,
) -> float:
    """Seconds a CLI should *observe* a gateway it asked to restart (SIGUSR1).

    In-band restart defers ``stop()`` until active work finishes — an
    unbounded wait — and then spends up to ``drain_timeout`` inside
    ``stop()``. This budget is advisory only: it sizes how long a CLI
    observer watches before detaching with a clear message. Expiry is never
    authority to force the old process (``systemctl restart``,
    ``launchctl kickstart -k``, SIGTERM, SIGKILL) — only a process proven
    already dead may be recovered (#77184).
    """
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        after_turn = max(float(after_turn_timeout), 0.0)
    except (TypeError, ValueError):
        after_turn = 0.0
    try:
        margin = max(float(headroom), 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    return drain + after_turn + margin


def parse_signal_interrupt_grace_timeout(raw: object) -> float:
    """Parse the unexpected-signal post-interrupt grace timeout."""
    try:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            value = DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
        else:
            value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
    if not math.isfinite(value):
        return DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
    return max(0.0, value)
