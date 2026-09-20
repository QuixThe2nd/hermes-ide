"""Kanban notifier agent-wake tests for blocked dev-pipeline jobs.

Contracts under test (the 2026-08-25 t_135a3014 incident: a delegated job
OOM'd, timed out, and was routed to triage with only a human-facing Discord
message — the submitting agent was never told):

* an actionable ``dev_blocked`` event wakes the subscribed agent session with
  a SELF-CONTAINED brief (identity, cause, run evidence, locations,
  standing instruction) — not the generic one-line status wake;
* triage routing (``block_loop_detected``) wakes too;
* at most ONE wake per (task, block signature, destination) — a recovery
  attempt that re-blocks for the same cause is a human-only signal, never a
  second agent turn (the anti-self-loop);
* a changed signature wakes again;
* ``cancelled_by_user`` never wakes — the human parked it;
* ``dev_pipeline.agent_wake_on_block: false`` disables the wake entirely.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from plugins.dev_pipeline import executor as ex


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


def _blocked_dev_job(conn, *, reason: str, kind: str = "infra_broken") -> str:
    """Create a subscribed dev job and block it once. Returns the task id."""
    task_id = kb.create_task(conn, title="fix the flaky e2e suite", assignee="worker")
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
    )
    assert ex.block_dev_task(conn, task_id, kind, reason)
    return task_id


def _dev_home(tmp_path, monkeypatch, *, config: str = "") -> Path:
    """Point HERMES_HOME at a fresh temp root, optionally writing config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if config:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    return home


def test_infra_broken_wakes_agent_with_self_contained_brief(tmp_path, monkeypatch):
    db_path = tmp_path / "dev-agent-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = _blocked_dev_job(
            conn, reason="unit hermes-dev-t1-6.service OOM-killed at MemoryMax=6G",
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    # The human still gets their one-line ping...
    assert len(adapter.sent) == 1
    assert "blocked" in adapter.sent[0]["text"].lower()
    # ...and the agent gets exactly one injected turn.
    assert len(adapter.handled) == 1
    brief = adapter.handled[0].text

    # Self-contained: identity, cause, standing instruction, and the
    # not-the-user framing that keeps the woken agent from treating this as
    # a human message.
    assert task_id in brief
    assert "fix the flaky e2e suite" in brief
    assert "infra_broken" in brief
    assert "OOM-killed" in brief
    assert "Board:" in brief and "Workspace:" in brief and "Logs:" in brief
    assert "not the user" in brief
    assert "Investigate first" in brief
    assert "Recover autonomously" in brief
    assert "Escalate to the human only" in brief
    # Not the generic status wake.
    assert "needs attention" not in brief


def test_triage_routing_wakes_agent_once(tmp_path, monkeypatch):
    """block_loop_detected is the one transition that demands a decision —
    the agent must hear about it, not only the human."""
    db_path = tmp_path / "dev-agent-wake-triage.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = _blocked_dev_job(conn, reason="clone failed: auth refused")
        # Simulate the loop the incident actually hit: unblock, re-block for
        # the same cause → recurrence limit → routed to triage.
        assert kb.unblock_task(conn, task_id)
        assert ex.block_dev_task(conn, task_id, "infra_broken", "clone failed: auth refused")
        assert kb.get_task(conn, task_id).status == "triage"
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))

    assert len(adapter.handled) == 1
    brief = adapter.handled[0].text
    assert "triage" in brief
    assert "block loop detected" in brief
    # The human ping for the triage routing also fired.
    assert any("TRIAGE" in m["text"] for m in adapter.sent)


def test_same_signature_reblock_does_not_wake_again(tmp_path, monkeypatch):
    """Loop safety: an agent recovery that re-blocks with the SAME signature
    is a human-only signal. One wake total, no self-sustaining agent loop."""
    db_path = tmp_path / "dev-agent-wake-dedupe.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = _blocked_dev_job(conn, reason="attempt unit timed out")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))
    assert len(adapter.handled) == 1

    # Recovery attempt → re-block for the identical cause.
    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert ex.block_dev_task(conn, task_id, "infra_broken", "attempt unit timed out")
    finally:
        conn.close()

    # A FRESH runner is a gateway restart: no in-memory state survives, so
    # only the on-disk ledger can be suppressing this second wake.
    restarted = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(restarted)))
    # Still exactly one agent turn overall...
    assert restarted.handled == []
    # ...while the human-facing message for the re-block DID go out.
    assert any("TRIAGE" in m["text"] for m in restarted.sent)


