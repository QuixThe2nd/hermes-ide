"""Regression gate: the agent ``restart`` tool has NO confirmation gate.

An older flow pinged the chat and waited for a user "restart" reply before
queueing. That flow was removed: calling ``handle_restart`` with a valid runner
and live gateway loop must reach the shared ``queue_user_restart`` sequence
immediately, with ZERO user-facing interaction — no adapter send, no typing
indicator, no waiting-for-user path — before the restart is queued.

If a confirmation step ever returns, these asserts fail on the adapter traffic
it necessarily produces (or on queue_user_restart never being reached because
the handler is parked awaiting a reply).
"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def gateway_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5.0)
    loop.close()


@pytest.fixture
def reset_session_env(monkeypatch):
    # Not a cron session; no pinned session key / message id — the handler
    # must work with no ambient interaction state at all.
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_MESSAGE_ID", raising=False)


def _install_runner(monkeypatch, loop):
    runner, adapter = make_restart_runner()
    runner._gateway_loop = loop
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    return runner, adapter


def test_handle_restart_queues_with_zero_user_replies(
    monkeypatch, gateway_loop, reset_session_env
):
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _install_runner(monkeypatch, gateway_loop)
    typing_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(adapter, "send_typing", typing_spy)
    queued = AsyncMock(
        return_value={"status": "restarting", "active_agents": 0, "via_service": False}
    )
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)

    result_raw = handle_restart({})

    # The restart was queued exactly once, immediately.
    queued.assert_awaited_once()
    assert queued.await_args.args[0] is runner
    payload = json.loads(result_raw)
    assert payload["success"] is True
    assert payload["status"] == "restarting"

    # Zero user-facing interaction on the way in: nothing was sent, nothing
    # typed, no platform-facing choreography before the queue call.
    assert adapter.sent == []
    typing_spy.assert_not_awaited()


def test_handle_restart_already_committed_short_circuits_without_user_ping(
    monkeypatch, gateway_loop, reset_session_env
):
    """A restart already in flight answers from the state probe alone — still
    no adapter traffic, no queue call."""
    from plugins.gateway_restart.tool import handle_restart

    runner, adapter = _install_runner(monkeypatch, gateway_loop)
    runner._restart_requested = True
    queued = AsyncMock()
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)

    result_raw = handle_restart({})

    payload = json.loads(result_raw)
    assert payload["status"] == "already_in_progress"
    queued.assert_not_awaited()
    assert adapter.sent == []
