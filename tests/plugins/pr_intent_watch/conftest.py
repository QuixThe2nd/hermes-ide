"""Hermeticity guards for the pr_intent_watch plugin test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REAL_SYSTEMD_PATHS = (
    "/etc/systemd/system",
    "/run/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    str(Path.home() / ".config/systemd"),
    str(Path.home() / ".local/share/systemd"),
    "/var/lib/systemd/timers",
)


@pytest.fixture(autouse=True)
def _block_real_systemd_subprocess(monkeypatch):
    """Fail fast if any pr_intent_watch test touches real systemctl."""

    real_run = subprocess.run

    def guarded_run(args, *run_args, **run_kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        joined = " ".join(str(part) for part in argv)
        if "systemctl" in joined:
            raise AssertionError(
                f"pr_intent_watch tests must not invoke real systemctl: {joined}"
            )
        for path in REAL_SYSTEMD_PATHS:
            if path in joined:
                raise AssertionError(
                    f"pr_intent_watch tests must not touch live systemd paths: {joined}"
                )
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
