"""Concurrently-superseded restart gates resolve truthfully — ``superseded``.

When a confirmed restart queues (either user-restart entry point funnels into
``gateway.restart.queue_user_restart``) while ANOTHER restart gate's confirm
wait is still pending, that pending wait is resolved with the distinguished
``SUPERSEDED_RESPONSE`` token and the waiting ``restart`` tool must answer
``{"success": false, "status": "superseded"}`` — never ``cancelled``, which
would claim the requester replied when the gateway actually bounced out from
under the wait. These tests pin the brief's five cases end to end through the
REAL clarify registry and the REAL shared queue sequence, plus the
resume-seam note that names the bounce on the replayed tool result.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
import tools.clarify_gateway as cg
from gateway.config import Platform
from gateway.restart import EXTERNAL_GATEWAY_SUPERVISOR_ENV
from gateway.session_context import clear_session_vars, set_session_vars
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

# A confirmable session: platform + chat + session key all bound.
_TELEGRAM_SESSION = {
    "platform": "telegram",
    "chat_id": "42",
    "chat_type": "dm",
    "session_key": "tg-42",
}

_DISCORD_SESSION = {
    "platform": "discord",
    "chat_id": "55",
    "chat_type": "thread",
    "thread_id": "999",
    "user_id": "123456789012345678",
    "session_key": "discord-55",
}


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """HERMES_HOME + the module-level marker dir isolated to tmp."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    yield hermes_home


@pytest.fixture(autouse=True)
def _unsupervised_env(monkeypatch):
    """Neutral supervisor/container detection (mirrors test_restart_tool)."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr("gateway.restart.is_container_restart_context", lambda: False)
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)


@pytest.fixture(autouse=True)
def _restore_plugin_modules():
    """Drop gateway_restart/plugin-manager modules between tests (quota pattern)."""
    prefixes = ("plugins.gateway_restart", "hermes_cli.plugins")
    saved = {k: m for k, m in sys.modules.items() if k.startswith(prefixes)}
    yield
    for key in list(sys.modules):
        if key.startswith(prefixes):
            del sys.modules[key]
    sys.modules.update(saved)
    for key, mod in saved.items():
        if "." in key:
            parent_name, attr = key.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, mod)


class _GatewayLoop:
    """A live event loop on a background thread, standing in for the gateway loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest.fixture
def gateway_loop():
    holder = _GatewayLoop()
    yield holder.loop
    holder.close()


def _live_runner(monkeypatch, gateway_loop):
    """A restart runner installed on the live-runner weakref."""
    runner, _adapter = make_restart_runner()
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    runner.request_restart = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    return runner


def _queue_other_gates_restart(runner, gateway_loop, *, chat_id="99"):
    """Queue ANOTHER session's confirmed restart through the shared sequence.

    This is the production supersession trigger: both user-restart entry
    points funnel into ``gateway.restart.queue_user_restart``, whose sweep
    resolves every still-pending restart-kind wait with the distinguished
    token before the drain hand-off.
    """
    from gateway.restart import queue_user_restart

    future = asyncio.run_coroutine_threadsafe(
        queue_user_restart(runner, make_restart_source(chat_id=chat_id), "m-1"),
        gateway_loop,
    )
    return future.result(timeout=5)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── 1. the drain resolves a pending restart gate as superseded ───────────────


