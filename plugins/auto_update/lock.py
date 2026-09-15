"""Nonblocking profile-scoped flock lock for scheduled update runs."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore


def lock_path() -> Path:
    return get_hermes_home() / "auto-update" / ".run.lock"


@contextmanager
def nonblocking_run_lock() -> Iterator[bool]:
    """Acquire the run lock or yield False on contention.

    Contention is a quiet successful deferral — callers should exit 0.
    The lock file is left in place after release so a second opener cannot
    recreate-and-reflock a fresh inode.
    """
    if fcntl is None:  # pragma: no cover - unsupported platform
        yield True
        return

    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
