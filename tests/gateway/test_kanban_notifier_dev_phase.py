"""Kanban notifier tests for terminal-event mention semantics on plain tasks.

The dev-pipeline phase-notification machinery was removed along with the
plugin; what remains under test is the generic terminal-event behavior it
used to share a claim with: the assignee mention and blocked wording on the
human-facing ping for a plain kanban task.
"""

from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


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


def test_terminal_event_keeps_mention_semantics(tmp_path, monkeypatch):
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
        kb.block_task(conn, task_id, reason="needs input", kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "@" in adapter.sent[0]["text"]
    assert "blocked" in adapter.sent[0]["text"].lower()
