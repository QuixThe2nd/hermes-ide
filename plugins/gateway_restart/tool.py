"""``restart`` tool — agent-callable gateway restart on the /restart drain path.

The tool queues through the same gateway-owned sequence as the ``/restart``
slash command: warn about uncommitted runtime source, persist the requester's
comeback routing, offer the opt-in Discord wind-down embed, then hand off to
``GatewayRunner.request_restart``. The shared drain blocks new work and waits
for in-flight sessions to finish naturally, with no cap, before the gateway
bounces and comes back online. The tool never runs a wait of its own beside
that drain. The shell/systemctl path stays blocked by the lifecycle guard —
that path SIGTERMs the gateway and kills whatever child ran the command.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from agent.turn_control import turn_control_field_for

logger = logging.getLogger(__name__)

RESTART_SCHEMA = {
    "name": "restart",
    "description": (
        "Queue a Hermes gateway restart through the same drain path as the "
        "/restart slash command. The restart is queued immediately, including "
        "when other sessions or background work are active. The shared drain "
        "blocks new work and waits for every in-flight turn to finish "
        "naturally, without forcing or capping that work. The gateway then "
        "bounces and the requesting chat gets a comeback notice. A successful "
        "restart ends this turn immediately: do not plan further tool calls "
        "or a closing summary after it — the user sees the restart lifecycle "
        "notices instead. Use it to pick up config or code changes, or to "
        "recover a misbehaving gateway."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# Gateway-loop round trips (the state probe, then the queued restart) are
# quick — the shared on-loop sequence synchronously writes one small marker
# file, optionally sends the Discord wind-down embed, and hands off to
# request_restart, which schedules the drain task. The synchronous marker
# write matters: it cannot keep running past a cancelled hand-off the way a
# thread-pool write can, so a timeout can never leave a phantom notify marker
# behind. The bound exists so a wedged loop fails the tool call instead of
# hanging the worker thread forever. A timeout cancels the queued restart
# mid-setup; nothing before the request_restart call mutates admission, and
# request_restart owns rolling its own setup back, so the failed hand-off
# leaves the gateway retryable and never reaches stop().
_BEGIN_RESTART_TIMEOUT_S = 15.0

_NO_RUNNER_ERROR = (
    "No live gateway runner in this process — the restart tool only works "
    "inside a running gateway. From outside, an operator can run "
    "`hermes gateway restart` in a separate shell."
)

# The runner the plugin needs must expose the current restart entry point.
# A runner without it is a foreign or outdated object — restarts from here
# would crash on a missing attribute (the bug this guard exists for), so the
# tool refuses with a typed error instead.
_UNSUPPORTED_RUNNER_ERROR = (
    "This gateway runner does not expose the current restart API "
    "(`request_restart`) — the gateway and the restart tool are out of "
    "sync. Restart the gateway from an operator shell "
    "(`hermes gateway restart`) once, which realigns both, and retry."
)

_CRON_REFUSAL = (
    "Refusing to restart the gateway from a cron session: a cron-triggered "
    "restart is the SIGTERM-respawn loop the gateway lifecycle guard exists "
    "to stop. Use /restart or the `restart` tool from an interactive chat, "
    "not from cron."
)


def check_restart_requirements() -> bool:
    """True only inside a live gateway process.

    Hidden in CLI/cron subprocesses and anything else without a running
    ``GatewayRunner``. Deliberately does NOT require systemd: an unsupervised
    foreground ``hermes gateway run`` still restarts fine via the detached
    helper, exactly like ``/restart`` there.
    """
    try:
        from gateway.run import _gateway_runner_ref

        return _gateway_runner_ref() is not None
    except Exception:
        return False


def _result_json(runner: Any, status: dict) -> str:
    payload = {
        "success": True,
        "status": status.get("status"),
        "draining": bool(getattr(runner, "_draining", False)),
        "active_agents": status.get("active_agents", 0),
        "via_service": status.get("via_service"),
    }
    # Committed restart statuses stamp the reserved exact field so the tool
    # executor can arm the per-turn flag and end this turn (no further
    # provider calls, no later sibling tools).
    control_field = turn_control_field_for(status.get("status"))
    if control_field:
        payload.update(control_field)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _error_json(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _submit_to_loop(coro: Any, loop: Any) -> tuple[Any, Optional[Exception]]:
    """Schedule *coro* on the gateway loop; ``(future, None)`` or ``(None, exc)``.

    ``asyncio.run_coroutine_threadsafe`` raises outright when submission fails
    — for example, a loop that closed between the caller's liveness check and
    this hop — and the coroutine it was handed never runs. Close it here and
    return the error so no coroutine leaks and the caller gets an ordinary
    restart error.
    """
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop), None
    except Exception as exc:
        coro.close()
        return None, exc


def _probe_restart_state(runner: Any, loop: Any) -> Optional[tuple[bool, bool]]:
    """Read ``(committed, draining)`` in one gateway-loop snapshot."""
    from gateway.restart import restart_committed

    async def _probe() -> tuple[bool, bool]:
        return restart_committed(runner), bool(getattr(runner, "_draining", False))

    future, submit_error = _submit_to_loop(_probe(), loop)
    if submit_error is not None:
        return None
    try:
        return future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception:
        future.cancel()
        return None


async def _queue_user_restart(runner: Any, source: Any, message_id: Optional[str]) -> dict:
    """Queue the restart through the ONE shared gateway sequence.

    Thin delegation to ``gateway.restart.queue_user_restart`` — the
    gateway-owned helper the ``/restart`` slash handler also calls — so this
    path owns no requester-setup choreography of its own and imports no core
    privates; the two user-restart entry points cannot drift apart. Must run
    on the gateway event loop (``request_restart`` schedules its drain task
    there), and returns the shared status dict shape: ``status``
    ("restarting" / "already_in_progress"), ``active_agents`` (counted
    before the drain closes in), and ``via_service`` (None when nothing was
    started this call).
    """
    from gateway.restart import queue_user_restart

    return await queue_user_restart(runner, source, message_id)


def handle_restart(args: dict, **_: Any) -> str:
    """Queue a gateway restart via the shared /restart drain path.

    Cron sessions, missing or outdated runners, a dead gateway loop, and an
    invocation on the gateway loop itself are refused. An already-committed
    restart returns ``already_in_progress`` without queueing another; a generic
    shutdown drain returns the existing error. Otherwise the shared queue is
    submitted to the gateway loop and its status becomes the tool result.
    """
    from gateway.session_context import get_session_env
    from utils import is_truthy_value

    if is_truthy_value(get_session_env("HERMES_CRON_SESSION", "")):
        return _error_json(_CRON_REFUSAL)

    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except Exception:
        runner = None
    if runner is None:
        return _error_json(_NO_RUNNER_ERROR)

    # The gateway-owned queue sequence this hands the runner into calls these
    # on it directly. A runner without them is a foreign or outdated object,
    # and the failure mode this tool already lived through is the raw
    # AttributeError — refuse early with a typed error instead of crashing
    # the tool call mid-flow.
    if not callable(getattr(runner, "request_restart", None)) or not callable(
        getattr(runner, "_running_agent_count", None)
    ):
        return _error_json(_UNSUPPORTED_RUNNER_ERROR)

    # Tool handlers run in a worker thread, and request_restart() calls
    # asyncio.create_task — every gateway-side observation and coroutine must
    # use the gateway event loop, never this thread's (nonexistent) loop.
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop.is_closed():
        return _error_json(
            "The gateway event loop is not running, so a restart cannot be "
            "started from here."
        )

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        return _error_json(
            "The restart tool must run from a tool worker thread, not on the "
            "gateway event loop."
        )

    state = _probe_restart_state(runner, loop)
    if state is None:
        return _error_json(
            "The gateway event loop could not confirm the current restart "
            "state; no restart was started."
        )
    committed, draining = state
    if committed:
        # A restart already in flight owns the notify payload — do not write
        # another requester's routing or call request_restart a second time.
        return _result_json(
            runner,
            {
                "status": "already_in_progress",
                "active_agents": runner._running_agent_count(),
                "via_service": None,
            },
        )
    if draining:
        return _error_json(
            "Gateway shutdown is already in progress; no restart was started."
        )

    session_key = get_session_env("HERMES_SESSION_KEY", "").strip()
    get_cached_source = getattr(runner, "_get_cached_session_source", None)
    source = get_cached_source(session_key) if callable(get_cached_source) else None
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip() or None
    future, submit_error = _submit_to_loop(
        _queue_user_restart(runner, source, message_id), loop
    )
    if submit_error is not None:
        logger.warning(
            "restart tool: the queued restart could not be submitted: %s",
            submit_error,
        )
        return _error_json(f"Failed to begin gateway restart: {submit_error}")
    try:
        status = future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        # Cancellation lands at an await point: nothing before the
        # request_restart call mutates admission, and request_restart rolls
        # its own setup back when it cannot establish the drain task — so
        # the gateway stays retryable and stop() is never reached.
        future.cancel()
        logger.warning("restart tool: queued restart failed: %s", exc)
        return _error_json(f"Failed to begin gateway restart: {exc}")

    if status.get("status") == "shutdown_in_progress":
        return _error_json(
            "Gateway shutdown is already in progress; no restart was started."
        )
    return _result_json(runner, status)
