"""``restart`` tool — agent-callable gateway restart on the /restart drain path.

When other sessions or background work are in flight, the tool pings the
requester in the calling chat and waits for them to type the exact word
``restart`` before anything happens (same primitive as ``clarify``: an
open-ended registration whose reply the gateway text-intercept eats instead of
starting a new turn). After the word lands — and once those other sessions
have finished — it calls ``GatewayRunner.begin_user_restart``, the same shared
entry point the ``/restart`` slash command uses, so the two cannot drift
apart: in-flight turns drain first, then the gateway bounces and comes back
online. When this session is provably the only work in flight, the ping is
skipped and the drain is queued outright — with no one else to time the bounce
around, the confirmation has nothing to protect. The
shell/systemctl path stays blocked by the lifecycle guard — that path SIGTERMs
the gateway and kills whatever child was running the command.

On Discord, the calling thread is temporarily retitled ``Restart Pending``
while the tool waits for the word, then restored to its exact original name on
every exit — via a small optional adapter capability, so the tool itself stays
platform-safe. The original name is captured by a read-only phase before the
renaming edit is submitted, so even a rename whose response is lost after
Discord applied it cannot lose the restore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESTART_SCHEMA = {
    "name": "restart",
    "description": (
        "Restart the Hermes gateway — the same drain path as the /restart "
        "slash command. When other sessions or background work are in "
        "flight, the requester is pinged in the current chat and must reply "
        "with the exact word `restart` before anything happens; anything "
        "else cancels. After the exact word, the bounce waits for those "
        "other sessions to finish before firing. When this is the only "
        "active session, there is no ping — the restart is queued outright "
        "and fires after this turn ends. In-flight turns (including this "
        "one) drain first, then the gateway stops and comes back online, "
        "and the requesting chat gets a comeback notice. Use it to pick up "
        "config or code changes, or to recover a misbehaving gateway."
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

# The same gate, said honestly when other sessions are still working: the
# bounce will not fire the moment the word lands, but once they finish.
_CONFIRM_PROMPT_WAITING_ON_OTHERS = (
    "Gateway restart requested — reply with the exact word `restart` "
    "(lowercase, on its own) and the gateway will bounce once the other "
    "active sessions finish. Anything else cancels."
)

# One-shot note after a successful confirm while other work still runs: the
# bounce is armed, it is just waiting on the other sessions. Plain text on
# every platform (no embed, no progress bubble), sent at most once, and
# cosmetic on failure — it must never gate or cancel the restart.
_ARMED_NOTICE = (
    "Restart confirmed — waiting for the other active sessions to finish; "
    "the gateway bounces as soon as they do."
)

# How often the post-confirm idle wait re-checks the in-flight work from the
# tool's worker thread. The wait itself is unbounded like the confirm wait;
# this only paces the poll.
_IDLE_POLL_INTERVAL_S = 0.5

# Injectable so tests drive the idle wait deterministically instead of
# sleeping real seconds; production always uses time.sleep.
_idle_sleep = time.sleep

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
    from gateway.session_context import delivered_via_upstream_relay, get_session_env

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
        # Relay provenance has to survive this rebuild: a relay-delivered
        # Discord thread carries platform=discord, and without the flag
        # adapter resolution would reach the native Discord adapter —
        # retitling a thread this process may not own the bot for — or find
        # no adapter at all and refuse the confirmation. With it, the relay
        # adapter is selected, which has no title capability: a no-op, and
        # the prompt still goes out over the relay socket.
        delivered_via_upstream_relay=delivered_via_upstream_relay(),
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


def _extra_background_work_in_flight(runner: Any) -> bool:
    """Cron or API-server work that ``_running_agents`` cannot see.

    Both counters already swallow their own lookup errors and report 0
    (best-effort by design), so an error escaping one here means a runner too
    odd to reason about — count that as work in flight rather than idle.
    """
    for counter in ("_active_cron_job_count", "_active_api_run_count"):
        try:
            if int(getattr(runner, counter)()) > 0:
                return True
        except Exception:
            return True
    return False


def _other_active_work_in_flight(runner: Any, session_key: str) -> bool:
    """True when any in-flight work exists besides the calling session."""
    running = getattr(runner, "_running_agents", None)
    try:
        if running and any(key != session_key for key in running):
            return True
    except Exception:
        # Unreadable state looks busy, not idle — same stance as below.
        return True
    return _extra_background_work_in_flight(runner)


def _session_is_only_active_work(runner: Any, session_key: str) -> bool:
    """True only when the calling session is provably the sole work in flight.

    ``_running_agents`` is a live mapping view in production
    (``SessionFieldView``) and a plain dict on test doubles, so it is read
    duck-typed. Fail closed: an empty mapping, a missing/blank session key,
    or state that cannot be read is NOT proof of being alone — those keep
    the confirm-then-bounce path. Skipping the confirmation requires the
    session key to be present, alone, with no cron/API work beside it.
    """
    if not session_key:
        return False
    running = getattr(runner, "_running_agents", None)
    if running is None:
        return False
    try:
        if session_key not in running or len(running) != 1:
            return False
    except Exception:
        return False
    return not _extra_background_work_in_flight(runner)


def _wait_for_other_work_to_clear(runner: Any, session_key: str) -> None:
    """Block the tool's worker thread until no other in-flight work remains.

    Runs only after a successful confirm, so the bounce is already committed
    — this is a wait for *timing*, not a second gate: the same predicate as
    the skip check (still excluding the calling session), unbounded like the
    confirm wait, and a session that starts mid-wait simply extends it (the
    gateway is not idle yet). This never parks or steers the other chats and
    never touches ``_draining`` / ``_restart_requested`` —
    ``begin_user_restart`` owns all of that once this returns.
    """
    while _other_active_work_in_flight(runner, session_key):
        _idle_sleep(_IDLE_POLL_INTERVAL_S)


def _send_armed_notice(adapter: Any, loop: Any, source: Any) -> None:
    """Tell the requester the bounce is armed and waiting; cosmetic, one-shot."""
    if adapter is None or source is None or not getattr(source, "chat_id", ""):
        return
    metadata: dict = {}
    if getattr(source, "thread_id", None):
        metadata["thread_id"] = source.thread_id
    future, submit_error = _submit_to_loop(
        adapter.send(source.chat_id, _ARMED_NOTICE, metadata=metadata or None), loop
    )
    if submit_error is not None:
        logger.debug(
            "restart tool: armed notice could not be submitted: %s", submit_error
        )
        return
    try:
        future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        future.cancel()
        logger.debug("restart tool: armed notice failed: %s", exc)


def _drop_pending_confirm(clarify_mod: Any, clarify_id: str) -> None:
    """Disarm and reap a registered confirm prompt that will not be waited on.

    Used when the prompt send fails: a registered-but-never-delivered entry
    would otherwise stay armed and the gateway text-intercept would eat the
    requester's next message as its reply. Resolving with the empty sentinel
    disarms it; ``wait_for_response`` then pops the entry from the registry
    and the session index — this caller exits on the delivery error without
    ever waiting, so the reap has to happen here.

    The reap runs even when the resolve reports the entry was already
    resolved: a racing user reply sets the event (first-writer-wins, so the
    real reply stands and the sentinel is dropped) but only a wait pops the
    entry, and this caller never waits. A resolved-yet-still-registered
    entry would sit at the head of the session's clarify index and eat the
    replies meant for the session's next clarify.
    """
    try:
        clarify_mod.resolve_gateway_clarify(clarify_id, "")
        # An already-set event (our sentinel or the racing reply) returns
        # at once; an entry reaped elsewhere returns None. Nothing blocks.
        clarify_mod.wait_for_response(clarify_id, 1.0)
    except Exception:
        logger.debug("Failed to drop pending restart confirm", exc_info=True)


def _submit_to_loop(coro: Any, loop: Any) -> tuple[Any, Optional[Exception]]:
    """Schedule *coro* on the gateway loop; ``(future, None)`` or ``(None, exc)``.

    ``asyncio.run_coroutine_threadsafe`` RAISES outright when the submission
    itself fails — a loop that closed between the caller's liveness check and
    this hop — and the coroutine it was handed never runs, so it would leak
    un-awaited. Closing it here and returning the error instead keeps every
    caller on its logged-cosmetic path: a dead-loop race is an ordinary
    failure to deliver/retitle, never an exception escaping the restart gate
    past a registered confirm prompt.
    """
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop), None
    except Exception as exc:
        coro.close()
        return None, exc


def _deliver_confirm_prompt(
    adapter: Any,
    loop: Any,
    source: Any,
    prompt: str,
    content: str,
    metadata: dict,
) -> Optional[str]:
    """Deliver the confirmation prompt; ``None`` when exactly one landed.

    Discord adapters that offer ``send_restart_confirmation`` render one
    dedicated confirmation embed (requester ping included) — the Discord
    presentation lives in the adapter, not here. Every other adapter,
    Discord relay included, keeps the plain-text prompt. A rich send that
    definitively failed (``SendResult.success`` False — pre-send failures
    only; the adapter reports success only once the message exists) falls
    back to exactly one plain prompt, so the requester never sees both and
    never sees neither. A rich send that RAISES (the adapter lets
    ``channel.send`` exceptions propagate precisely because the message
    may already exist) is ambiguous: disarm, cancel, and no fallback.
    """
    from gateway.config import Platform

    rich_send = getattr(adapter, "send_restart_confirmation", None)
    use_rich = source.platform == Platform.DISCORD and callable(rich_send)
    if use_rich:
        coro = rich_send(
            chat_id=source.chat_id,
            prompt=prompt,
            requester_user_id=str(getattr(source, "user_id", "") or ""),
            metadata=metadata or None,
        )
    else:
        coro = adapter.send(source.chat_id, content, metadata=metadata or None)

    send_future, submit_error = _submit_to_loop(coro, loop)
    if submit_error is not None:
        # The loop closed before the send could be scheduled — nothing was
        # delivered, so the caller cancels and disarms the registration.
        return f"Failed to deliver the restart confirmation prompt: {submit_error}"
    try:
        send_result = send_future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        # Ambiguous delivery (timeout / dead loop): same boundary rule as
        # clarify — never assume the message did not land, so no duplicate
        # plain fallback. The caller cancels and disarms the registration.
        send_future.cancel()
        return f"Failed to deliver the restart confirmation prompt: {exc}"
    if send_result is None or getattr(send_result, "success", True) is not False:
        return None

    error = (
        "Failed to deliver the restart confirmation prompt: "
        f"{getattr(send_result, 'error', None) or 'send failed'}"
    )
    if not use_rich:
        return error

    # The embed never landed — one plain prompt, requester mention included.
    fallback_future, submit_error = _submit_to_loop(
        adapter.send(source.chat_id, content, metadata=metadata or None), loop
    )
    if submit_error is not None:
        return f"Failed to deliver the restart confirmation prompt: {submit_error}"
    try:
        fallback_result = fallback_future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        fallback_future.cancel()
        return f"Failed to deliver the restart confirmation prompt: {exc}"
    if (
        fallback_result is not None
        and getattr(fallback_result, "success", True) is False
    ):
        return (
            "Failed to deliver the restart confirmation prompt: "
            f"{getattr(fallback_result, 'error', None) or 'send failed'}"
        )
    return None


def _begin_restart_pending_thread_title(adapter: Any, loop: Any, source: Any) -> Any:
    """Retitle the calling Discord thread before the prompt can land.

    Two round trips, because one is not enough to be robust: first the
    adapter's read-only ``capture_...`` resolves the exact thread and hands
    back its exact original name — so this thread HOLDS the restore state
    before anything is mutated — and only then is the mutating edit
    submitted as its own round trip. A rename Discord applied whose
    response then stalls past the round-trip bound times out here with an
    unknowable outcome, and the captured state survives it: the exit-time
    restore below still fires (idempotent if the edit never landed), where
    dropping the name on that timeout is what would queue the restart over
    a thread stuck on ``Restart Pending``.

    Returns the invocation-scoped restore state, or ``None`` when nothing
    was renamed: no capability (every non-Discord adapter and the Discord
    relay), no thread bound, or a cosmetic capture failure — all logged and
    none fatal to the restart gate. Every failure mode, including a
    submission that cannot be scheduled at all, is a logged cosmetic outcome;
    a rename that was never scheduled still returns the captured state so the
    idempotent exit-time restore runs anyway.
    """
    from gateway.config import Platform

    if source is None:
        return None
    capture = getattr(adapter, "capture_restart_pending_thread_title", None)
    begin = getattr(adapter, "begin_restart_pending_thread_title", None)
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if (
        not thread_id
        or getattr(source, "platform", None) != Platform.DISCORD
        or not callable(capture)
        or not callable(begin)
    ):
        return None

    future, submit_error = _submit_to_loop(capture(thread_id=thread_id), loop)
    if submit_error is not None:
        logger.warning(
            "restart tool: temporary thread title capture could not be "
            "submitted (cosmetic): %s",
            submit_error,
        )
        return None
    try:
        restore = future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        future.cancel()
        logger.warning(
            "restart tool: temporary thread title capture failed (cosmetic): %s", exc
        )
        return None
    if restore is None:
        return None

    edit_future, submit_error = _submit_to_loop(begin(restore), loop)
    if submit_error is not None:
        # The edit was never scheduled, so nothing was mutated — keep the
        # captured state anyway: the exit-time restore is idempotent, and
        # discarding it here on a submit race is the same
        # stranded-"Restart Pending" bug as losing it on a stalled response.
        logger.warning(
            "restart tool: temporary thread title rename could not be "
            "submitted (cosmetic): %s",
            submit_error,
        )
        return restore
    try:
        edit_future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        # The edit may already be applied on Discord's side — keep the
        # captured state so the restore still runs; losing it here is the
        # stranded-"Restart Pending" bug, and a redundant restore is inert.
        edit_future.cancel()
        logger.warning(
            "restart tool: temporary thread title rename outcome unknown "
            "(cosmetic): %s",
            exc,
        )
    return restore


def _restore_thread_title(adapter: Any, loop: Any, restore: Any) -> None:
    """Restore the thread title captured before the wait; cosmetic, non-fatal.

    Runs on every exit from the confirm wait and completes before the caller
    can queue the restart, so ``Restart Pending`` never outlives the wait.
    A failed restore is logged here, never raised — it must not cancel,
    gate, or queue the restart by itself.
    """
    if restore is None:
        return
    end = getattr(adapter, "end_restart_pending_thread_title", None)
    if not callable(end):
        return
    future, submit_error = _submit_to_loop(end(restore), loop)
    if submit_error is not None:
        logger.warning(
            "restart tool: temporary thread title restore could not be "
            "submitted (cosmetic): %s",
            submit_error,
        )
        return
    try:
        future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        future.cancel()
        logger.warning(
            "restart tool: temporary thread title restore failed (cosmetic): %s", exc
        )


def _confirm_restart_with_requester(
    runner: Any,
    loop: Any,
    source: Any,
    session_key: str,
    prompt: str = _CONFIRM_PROMPT,
) -> Optional[str]:
    """Ping the requester and block until they type the exact word.

    Returns ``None`` when the requester confirmed; otherwise a human-readable
    reason the restart must not happen. Runs on the tool's worker thread —
    the blocking wait is a ``threading.Event`` (same primitive as
    ``clarify``), so the gateway event loop stays free while the platform
    adapters resolve the reply. ``prompt`` is the question the requester
    sees; the caller picks the wording for whether other work is in flight.
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

    content = prompt
    # Discord: the ping is the point — prefix the requester's snowflake so
    # the plain prompt notifies them. This full-text prompt is what non-
    # Discord adapters send and what the rich path falls back to; the rich
    # embed message instead shows the prompt once (embed-only) with just
    # the mention as its content. Neither path goes through send_clarify,
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
        question=prompt,
        choices=None,
    )

    # The thread itself becomes the pending indicator: retitled before the
    # prompt can land, restored on every exit below — and always before the
    # caller can queue the restart. The setup runs INSIDE the guarded region
    # so even an unexpected failure there cannot jump past the restore (and
    # every submission failure it can hit is already a logged cosmetic no-op
    # inside the helper).
    title_restore = None
    try:
        title_restore = _begin_restart_pending_thread_title(adapter, loop, source)
        deliver_error = _deliver_confirm_prompt(
            adapter, loop, source, prompt, content, metadata
        )
        if deliver_error is not None:
            _drop_pending_confirm(clarify_gateway, clarify_id)
            return deliver_error

        response = clarify_gateway.wait_for_response(clarify_id, 0)
    finally:
        _restore_thread_title(adapter, loop, title_restore)

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
    4. When this session is provably the only work in flight (its key is the
       lone ``_running_agents`` entry and no cron/API work runs), the
       confirmation is skipped and the drain is queued outright — with no
       other session to time the bounce around, the ping has nothing to
       protect.
    5. Otherwise the requester is pinged in the calling chat and the call
       blocks until they reply. Only the exact word ``restart`` (after
       ``strip()``, nothing else — not ``Restart``, not ``yes``, not
       ``/restart``) confirms; anything else or empty cancels with
       ``{"success": false, "status": "cancelled"}``. While the tool waits,
       a Discord thread is temporarily retitled ``Restart Pending`` and
       restored to its exact original name on every exit — before the
       restart can be queued, and cosmetically (a rename or restore failure
       is logged, never fatal).
    6. After a successful confirm with other work still in flight, the
       bounce waits (unbounded, from this worker thread) until that work
       clears, then proceeds to the same supervisor/container branch as
       ``/restart`` inside ``begin_user_restart``, with the requester's
       routing persisted to ``.restart_notify.json`` for the comeback
       notice.
    7. On confirmation (or a skipped confirm), returns once the restart is
       queued — the bounce happens after this turn ends.
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

    if not _session_is_only_active_work(runner, session_key):
        # Other work is in flight — or aloneness cannot be proven — so keep
        # today's timing gate: the exact word `restart` confirms, anything
        # else cancels, and the wait for the reply stays unbounded.
        confirm_error = _confirm_restart_with_requester(
            runner,
            loop,
            source,
            session_key,
            prompt=(
                _CONFIRM_PROMPT_WAITING_ON_OTHERS
                if _other_active_work_in_flight(runner, session_key)
                else _CONFIRM_PROMPT
            ),
        )
        if confirm_error is not None:
            return _cancelled_json(confirm_error)

        # The word landed, so the bounce is committed. When other work is
        # still in flight, hold the drain until it clears — bouncing under
        # the other sessions is exactly what the confirm just timed us
        # around. If they already finished, the wait is a no-op and the
        # drain queues immediately.
        if _other_active_work_in_flight(runner, session_key):
            try:
                notice_adapter = runner._adapter_for_source(source)
            except Exception:
                notice_adapter = None
            _send_armed_notice(notice_adapter, loop, source)
            _wait_for_other_work_to_clear(runner, session_key)

    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip() or None
    begin = runner.begin_user_restart(source=source, message_id=message_id)
    future, submit_error = _submit_to_loop(begin, loop)
    if submit_error is not None:
        logger.warning(
            "restart tool: begin_user_restart could not be submitted: %s", submit_error
        )
        return _error_json(f"Failed to begin gateway restart: {submit_error}")
    try:
        status = future.result(timeout=_BEGIN_RESTART_TIMEOUT_S)
    except Exception as exc:
        future.cancel()
        logger.warning("restart tool: begin_user_restart failed: %s", exc)
        return _error_json(f"Failed to begin gateway restart: {exc}")
    return _result_json(runner, status)
