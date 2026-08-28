"""``restart`` tool — agent-callable gateway restart on the /restart drain path.

The tool is a thin front end for ``GatewayRunner.begin_user_restart``, the same
shared entry point the ``/restart`` slash command uses, so the two cannot drift
apart: in-flight turns drain first, then the gateway bounces and comes back
online. The shell/systemctl path stays blocked by the lifecycle guard — that
path SIGTERMs the gateway and kills whatever child was running the command.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESTART_SCHEMA = {
    "name": "restart",
    "description": (
        "Restart the Hermes gateway — the same drain path as the /restart "
        "slash command. In-flight turns (including this one) finish first, "
        "then the gateway stops and comes back online, and the requesting "
        "chat gets a comeback notice. Use it to pick up config or code "
        "changes, or to recover a misbehaving gateway. Returns immediately; "
        "the restart itself fires after the current turn ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# begin_user_restart only writes two small marker files and schedules the
# drain task — well under a second. The bound exists so a wedged loop fails
# the tool call instead of hanging the worker thread forever.
_BEGIN_RESTART_TIMEOUT_S = 15.0

_NO_RUNNER_ERROR = (
    "No live gateway runner in this process — the restart tool only works "
    "inside a running gateway. From outside, an operator can run "
    "`hermes gateway restart` in a separate shell."
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


def _source_from_session_context() -> Optional[object]:
    """Build the requester's ``SessionSource`` from the turn's session context.

    Returns None when this turn has no messaging-platform identity (no
    platform/chat bound) — the restart still proceeds, just without a
    per-chat comeback notice.
    """
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_context import get_session_env

    platform_name = get_session_env("HERMES_SESSION_PLATFORM", "").strip()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    if not platform_name or not chat_id:
        return None
    try:
        platform = Platform(platform_name)
    except ValueError:
        return None
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip() or "dm",
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "").strip() or None,
        user_id=get_session_env("HERMES_SESSION_USER_ID", "").strip() or None,
        scope_id=get_session_env("HERMES_SESSION_SCOPE_ID", "").strip() or None,
    )


def _result_json(runner: Any, status: dict) -> str:
    return json.dumps(
        {
            "success": True,
            "status": status.get("status"),
            "draining": bool(getattr(runner, "_draining", False)),
            "active_agents": status.get("active_agents", 0),
            "via_service": status.get("via_service"),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _error_json(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def handle_restart(args: dict, **_: Any) -> str:
    """Restart the gateway via the shared /restart drain path.

    Contract (JSON string, like other tools):

    1. Cron sessions are refused — cron restarting the gateway is the
       SIGTERM-respawn loop the lifecycle guard exists to stop.
    2. No live runner → ``{"success": false, "error": ...}``.
    3. A restart already draining/queued → in-progress status, and
       ``request_restart`` is NOT called a second time.
    4. Otherwise the same supervisor/container branch as ``/restart`` runs
       (inside ``begin_user_restart``) and the requester's routing is
       persisted to ``.restart_notify.json`` for the comeback notice.
    5. Returns immediately — the bounce happens after this turn ends.
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

    source = _source_from_session_context()
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip() or None
    begin = runner.begin_user_restart(source=source, message_id=message_id)

    # Tool handlers run in a worker thread, and request_restart() calls
    # asyncio.create_task — the coroutine must land on the gateway event
    # loop, never on this thread's (nonexistent) loop.
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or loop.is_closed():
        begin.close()
        return _error_json(
            "The gateway event loop is not running, so a restart cannot be "
            "started from here."
        )

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if current_loop is not loop:
        future = asyncio.run_coroutine_threadsafe(begin, loop)
        try:
            status = future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
        except Exception as exc:
            future.cancel()
            logger.warning("restart tool: begin_user_restart failed: %s", exc)
            return _error_json(f"Failed to begin gateway restart: {exc}")
        return _result_json(runner, status)

    # Already on the gateway loop (a sync handler invoked inline rather than
    # from the executor): blocking on our own loop would deadlock, so
    # schedule the shared coroutine and report the pre-dispatch state. The
    # coroutine re-checks _restart_requested/_draining itself, so a concurrent
    # request still cannot double-enter request_restart.
    if getattr(runner, "_restart_requested", False) or getattr(
        runner, "_draining", False
    ):
        begin.close()
        return _result_json(
            runner,
            {
                "status": "already_in_progress",
                "active_agents": runner._running_agent_count(),
                "via_service": None,
            },
        )
    task = loop.create_task(begin)
    background = getattr(runner, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)
    from gateway.restart import user_restart_via_service

    return _result_json(
        runner,
        {
            "status": "restarting",
            "active_agents": runner._running_agent_count(),
            "via_service": user_restart_via_service(),
        },
    )
