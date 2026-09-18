"""Tool-surface parents reporting for kanban_complete. Regression for #113373.

``complete_task`` reports every refusal as bare ``False``. On the CLI surface
the open PRs #110323/#110330/#110334 make the parents refusal actionable;
the worker-facing TOOL surface (``tools/kanban_tools._handle_complete`` — the
call a dispatcher-owned worker actually makes) collapsed it into
"unknown id, stale run, or already terminal", sending the worker/operator
chasing stale runs while the real blocker was a reopened or unfinished parent.
"""
import json

import pytest


@pytest.fixture
def running_child_with_parent(monkeypatch, tmp_path):
    """A claimed (running) child whose parent is NOT done. Returns ids."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent_id = kb.create_task(conn, title="parent gate", assignee="op")
        assert kb.complete_task(conn, parent_id, result="parent shipped")
        child_id = kb.create_task(
            conn, title="child work", assignee="test-worker", parents=[parent_id])
        assert kb.claim_task(conn, child_id) is not None
        # Parent reopens mid-run: the #113373 timeline.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
                (parent_id,),
            )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child_id)
    return parent_id, child_id


def test_complete_names_unsatisfied_parent(running_child_with_parent):
    """The refusal names the blocking parent instead of crying stale run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    parent_id, child_id = running_child_with_parent
    out = json.loads(kt._handle_complete(
        {"task_id": child_id, "summary": "genuinely done work"}))
    assert out.get("error")
    assert parent_id in out["error"]
    assert "parent" in out["error"].lower()
    assert "stale run" not in out["error"]
    # Nothing mutated: the run is still open for a retry after the parent.
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child_id).status == "running"
    finally:
        conn.close()


def test_complete_multiple_parents_deterministic_order(monkeypatch, tmp_path):
    """Several unfinished parents are all named, in stable id order."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        p1 = kb.create_task(conn, title="gate one", assignee="op")
        p2 = kb.create_task(conn, title="gate two", assignee="op")
        for p in (p1, p2):
            assert kb.complete_task(conn, p, result="x")
        child = kb.create_task(
            conn, title="child", assignee="test-worker", parents=[p2, p1])
        assert kb.claim_task(conn, child) is not None
        # Both gates reopen mid-run.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL "
                "WHERE id IN (?, ?)", (p1, p2),
            )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    out = json.loads(kt._handle_complete(
        {"task_id": child, "summary": "done"}))
    assert out.get("error")
    # Deterministic id order: ids are random hex, so sort first rather than
    # assuming creation order.
    first, second = sorted([p1, p2])
    assert out["error"].index(first) < out["error"].index(second)


def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def test_complete_unknown_id_stays_generic(monkeypatch, tmp_path):
    """Missing tasks keep the generic message (no phantom parent lookup)."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete(
        {"task_id": "t_deadbeefdeadbeef", "summary": "x"}))
    assert "unknown id, stale run, or already terminal" in out.get("error", "")


def test_complete_terminal_stays_generic(monkeypatch, tmp_path):
    """An already-done task keeps the generic message (no parents to name)."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="test-worker")
        assert kb.claim_task(conn, tid) is not None
        assert kb.complete_task(conn, tid, result="done")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "again"}))
    assert "unknown id, stale run, or already terminal" in out.get("error", "")


def test_complete_happy_path_unchanged(monkeypatch, tmp_path):
    """Parents done -> completion still succeeds exactly as before."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="op")
        assert kb.complete_task(conn, parent, result="p done")
        child = kb.create_task(
            conn, title="c", assignee="test-worker", parents=[parent])
        assert kb.claim_task(conn, child) is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    out = json.loads(kt._handle_complete({"task_id": child, "summary": "c done"}))
    assert out.get("ok") is True


def test_delegate_description_states_child_handoff_contract():
    """Spawn-time surfacing (#113373 ask 1): the delegate_task schema tells the
    parent up front that a child cannot close tracked work and must hand back
    findings. Keyword-level so rewording does not break CI."""
    from tools.delegate_tool import _build_top_level_description
    desc = _build_top_level_description()
    assert "tracked work" in desc
    assert len(desc) <= 2200
