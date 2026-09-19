"""A child spawned off the Windows ``hermes.exe`` shim waits for its parent before touching the venv.

Regression for #101600: the post-swap / re-exec child reached the shim quarantine while the
shim-run parent was still alive (relaunching gateways, or merely tearing down), the single
``os.rename`` failed with a PermissionError and the whole dependency install was deferred. The
wait is host-independent (env pid + psutil), so it is exercised with real processes here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from hermes_cli import update_handoff


def _sleeper(seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_child_waits_until_the_named_shim_parent_has_exited():
    """Real topology: the shim-run parent is OLDER than the child it spawns, so the child is a
    fresh interpreter started after the sleeper and told to wait for it."""
    parent = _sleeper(1.5)
    started = time.monotonic()
    try:
        child = subprocess.run(
            [sys.executable, "-c",
             "import os, sys; from hermes_cli import update_handoff\n"
             "ok = update_handoff.wait_for_shim_parent_exit(timeout=20)\n"
             "sys.exit(0 if ok and update_handoff.SHIM_PARENT_PID_ENV not in os.environ else 3)"],
            cwd=str(Path(__file__).resolve().parents[2]),
            env={**os.environ, update_handoff.SHIM_PARENT_PID_ENV: str(parent.pid)},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert child.returncode == 0, child.stderr
        assert parent.poll() is not None, "the child returned while the parent was still running"
        assert time.monotonic() - started >= 1.0
    finally:
        parent.kill()
        parent.wait()


def test_wait_is_bounded_and_skips_absent_garbage_and_recycled_pids(monkeypatch):
    # No shim parent named (every non-shim run, incl. Linux): no wait at all.
    monkeypatch.delenv(update_handoff.SHIM_PARENT_PID_ENV, raising=False)
    assert update_handoff.wait_for_shim_parent_exit(timeout=5) is True
    monkeypatch.setenv(update_handoff.SHIM_PARENT_PID_ENV, "not-a-pid")
    assert update_handoff.wait_for_shim_parent_exit(timeout=5) is True

    # A live process YOUNGER than us cannot be our parent: a recycled pid is never waited on.
    younger = _sleeper(30)
    try:
        assert psutil.Process(younger.pid).create_time() >= psutil.Process().create_time()
        monkeypatch.setenv(update_handoff.SHIM_PARENT_PID_ENV, str(younger.pid))
        started = time.monotonic()
        assert update_handoff.wait_for_shim_parent_exit(timeout=5) is True
        assert time.monotonic() - started < 2.0 and younger.poll() is None
    finally:
        younger.kill()
        younger.wait()

    # A parent that never exits only delays the child by the bound; the strict shim quarantine
    # downstream stays the fail-closed guard. Our own parent is older than us and outlives us.
    monkeypatch.setenv(update_handoff.SHIM_PARENT_PID_ENV, str(os.getppid()))
    assert update_handoff.wait_for_shim_parent_exit(timeout=0.5) is False
