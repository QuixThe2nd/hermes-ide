"""Kanban notifier dev_phase progress message tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from plugins.dev_pipeline import executor as ex


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


async def _run_notifier_ticks(monkeypatch, runner, count: int = 1) -> None:
    """Run the watcher for ``count`` ticks, re-arming ``_running`` each time.

    The fake sleep flips ``runner._running`` off after the tick body so the
    ``while self._running`` loop exits; re-arming lets a single test drive
    multiple genuine claim/deliver passes.
    """
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    for _ in range(count):
        runner._running = True
        await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _add_dev_phase_sub(conn, task_id: str, *, with_reply_metadata: bool = True):
    delivery_metadata = {
        "thread_id": "20197",
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
    }
    if with_reply_metadata:
        delivery_metadata["telegram_reply_to_message_id"] = "462"
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="20197",
        delivery_metadata=delivery_metadata,
    )


def _unseen_dev_phase_events(task_id: str) -> list:
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            kinds=["dev_phase"],
        )
        return events
    finally:
        conn.close()


def test_dev_phase_sends_progress_without_mention(tmp_path, monkeypatch):
    db_path = tmp_path / "dev-phase.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_PLANNING)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]["text"]
    assert msg == f"Dev job {task_id} started planning the work."
    assert "@" not in msg
    assert "→" not in msg
    assert "telegram_reply_to_message_id" not in adapter.sent[0]["metadata"]
    assert adapter.sent[0]["metadata"].get("thread_id") == "20197"

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1
    # Cursor advanced past the event: nothing replays.
    assert _unseen_dev_phase_events(task_id) == []


def test_sequential_dev_phase_events_render_sentences(tmp_path, monkeypatch):
    db_path = tmp_path / "dev-phase-arrow.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id, with_reply_metadata=False)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_PLANNING)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert adapter.sent[0]["text"] == f"Dev job {task_id} started planning the work."
    second = adapter.sent[1]["text"]
    assert second == (
        f"Dev job {task_id} finished planning the work "
        f"and is now writing the code."
    )
    assert "→" not in second and "->" not in second


def test_same_phase_heartbeat_is_silent_and_cursor_advances(tmp_path, monkeypatch):
    """RUNNING → RUNNING with no payload kind/detail must send nothing.

    The cursor must still advance so the heartbeats are never replayed —
    this is the exact pattern that used to flood chats.
    """
    db_path = tmp_path / "dev-phase-heartbeat.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id, with_reply_metadata=False)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING, {"entered": True})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    # Tick 1 establishes RUNNING (one "started" sentence).
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1

    conn = kb.connect()
    try:
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING, {"unit": "u1"})
    finally:
        conn.close()

    # Tick 2: two same-phase heartbeats, nothing new to say → zero sends.
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1
    # The heartbeats were claimed, not left to replay forever.
    assert _unseen_dev_phase_events(task_id) == []


def test_file_edited_progress_renders_editing_sentence(tmp_path, monkeypatch):
    db_path = tmp_path / "dev-phase-edit.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id, with_reply_metadata=False)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING, {"entered": True})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1

    conn = kb.connect()
    try:
        ex.record_dev_phase(
            conn, task_id, None, ex.PHASE_RUNNING,
            {"kind": "file_edited", "detail": "plugins/home_server/core.py"},
        )
    finally:
        conn.close()

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 2
    msg = adapter.sent[1]["text"]
    assert msg == f"Dev job {task_id} is editing `plugins/home_server/core.py`."
    assert "→" not in msg
    assert "RUNNING" not in msg


def test_burst_progress_collapses_to_one_message(tmp_path, monkeypatch):
    """A same-phase burst in one tick sends at most one message.

    file_edited beats command beats checkpoint, and identical (kind, detail)
    pairs never repeat.
    """
    db_path = tmp_path / "dev-phase-burst.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id, with_reply_metadata=False)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_RUNNING, {"entered": True})
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 1

    conn = kb.connect()
    try:
        ex.record_dev_phase(
            conn, task_id, None, ex.PHASE_RUNNING,
            {"kind": "file_edited", "detail": "plugins/a.py"},
        )
        ex.record_dev_phase(
            conn, task_id, None, ex.PHASE_RUNNING,
            {"kind": "command", "detail": "pytest"},
        )
        # Duplicate of the first edit — must not add a second message.
        ex.record_dev_phase(
            conn, task_id, None, ex.PHASE_RUNNING,
            {"kind": "file_edited", "detail": "plugins/a.py"},
        )
        ex.record_dev_phase(
            conn, task_id, None, ex.PHASE_RUNNING,
            {"kind": "checkpoint", "detail": "mid-run checkpoint"},
        )
    finally:
        conn.close()

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.sent) == 2
    msg = adapter.sent[1]["text"]
    assert msg == f"Dev job {task_id} is editing `plugins/a.py`."
    assert "pytest" not in msg
    assert "checkpoint" not in msg


def test_progress_notifications_false_advances_cursor_without_send(
    tmp_path, monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "dev_pipeline:\n  progress_notifications: false\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "dev-phase-muted.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        _add_dev_phase_sub(conn, task_id, with_reply_metadata=False)
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_VERIFYING)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert adapter.sent == []

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert adapter.sent == []

    assert _unseen_dev_phase_events(task_id) == []


def test_terminal_event_keeps_mention_semantics_with_dev_phase(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "dev-phase-terminal.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="dev job", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
        )
        ex.record_dev_phase(conn, task_id, None, ex.PHASE_PUBLISHING)
        kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert "@" in adapter.sent[1]["text"]
    assert "blocked" in adapter.sent[1]["text"].lower()
    assert adapter.sent[0]["text"].lower().startswith("dev job")
    assert "@" not in adapter.sent[0]["text"]
