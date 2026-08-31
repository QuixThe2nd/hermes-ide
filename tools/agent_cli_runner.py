#!/usr/bin/env python3
"""Shared runner for external coding-agent CLIs (Cursor Agent, claude-glm).

One implementation of spawn / stream-to-log / watchdog used by
``delegate_cursor_agent``, ``delegate_claude_agent``, and the dev-pipeline
executor's attempt runner — so behavior (timeouts, stall detection,
process-group cleanup) cannot drift between callers.

The runner owns: subprocess spawn in a new session, a reader thread teeing
stdout to a JSONL log, a monitor loop with interrupt + optional wall-clock
timeout + opt-in stall watchdog, and process-group termination. Callers own:
binary resolution, argv construction, environment construction, and log
parsing.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

from tools.environments.local import build_subprocess_env

logger = logging.getLogger(__name__)

# Process-group signalling is POSIX-only. On Windows we degrade to
# proc.terminate()/proc.kill() (see _terminate_process), so keep the
# killpg paths behind a capability flag and use a SIGKILL fallback that
# exists at import time on every platform.
_KILLPG_SUPPORTED = hasattr(os, "killpg")
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

_MONITOR_POLL_SECONDS = 0.1
_TERMINATE_GRACE_SECONDS = 2.0


def _check_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return is_interrupted()
    except Exception:
        return False


def _signal_process_group(proc: subprocess.Popen, sig: signal.Signals) -> bool:
    if not _KILLPG_SUPPORTED:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), sig)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        return True
    except (OSError, ProcessLookupError, AttributeError):
        return False


def _signal_pgid(pgid: int, sig: signal.Signals) -> bool:
    if not _KILLPG_SUPPORTED:
        return False
    try:
        os.killpg(pgid, sig)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        return True
    except (OSError, ProcessLookupError):
        return False


def _wait_pgid_reap(pgid: int, timeout: float) -> None:
    if not _KILLPG_SUPPORTED:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # windows-footgun: ok — guarded by _KILLPG_SUPPORTED
        except ProcessLookupError:
            return
        except OSError as exc:
            if getattr(exc, "errno", None) == 3:
                return
            return
        time.sleep(0.05)


def _terminate_process(proc: subprocess.Popen, pgid: Optional[int] = None) -> None:
    if pgid is not None:
        if not _signal_pgid(pgid, signal.SIGTERM):
            try:
                proc.terminate()
            except Exception:
                pass
    elif not _signal_process_group(proc, signal.SIGTERM):
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except Exception:
        pass

    if pgid is not None:
        _signal_pgid(pgid, _SIGKILL)
        _wait_pgid_reap(pgid, _TERMINATE_GRACE_SECONDS)
    elif not _signal_process_group(proc, _SIGKILL):
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except Exception:
        pass


def _read_log_text(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def _default_subprocess_env() -> dict:
    """Legacy env for interactive delegate-tool runs.

    The agent wrapper scripts run with `set -u` and die on unbound $HOME in
    bare environments (transient systemd units, cron). Guarantee HOME so any
    sparse-env caller works, and prepend ~/.local/bin so binary resolution
    stays consistent when PATH is minimal. No credentials are injected here —
    wrappers pull them from their own config/secret files at runtime.
    scrub_secrets=False + inherit_profile_home=False preserves exact legacy
    os.environ.copy() behavior while routing through the single env factory.
    """
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    if not env.get("HOME"):
        env["HOME"] = str(Path.home())
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env


def run_agent_cli(
    cmd: List[str],
    *,
    workdir: str,
    timeout_seconds: int = 0,
    stall_watchdog_seconds: float = 0.0,
    log_dir: Optional[Path] = None,
    run_timestamp: Optional[str] = None,
    log_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    on_spawn: Optional[Callable[[Path], None]] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
) -> Tuple[Optional[str], str, str, float, Optional[int]]:
    """Spawn the agent, stream stdout to a log file, enforce watchdogs.

    ``timeout_seconds <= 0`` disables the wall-clock limit. The stall
    watchdog (no stdout growth for ``stall_watchdog_seconds``) is opt-in
    and off by default: ``stall_watchdog_seconds <= 0`` disables it
    entirely, so a quiet-but-healthy child runs until it exits or an
    explicit wall-clock limit fires.

    The log path is either explicit (``log_path`` — used by the dev-pipeline
    executor, which needs a known path to tail mid-run) or computed as
    ``log_dir / f"{run_timestamp}-{pid}.jsonl"`` (the delegate tools' scheme).

    ``env=None`` uses the legacy interactive default (see
    ``_default_subprocess_env``); an explicit mapping is used as-is (the
    executor passes its sanitized attempt environment).

    ``on_spawn`` is invoked exactly once with the resolved ``log_path``,
    after the subprocess exists and before the reader thread starts — the
    hook a caller uses to announce the run's log path (e.g. a live-viewer
    notice) while the run is still young. It is best-effort: any exception
    it raises is logged at debug level and swallowed, so a broken callback
    can never affect the run itself.

    ``on_proc`` is the same best-effort hook for the ``Popen`` itself, fired
    right after spawn. A background delegation uses it to keep a handle for
    ``interrupt_fn``, so an explicit stop can kill the process group even if
    the monitoring thread is briefly descheduled.

    Returns ``(error_code, log_path, log_text, duration_seconds, returncode)``.
    """
    start_mono = time.monotonic()
    last_byte_mono = start_mono

    if env is None:
        env = _default_subprocess_env()

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=dict(env),
    )

    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None

    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            logger.debug("on_proc callback failed for pid %s", proc.pid, exc_info=True)

    if log_path is None:
        assert log_dir is not None and run_timestamp is not None, (
            "run_agent_cli needs either log_path or log_dir + run_timestamp"
        )
        log_path = log_dir / f"{run_timestamp}-{proc.pid}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if on_spawn is not None:
        try:
            on_spawn(log_path)
        except Exception:
            logger.debug("on_spawn callback failed for %s", log_path, exc_info=True)

    reader_done = threading.Event()

    def _reader() -> None:
        nonlocal last_byte_mono
        try:
            assert proc.stdout is not None
            with open(log_path, "wb") as log_file:
                while True:
                    try:
                        chunk = proc.stdout.read1(4096)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break
                    log_file.write(chunk)
                    log_file.flush()
                    last_byte_mono = time.monotonic()
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    error_code: Optional[str] = None
    while proc.poll() is None:
        if _check_interrupted():
            error_code = "interrupted"
            break

        now = time.monotonic()
        elapsed = now - start_mono
        if timeout_seconds > 0 and elapsed >= timeout_seconds:
            error_code = "timeout"
            break
        if stall_watchdog_seconds > 0 and now - last_byte_mono >= stall_watchdog_seconds:
            error_code = "stalled"
            break
        time.sleep(_MONITOR_POLL_SECONDS)

    if error_code is not None:
        _terminate_process(proc, pgid)

    reader_thread.join(timeout=_TERMINATE_GRACE_SECONDS + 1.0)
    duration = time.monotonic() - start_mono

    if reader_thread.is_alive():
        _terminate_process(proc, pgid)
        return (
            "incomplete_output",
            str(log_path),
            _read_log_text(log_path),
            duration,
            proc.poll() if proc.poll() is not None else -1,
        )

    log_text = _read_log_text(log_path)

    returncode = proc.poll()
    if returncode is None:
        try:
            returncode = proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except Exception:
            returncode = -1

    return error_code, str(log_path), log_text, duration, returncode
