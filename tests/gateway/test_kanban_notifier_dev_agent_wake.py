"""Kanban notifier wake tests for blocked plain-kanban tasks.

The dev-pipeline agent-wake machinery was removed along with the plugin;
what remains under test is the generic behavior it used to interact with:

* ``block_loop_detected`` on a plain kanban task is a pure human decision —
  no agent turn is injected, only the human-facing TRIAGE ping fires.
"""

from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    """Push-capable adapter recording both the human ping and the wake turn."""

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


async def _run_notifier_ticks(monkeypatch, runner, count: int = 1) -> None:
    """Run the watcher for ``count`` ticks, re-arming ``_running`` each time."""
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


def test_plain_kanban_triage_does_not_wake(tmp_path, monkeypatch):
    """``block_loop_detected`` on a plain kanban task is a pure human
    decision — no agent turn is injected."""
    db_path = tmp_path / "plain-kanban-triage.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ordinary card", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="telegram", chat_id="chat-1",
        )
        assert kb.block_task(conn, task_id, reason="stuck", kind="needs_input")
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(conn, task_id, reason="stuck", kind="needs_input")
        assert kb.get_task(conn, task_id).status == "triage"
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))

    assert adapter.handled == []
    assert any("TRIAGE" in m["text"] for m in adapter.sent)