def test_pending_gate_superseded_by_another_confirmed_restarts_queue(
    gateway_loop, monkeypatch
):
    """The queued restart's sweep hands the waiting tool the truthful result.

    The gate runs on a worker thread against the REAL registry (exactly the
    production shape: the tool blocks on a threading.Event while another
    session's confirmed restart queues on the gateway loop). The result must
    be the exact superseded JSON — ``success: false`` so the model cannot
    claim a restart of its own, ``status: "superseded"`` and never
    ``cancelled``, which would be a false statement about a reply that never
    arrived — and the OTHER gate's queue must be the only restart started.
    """
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    outcome: dict = {}

    def _run_gate():
        set_session_vars(**_TELEGRAM_SESSION)
        try:
            outcome["result"] = handle_restart({})
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc
        finally:
            clear_session_vars(None)

    gate = threading.Thread(target=_run_gate, daemon=True)
    gate.start()
    try:
        assert _wait_until(lambda: cg.has_pending("tg-42")), (
            "the confirm gate never armed its wait"
        )
        # The OTHER session's confirmed restart queues while this gate waits.
        assert _queue_other_gates_restart(runner, gateway_loop)["status"] == (
            "restarting"
        )
        gate.join(timeout=10)
        assert not gate.is_alive(), "the superseded gate never returned"
        assert "error" not in outcome

        from gateway.restart import SUPERSEDED_RESTART_ERROR

        result = json.loads(outcome["result"])
        # The exact truthful result, byte-for-byte the brief's contract.
        assert set(result) == {"success", "status", "error"}
        assert result["success"] is False
        assert result["status"] == "superseded"
        assert result["status"] != "cancelled"
        assert result["error"] == SUPERSEDED_RESTART_ERROR
        # Only the OTHER gate's queue reached request_restart — this gate
        # started nothing and queued nothing of its own.
        runner.request_restart.assert_called_once_with(detached=True, via_service=False)
        # The wait was reaped by its waiter, not stranded in the registry.
        assert cg.has_pending("tg-42") is False
    finally:
        # On an unfixed tree the wait never resolves; unblock the worker so
        # it cannot leak a live registration into later tests.
        cg.clear_session("tg-42")
        gate.join(timeout=10)


# ── 2. a genuine non-matching reply keeps today's exact cancellation ─────────


def test_genuine_wrong_reply_with_no_other_restart_draining_cancels_exactly_as_today(
    gateway_loop, monkeypatch
):
    """A real ``not now`` reply (nothing else draining) is still ``cancelled``.

    The superseded token must never widen into the reply path: a requester
    who actually answered something other than the exact word keeps the
    exact pre-existing result — status ``cancelled``, the "not the exact
    word" explanation, and no restart queued.
    """
    from plugins.gateway_restart.tool import handle_restart

    runner = _live_runner(monkeypatch, gateway_loop)
    adapter = runner.adapters[Platform.TELEGRAM]
    real_send = adapter.send

    async def _send_then_reply(chat_id, content, reply_to=None, metadata=None):
        sent = await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)
        # The requester replies from the gateway loop thread while the gate
        # is open — exactly as the text-intercept would.
        assert (
            cg.attempt_text_response_for_session("tg-42", "not now")
            == cg.TEXT_RESOLVED
        )
        return sent

    adapter.send = _send_then_reply

    set_session_vars(**_TELEGRAM_SESSION)
    try:
        result = json.loads(handle_restart({}))
    finally:
        clear_session_vars(None)
        cg.clear_session("tg-42")

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert "not the exact word" in result["error"]
    runner.request_restart.assert_not_called()
    assert cg.has_pending("tg-42") is False


# ── 3. the sweep resolves restart-kind waits only; clear_session keeps ``""`` ─


def test_restart_drain_sweep_resolves_only_restart_kind_waits(
    gateway_loop, monkeypatch
):
    """A restart-kind wait gets the token; an ordinary clarify stays put.

    The sweep rides the shared queue sequence and must be surgical: the
    OTHER session's ordinary clarify-kind wait is not touched by the
    restart drain, and still resolves with plain ``""`` through
    ``clear_session`` — the default cancellation every non-restart caller
    (``/new``, eviction, shutdown) depends on, byte-identical to before.
    """
    runner = _live_runner(monkeypatch, gateway_loop)

    cg.register(
        clarify_id="restart-gate",
        session_key="tg-77",
        question="Confirm the restart?",
        choices=None,
        wait_kind="restart",
    )
    cg.register(
        clarify_id="ordinary-clarify",
        session_key="tg-88",
        question="Which database?",
        choices=["prod", "staging"],
    )

    outcomes: dict = {}

    def _wait_on(clarify_id, key):
        outcomes[key] = cg.wait_for_response(clarify_id, 0)

    restart_waiter = threading.Thread(
        target=_wait_on, args=("restart-gate", "restart"), daemon=True
    )
    clarify_waiter = threading.Thread(
        target=_wait_on, args=("ordinary-clarify", "clarify"), daemon=True
    )
    restart_waiter.start()
    clarify_waiter.start()
    try:
        assert _queue_other_gates_restart(runner, gateway_loop)["status"] == (
            "restarting"
        )

        restart_waiter.join(timeout=10)
        assert not restart_waiter.is_alive(), "the restart-kind wait never resolved"
        # The distinguished out-of-band token, not "" and not any real reply.
        assert outcomes["restart"] == cg.SUPERSEDED_RESPONSE

        # The ordinary clarify was NOT blanket-resolved by the restart drain.
        assert cg.has_pending("tg-88") is True
        assert clarify_waiter.is_alive()

        # …and clear_session still hands it today's plain cancellation.
        assert cg.clear_session("tg-88") == 1
        clarify_waiter.join(timeout=10)
        assert not clarify_waiter.is_alive()
        assert outcomes["clarify"] == ""
    finally:
        cg.clear_session("tg-77")
        cg.clear_session("tg-88")
        restart_waiter.join(timeout=10)
        clarify_waiter.join(timeout=10)


