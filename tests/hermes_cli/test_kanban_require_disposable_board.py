"""Tests for the fresh-process disposable-board guard.

Background (pc_fa9ca887ebc61dc8): fresh-process Kanban test helpers and
worktree repros can silently create tasks on the LIVE board when their
isolation env routing is incomplete — ``HERMES_HOME`` pointed at a profile
path still resolves to the shared install root by design. These tests pin
the fail-closed semantics of ``require_disposable_board``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

WT = str(Path(__file__).resolve().parents[2])

KANBAN_ENV_VARS = (
    "HERMES_HOME",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the process at a disposable home the standard e2e way."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_temp_home_passes(isolated_home):
    db = kb.require_disposable_board()
    assert db == (isolated_home / "kanban.db").resolve()
    # Explicit anchor form used by fresh-process helpers:
    assert kb.require_disposable_board(expect_under=isolated_home) == db


def test_kanban_db_pin_passes(tmp_path, monkeypatch, isolated_home):
    pinned = tmp_path / "other" / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    assert kb.require_disposable_board() == pinned.resolve()


def test_profile_style_home_is_not_disposable(monkeypatch):
    """HERMES_HOME under the real install still resolves to the live root."""
    live_root = kb._live_install_root()
    if live_root is None:  # pragma: no cover - platform fallback
        pytest.skip("no live install root discoverable on this platform")
    profile_home = live_root / "profiles" / "pc-guard-test"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    for var in KANBAN_ENV_VARS[1:]:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="live Kanban board"):
        kb.require_disposable_board()


def test_no_routing_at_all_is_rejected(monkeypatch):
    """Forgetting the override entirely must refuse the live board."""
    live_root = kb._live_install_root()
    if live_root is None:  # pragma: no cover - platform fallback
        pytest.skip("no live install root discoverable on this platform")
    for var in KANBAN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="live Kanban board"):
        kb.require_disposable_board()


def test_expect_under_catches_mismatched_anchor(tmp_path, monkeypatch, isolated_home):
    other = tmp_path / "expected-root"
    other.mkdir()
    with pytest.raises(RuntimeError, match="outside the expected"):
        kb.require_disposable_board(expect_under=other)


FAKE_WORKER_SNIPPET = """\
import os
import sys

sys.path.insert(0, os.getcwd())

from hermes_cli.kanban_db import require_disposable_board

require_disposable_board(expect_under=os.environ.get("HERMES_HOME") or None)
"""


def test_fake_worker_fails_closed_without_routing(tmp_path):
    """End-to-end: spawning a fresh-process worker with NO isolation env refuses to run.

    Before pc_fa9ca887ebc61dc8's guard this helper would happily heartbeat
    and complete a task against whatever board the ambient environment
    resolved — including the live one. The original helper lived in
    tests/stress/_fake_worker.py; that never-executed suite was retired on
    main, so a minimal worker is inlined here.
    """
    worker = tmp_path / "_fake_worker.py"
    worker.write_text(FAKE_WORKER_SNIPPET)
    env = {k: v for k, v in os.environ.items() if k not in KANBAN_ENV_VARS}
    env["HERMES_KANBAN_TASK"] = "guard-probe"
    proc = subprocess.run(
        [sys.executable, str(worker)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=WT,
    )
    assert proc.returncode != 0
    assert "live Kanban board" in proc.stderr