def test_changed_signature_wakes_again(tmp_path, monkeypatch):
    """A genuinely different cause is new information, not a loop."""
    db_path = tmp_path / "dev-agent-wake-resig.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = _blocked_dev_job(conn, reason="clone failed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.handled) == 1
    assert "clone failed" in adapter.handled[0].text

    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert ex.block_dev_task(
            conn, task_id, "attempts_exhausted", "gave up after 2 attempts",
        )
    finally:
        conn.close()

    asyncio.run(_run_notifier_ticks(monkeypatch, runner))
    assert len(adapter.handled) == 2
    assert "attempts_exhausted" in adapter.handled[1].text


def test_cancelled_by_user_never_wakes(tmp_path, monkeypatch):
    """The human parked it — the agent must leave it alone."""
    db_path = tmp_path / "dev-agent-wake-cancelled.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        _blocked_dev_job(conn, reason="parked by operator", kind="cancelled_by_user")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))

    assert adapter.handled == []
    # The human ping is unaffected.
    assert len(adapter.sent) == 1


def test_agent_wake_disabled_by_config(tmp_path, monkeypatch):
    db_path = tmp_path / "dev-agent-wake-off.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _dev_home(
        tmp_path, monkeypatch,
        config="dev_pipeline:\n  agent_wake_on_block: false\n",
    )

    conn = kb.connect()
    try:
        _blocked_dev_job(conn, reason="clone failed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))

    assert adapter.handled == []
    assert len(adapter.sent) == 1


def test_two_subscriptions_each_wake_once(tmp_path, monkeypatch):
    """Dedupe is per destination, not per task: a task watched from two
    chats wakes each once — that is delivery, not a loop — and the outcome
    is the same whether both claims land in one tick or two."""
    db_path = tmp_path / "dev-agent-wake-multi.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="shared job", assignee="worker")
        for chat in ("chat-1", "chat-2"):
            kb.add_notify_sub(
                conn, task_id=task_id, platform="telegram", chat_id=chat,
            )
        assert ex.block_dev_task(conn, task_id, "infra_broken", "clone failed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))
    assert len(adapter.handled) == 2

    # Re-blocking for the same cause wakes neither destination again.
    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert ex.block_dev_task(conn, task_id, "infra_broken", "clone failed")
    finally:
        conn.close()

    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(RecordingAdapter())))
    assert len(adapter.handled) == 2


def test_plain_kanban_triage_does_not_wake(tmp_path, monkeypatch):
    """``block_loop_detected`` on a NON-dev-pipeline task is a pure human
    decision — no dev_blocked event, so no agent turn."""
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


def test_delegate_submit_path_receives_the_wake(tmp_path, monkeypatch):
    """End to end through the REAL submit path: the notify subscription
    ``delegate_development`` registers from session context at submit time is
    the destination the wake lands in — the submitting agent's own chat."""
    db_path = tmp_path / "dev-agent-wake-e2e.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    _dev_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "submitting-chat")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")

    from plugins.dev_pipeline.tool import delegate_development

    result = json.loads(delegate_development(
        repo="https://github.com/QuixThe2nd/hermes-ide.git",
        task="make the e2e suite green",
    ))
    assert result["success"], result
    task_id = result["task_id"]

    conn = kb.connect()
    try:
        assert ex.block_dev_task(
            conn, task_id, "infra_broken", "clone failed: auth refused",
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_notifier_ticks(monkeypatch, _make_runner(adapter)))

    assert len(adapter.handled) == 1
    assert adapter.handled[0].text.startswith(
        f'[Dev-pipeline job "{task_id}" blocked'
    )
    # The wake was routed to the submitting session's chat — the same one
    # the submitter would read a human reply in — not a worker session.
    assert adapter.handled[0].source.chat_id == "submitting-chat"
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "submitting-chat"