def test_clear_session_alone_still_cancels_a_restart_kind_wait_with_empty_string():
    """The default ``clear_session`` path is unchanged for restart-kind waits.

    ``/new``, cached-agent eviction, and shutdown all call ``clear_session``
    directly — none of them is a restart, so a restart-kind wait cancelled
    that way must keep seeing plain ``""`` (the tool reports it as an
    ordinary cancellation, exactly as before the superseded token existed).
    """
    cg.register(
        clarify_id="lonely-gate",
        session_key="tg-66",
        question="Confirm the restart?",
        choices=None,
        wait_kind="restart",
    )
    outcomes: dict = {}

    def _wait_on():
        outcomes["reply"] = cg.wait_for_response("lonely-gate", 0)

    waiter = threading.Thread(target=_wait_on, daemon=True)
    waiter.start()
    try:
        assert _wait_until(lambda: cg.has_pending("tg-66"))
        assert cg.clear_session("tg-66") == 1
        waiter.join(timeout=10)
        assert not waiter.is_alive()
        assert outcomes["reply"] == ""
    finally:
        cg.clear_session("tg-66")
        waiter.join(timeout=10)


# ── 4. the thread title restore still runs on the superseded path ────────────


class _TitleCapability:
    """A recording stand-in for the adapter's pending-title capability."""

    def __init__(self, order, *, token="restore-token"):
        self.order = order
        self.token = token
        self.end_calls: list[object] = []

    async def capture(self, thread_id):
        self.order.append("capture")
        return self.token

    async def begin(self, restore):
        self.order.append("rename")

    async def end(self, restore):
        self.order.append("restore")
        self.end_calls.append(restore)

    def attach(self, adapter):
        adapter.capture_restart_pending_thread_title = self.capture
        adapter.begin_restart_pending_thread_title = self.begin
        adapter.end_restart_pending_thread_title = self.end
        return adapter


def test_superseded_path_restores_the_thread_title(gateway_loop, monkeypatch):
    """The Discord thread is retitled back even when the gate is superseded.

    The rename ran before the wait, so a superseded resolution that skipped
    the restore would strand the thread on ``Restart Pending`` forever. The
    restore must fire on this exit too — and this gate queues nothing, so
    the only restart started is the other gate's.
    """
    from gateway.platforms.base import SendResult
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = make_restart_runner()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._gateway_loop = gateway_loop
    runner._background_tasks = set()
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)

    order: list[str] = []

    async def _rich(**kwargs):
        order.append("rich")
        return SendResult(success=True, message_id="e1")

    adapter.send_restart_confirmation = _rich
    title = _TitleCapability(order)
    title.attach(adapter)
    inner_restart = MagicMock(return_value=True)

    def _record_restart(**kwargs):
        order.append("request_restart")
        return inner_restart(**kwargs)

    runner.request_restart = MagicMock(side_effect=_record_restart)

    outcome: dict = {}

    def _run_gate():
        set_session_vars(**_DISCORD_SESSION)
        try:
            outcome["result"] = handle_restart({})
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc
        finally:
            clear_session_vars(None)

    gate = threading.Thread(target=_run_gate, daemon=True)
    gate.start()
    try:
        assert _wait_until(lambda: "rename" in order), (
            "the gate never reached its confirm wait"
        )
        assert _queue_other_gates_restart(runner, gateway_loop)["status"] == (
            "restarting"
        )
        gate.join(timeout=10)
        assert not gate.is_alive(), "the superseded gate never returned"
        assert "error" not in outcome

        result = json.loads(outcome["result"])
        assert result["success"] is False
        assert result["status"] == "superseded"
        # Retitled before the prompt; the exact original name restored on
        # the superseded exit — and the only queued restart was the OTHER
        # gate's, which owns the loop until its request_restart returns.
        assert order == ["capture", "rename", "rich", "request_restart", "restore"]
        assert title.end_calls == [title.token]
        assert inner_restart.call_count == 1
        # The armed wait was reaped by its waiter.
        assert cg.has_pending("discord-55") is False
    finally:
        cg.clear_session("discord-55")
        gate.join(timeout=10)


