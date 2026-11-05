"""``hermes update`` stdio protection: log mirror + hangup/broken-pipe survival.

Moved from the removed dashboard's ``main_dashboard.py``; ``cmd_update`` and the
update evals are the surviving consumers.
"""

import contextlib
import os
import sys


class _UpdateOutputStream:
    """stdout/stderr wrapper for ``hermes update``: mirrors to ``logs/update.log`` and, once the
    terminal vanishes (BrokenPipe/OSError/ValueError), drops screen output instead of the update."""

    _BROKEN = (BrokenPipeError, OSError, ValueError)

    def __init__(self, original, log_file):
        self._original = original
        self._log = log_file
        self._original_broken = False

    def write(self, data):
        # Mirror to the log file first — it's the most reliable destination.
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.write(data)
        if not self._original_broken:
            try:
                return self._original.write(data)
            except self._BROKEN:
                self._original_broken = True  # terminal vanished; keep updating
        return len(data) if isinstance(data, (str, bytes)) else 0

    def flush(self):
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.flush()
        if self._original_broken:
            return
        try:
            self._original.flush()
        except self._BROKEN:
            self._original_broken = True

    def isatty(self):
        if self._original_broken:
            return False
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        # Defer to the underlying stream; callers handle failures as when unwrapped.
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_hangup_protection(gateway_mode: bool = False):
    """Protect ``cmd_update`` from SIGHUP (→ SIG_IGN, inherited by pip/git children) and broken pipes
    (stdio wrapped in ``_UpdateOutputStream``). SIGINT/SIGTERM are left alone — legitimate cancels.
    Gateway updates are already detached, but still need the log mirror. Returns state for
    ``_finalize_update_output``."""
    state = {
        "prev_stdout": sys.stdout, "prev_stderr": sys.stderr, "log_file": None, "installed": False}

    import signal as _signal

    if not gateway_mode and hasattr(_signal, "SIGHUP"):
        # Non-main thread: update still runs, just without hangup protection.
        with contextlib.suppress(ValueError, OSError):
            _signal.signal(_signal.SIGHUP, _signal.SIG_IGN)

    # Any failure here is non-fatal; we just skip the wrap.
    try:
        # Late-bound import so tests can monkeypatch
        # hermes_cli.config.get_hermes_home to simulate setup failure.
        from hermes_cli.config import get_hermes_home as _get_hermes_home
        logs_dir = _get_hermes_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(logs_dir / "update.log", "a", buffering=1, encoding="utf-8")

        import datetime as _dt

        stage = "continued on the pulled code" if os.environ.get("HERMES_UPDATE_POST_SWAP") == "1" else "started"
        log_file.write(f"\n=== hermes update {stage} {_dt.datetime.now().isoformat(timespec='seconds')} ===\n")

        state["log_file"] = log_file
        sys.stdout = _UpdateOutputStream(state["prev_stdout"], log_file)
        sys.stderr = _UpdateOutputStream(state["prev_stderr"], log_file)
        state["installed"] = True
    except Exception:
        state["log_file"] = None

    return state


def _finalize_update_output(state):
    """Restore stdio and close the update.log handle opened by ``_install_hangup_protection``."""
    if not state:
        return
    if state.get("installed"):
        with contextlib.suppress(Exception):
            sys.stdout = state.get("prev_stdout", sys.stdout)
        with contextlib.suppress(Exception):
            sys.stderr = state.get("prev_stderr", sys.stderr)
    log_file = state.get("log_file")
    if log_file is not None:
        with contextlib.suppress(Exception):
            log_file.flush()
            log_file.close()
