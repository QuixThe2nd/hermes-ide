"""``restart`` tool — agent-callable gateway restart on the /restart drain path.

The tool pings the requester in the calling chat and waits for them to type the
exact word ``restart`` before anything happens (same primitive as ``clarify``:
an open-ended registration whose reply the gateway text-intercept eats instead
of starting a new turn). Only then does it call
``GatewayRunner.begin_user_restart``, the same shared entry point the
``/restart`` slash command uses, so the two cannot drift apart: in-flight turns
drain first, then the gateway bounces and comes back online. The
shell/systemctl path stays blocked by the lifecycle guard — that path SIGTERMs
the gateway and kills whatever child was running the command.
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
        "slash command. The requester is pinged in the current chat and must "
        "reply with the exact word `restart` before anything happens; "
        "anything else cancels. Once confirmed, in-flight turns (including "
        "this one) finish first, then the gateway stops and comes back "
        "online, and the requesting chat gets a comeback notice. Use it to "
        "pick up config or code changes, or to recover a misbehaving "
        "gateway. Blocks until the requester replies; the restart itself "
        "fires after the current turn ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# Gateway-loop round trips (the confirm prompt send, then begin_user_restart)
# are quick — begin_user_restart only writes two small marker files and
# schedules the drain task. The bound exists so a wedged loop fails the tool
# call instead of hanging the worker thread forever.
_BEGIN_RESTART_TIMEOUT_S = 15.0

# The only reply that confirms a restart: this exact word, nothing else.
_CONFIRM_WORD = "restart"

_CONFIRM_PROMPT = (
    "Gateway restart requested — reply with the exact word `restart` "
    "(lowercase, on its own) to bounce the gateway now. Anything else "
    "cancels."
)

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
    platform/chat bound) — the restart then cannot be confirmed and is
    refused rather than bouncing unattended.
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


def _cancelled_json(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message, "status": "cancelled"},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _drop_pending_confirm(clarify_mod: Any, clarify_id: str) -> None:
    """Disarm a registered confirm prompt that will not be waited on.

    Used when the prompt send fails: a registered-but-never-delivered entry
    would otherwise stay armed and the gateway text-intercept would eat the
    requester's next message as its reply. Resolving with the empty sentinel
    and reaping via ``wait_for_response`` pops the entry from the registry.
    """
    try:
        if clarify_mod.resolve_gateway_clarify(clarify_id, ""):
            clarify_mod.wait_for_response(clarify_id, 1.0)
    except Exception:
        logger.debug("Failed to drop pending restart confirm", exc_info=True)


def _confirm_restart_with_requester(
    runner: Any,
    loop: Any,
    source: Any,
    session_key: str,
) -> Optional[str]:
    """Ping the requester and block until they type the exact word.

    Returns ``None`` when the requester confirmed; otherwise a human-readable
    reason the restart must not happen. Runs on the tool's worker thread —
    the blocking wait is a ``threading.Event`` (same primitive as
    ``clarify``), so the gateway event loop stays free while the platform
    adapters resolve the reply.
    """
    import uuid

    from gateway.config import Platform
    from tools import clarify_gateway

    if source is None or not source.chat_id or not session_key:
        return (
            "Cannot confirm the restart: this session has no chat to "
            "confirm in (no platform/chat or session key bound)."
        )

    try:
        adapter = runner._adapter_for_source(source)
    except Exception:
        adapter = None
    if adapter is None:
        return (
            "Cannot confirm the restart: no live adapter for this "
            "session's platform."
        )

    content = _CONFIRM_PROMPT
    # Discord: the ping is the point — prefix the requester's snowflake so
    # the prompt notifies them. Sent via plain send(), not send_clarify(),
    # so the `discord.clarify_mentions: false` opt-out cannot silence it.
    user_id = str(getattr(source, "user_id", "") or "")
    if source.platform == Platform.DISCORD and user_id.isdigit():
        content = f"<@{user_id}> {content}"
    metadata: dict = {}
    if source.thread_id:
        # Land in this Discord thread, not the parent channel.
        metadata["thread_id"] = source.thread_id

    # Register BEFORE sending: an open-ended clarify (choices=None) makes the
    # gateway text-intercept eat the requester's next message in this session
    # instead of starting a new turn.
    clarify_id = uuid.uuid4().hex[:10]
    clarify_gateway.register(
        clarify_id=clarify_id,
        session_key=session_key,
        # The plain prompt is the question; the Discord mention prefix stays
        # in the sent content only.
        question=_CONFIRM_PROMPT,
        choices=None,
    )

    send_future = asyncio.run_coroutine_threadsafe(
        adapter.send(source.chat_id, content, metadata=metadata or None),
        loop,
    )
    try:
        send_result = send_future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        send_future.cancel()
        _drop_pending_confirm(clarify_gateway, clarify_id)
        return f"Failed to deliver the restart confirmation prompt: {exc}"
    if send_result is not None and getattr(send_result, "success", True) is False:
        _drop_pending_confirm(clarify_gateway, clarify_id)
        return (
            "Failed to deliver the restart confirmation prompt: "
            f"{getattr(send_result, 'error', None) or 'send failed'}"
        )

    response = clarify_gateway.wait_for_response(clarify_id, 0)
    if str(response or "").strip() == _CONFIRM_WORD:
        return None
    return (
        "Restart cancelled — the reply was not the exact word "
        f"`{_CONFIRM_WORD}`."
    )


def handle_restart(args: dict, **_: Any) -> str:
    """Restart the gateway via the shared /restart drain path.

    Contract (JSON string, like other tools):

    1. Cron sessions are refused — cron restarting the gateway is the
       SIGTERM-respawn loop the lifecycle guard exists to stop.
    2. No live runner → ``{"success": false, "error": ...}``.
    3. A restart already draining/queued → in-progress status, no ping, and
       ``request_restart`` is NOT called a second time.
    4. Otherwise the requester is pinged in the calling chat and the call
       blocks until they reply. Only the exact word ``restart`` (after
       ``strip()``, nothing else — not ``Restart``, not ``yes``, not
       ``/restart``) proceeds to the same supervisor/container branch as
       ``/restart`` inside ``begin_user_restart``, with the requester's
       routing persisted to ``.restart_notify.json`` for the comeback
       notice. Anything else or empty cancels with
       ``{"success": false, "status": "cancelled"}``.
    5. On confirmation, returns once the restart is queued — the bounce
       happens after this turn ends.
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

    if getattr(runner, "_restart_requested", False) or getattr(
        runner, "_draining", False
    ):
        # A restart already in flight owns the notify payload — no ping, no
        # confirm wait, no second request_restart.
        return _result_json(
            runner,
            {
                "status": "already_in_progress",
                "active_agents": runner._running_agent_count(),
                "via_service": None,
            },
        )

    # Tool handlers run in a worker thread, and request_restart() calls
    # asyncio.create_task — every gateway-side coroutine (the confirm prompt
    # send included) must land on the gateway event loop, never on this
    # thread's (nonexistent) loop.
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
        # The confirm wait blocks a threading.Event until the requester
        # replies — doing that ON the gateway loop would freeze the whole
        # gateway, so an inline (non-executor) handler cannot use this tool.
        return _error_json(
            "The restart tool cannot wait for a confirmation on the gateway "
            "event loop; it must run from a tool worker thread."
        )

    source = _source_from_session_context()
    session_key = get_session_env("HERMES_SESSION_KEY", "").strip()
    confirm_error = _confirm_restart_with_requester(runner, loop, source, session_key)
    if confirm_error is not None:
        return _cancelled_json(confirm_error)

    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip() or None
    begin = runner.begin_user_restart(source=source, message_id=message_id)
    future = asyncio.run_coroutine_threadsafe(begin, loop)
    try:
        status = future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        future.cancel()
        logger.warning("restart tool: begin_user_restart failed: %s", exc)
        return _error_json(f"Failed to begin gateway restart: {exc}")
    return _result_json(runner, status)