# ── 5. the resume-seam note names the bounce on the superseded result ────────


def test_resume_note_names_the_bounce_on_the_superseded_tool_result():
    """The replayed superseded result gains one line naming the bounce.

    The forced-resume seam annotates the model-facing copy of the tail's
    superseded restart result (never a synthetic user row): the original
    JSON stays intact and one appended line names the bounce with an
    ISO-8601 UTC stamp, says who caused it, and corrects the record —
    superseded, not cancelled.
    """
    from datetime import datetime, timezone

    from gateway.restart import (
        SUPERSEDED_RESTART_NOTE_PREFIX,
        annotate_superseded_restart_tail,
    )
    from plugins.gateway_restart.tool import _superseded_json

    original = _superseded_json()
    rows = [
        {"role": "user", "content": "bounce the gateway"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": original},
    ]

    annotated = annotate_superseded_restart_tail(
        rows, restarted_at=datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc)
    )

    assert annotated == 1
    content = rows[-1]["content"]
    # The tool's own result survives intact; the note is ONE appended line.
    assert content.startswith(original)
    assert content.count("\n") == 1
    assert content.startswith(f"{original}\n{SUPERSEDED_RESTART_NOTE_PREFIX}")
    assert "at 2026-09-14T10:30:00Z" in content
    assert "by another confirmed restart" in content
    assert "superseded, not cancelled" in content
    # Idempotent: the note prefix is the mark, a second pass annotates nothing.
    assert (
        annotate_superseded_restart_tail(
            rows, restarted_at=datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc)
        )
        == 0
    )
    assert rows[-1]["content"] == content


def test_resume_note_is_surgical_about_what_it_annotates():
    """Lookalikes, cancellations, and non-tail rows are never annotated."""

    from gateway.restart import annotate_superseded_restart_tail
    from plugins.gateway_restart.tool import _superseded_json

    superseded = _superseded_json()

    # A lookalike echoing only the status field — not the tool's result.
    lookalike_content = '{"success":false,"status":"superseded"}'
    lookalike = {"role": "tool", "tool_call_id": "t1", "content": lookalike_content}
    assert annotate_superseded_restart_tail([lookalike]) == 0
    assert lookalike["content"] == lookalike_content

    # A genuine cancellation keeps its exact text.
    cancelled = {
        "role": "tool",
        "tool_call_id": "t2",
        "content": '{"success":false,"error":"Restart cancelled","status":"cancelled"}',
    }
    assert annotate_superseded_restart_tail([cancelled]) == 0
    assert '"status":"cancelled"' in cancelled["content"]
    assert "[system] The gateway was restarted" not in cancelled["content"]

    # Not in the tail: an assistant row after the tool result stops the scan.
    buried = [
        {"role": "tool", "tool_call_id": "t3", "content": superseded},
        {"role": "assistant", "content": "Restarting now."},
    ]
    assert annotate_superseded_restart_tail(buried) == 0
    assert buried[0]["content"] == superseded

    # Epoch timestamps stamp just as well as datetimes.
    stamped = [{"role": "tool", "tool_call_id": "t4", "content": superseded}]
    assert annotate_superseded_restart_tail(stamped, restarted_at=1760000000) == 1
    assert "at 2025-10-09T" in stamped[0]["content"]
