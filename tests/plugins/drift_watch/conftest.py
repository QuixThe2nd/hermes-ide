"""Hermeticity guards for the drift_watch plugin test suite."""

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
    """Fail fast if any drift_watch test touches real systemctl or systemd paths."""

    real_run = subprocess.run

    def guarded_run(args, *run_args, **run_kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        joined = " ".join(str(part) for part in argv)
        if "systemctl" in joined:
            raise AssertionError(
                f"drift_watch tests must not invoke real systemctl: {joined}"
            )
        for path in REAL_SYSTEMD_PATHS:
            if path in joined:
                raise AssertionError(
                    f"drift_watch tests must not touch live systemd paths: {joined}"
                )
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A clean one-commit git repo."""
    root = tmp_path / "repo"
    root.mkdir()
    _run(["git", "init", "-q", str(root)])
    _run(["git", "-C", str(root), "config", "user.email", "test@example.com"])
    _run(["git", "-C", str(root), "config", "user.name", "Test"])
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    _run(["git", "-C", str(root), "add", "."])
    _run(["git", "-C", str(root), "commit", "-qm", "init"])
    return root


def _run(argv: list[str]) -> None:
    import subprocess

    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{argv} failed: {proc.stderr}"
