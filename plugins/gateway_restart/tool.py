"""``restart`` tool — agent-callable gateway restart on the /restart path.

The tool queues through the same gateway-owned sequence as the ``/restart``
slash command: warn about uncommitted runtime source, persist the requester's
comeback routing, then hand off to ``GatewayRunner.request_restart``. The
successor instance spawns immediately and the transport hub routes all new
turns to it, while the old process only stays alive to finish its
long-running turns before exiting. The tool never runs a wait of its own.
The shell/systemctl path stays blocked by the lifecycle guard — that path
SIGTERMs the gateway and kills whatever child ran the command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from agent.turn_control import turn_control_field_for

logger = logging.getLogger(__name__)

RESTART_SCHEMA = {
    "name": "restart",
    "description": (
        "Restart hermes-ide through the same path as the /restart slash "
        "command. New turns and other sessions' long running turns "
        "continue in a new instance. A success reply here only means the "
        "restart was queued (status restarting/already_in_progress/forwarded), "
        "never completed; completion is only the rewritten result below. "
        "This call only requests the restart; "
        "the old process terminates this turn mechanically. The successor "
        "process rewrites this tool result with lines beginning 'Restarted "
        "successfully' plus 'Old process: <old_id>' and 'New process: "
        "<new_id>'. That result is context for you, not a message to send "
        "to the user: do not repeat or quote it. Continue the interrupted "
        "task using your tools and only then reply about that work, and "
        "do not call restart again for the same restart. Use this to pick "
        "up config or code changes, or to recover a misbehaving gateway."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "restart_hub": {
                "type": "boolean",
                "description": (
                    "Also restart hermes-discord-hub.service. The successor "
                    "gateway process performs the bounce after it comes up "
                    "(never this dying process), so the hub's Discord/Matrix "
                    "sessions re-attach to a fresh hub alongside the gateway. "
                    "Default false: no hub restart."
                ),
            },
        },
        "additionalProperties": False,
    },
}

# Gateway-loop round trips (the state probe, then the queued restart) are
# quick — the shared on-loop sequence synchronously writes one small marker
# file and hands off to
# request_restart, which schedules the drain task. The synchronous marker
# write matters: it cannot keep running past a cancelled hand-off the way a
# thread-pool write can, so a timeout can never leave a phantom notify marker
# behind. The bound exists so a wedged loop fails the tool call instead of
# hanging the worker thread forever. A timeout cancels the queued restart
# mid-setup; nothing before the request_restart call mutates admission, and
# request_restart owns rolling its own setup back, so the failed hand-off
# leaves the gateway retryable and never reaches stop().
_BEGIN_RESTART_TIMEOUT_S = 15.0

# Control-socket transport bounds: the reachability probe in check_fn stays
# cheap (a listing must not stall on a dead socket), while the restart request
# itself gets the same room the in-process hand-off has — the verb's queue
# sequence includes a bounded preflight before it acks.
_SOCKET_PROBE_TIMEOUT_S = 1.0
_SOCKET_RESTART_TIMEOUT_S = _BEGIN_RESTART_TIMEOUT_S

_NO_GATEWAY_ERROR = (
    "No running gateway answered on the control socket — the restart tool "
    "needs a live gateway to queue into. Start the gateway, or from an "
    "operator shell run `hermes gateway restart`."
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

# Self-describing queue state for every successful (committed) restart reply:
# `success` means the restart was queued, never completed, and `process` is
# the OLD gateway. Only the successor's rewrite claims completion.
_QUEUED_NOTE = (
    "Restart queued, not completed: 'process' names the OLD gateway. "
    "The successor process rewrites this result with the "
    "'Restarted successfully' completion notice and Old/New process "
    "identity."
)


def check_restart_requirements() -> bool:
    """True when a restart target is reachable: this process OR its control socket.

    Agent turns run in restart-safe worker processes outside the GatewayRunner
    process, so the in-process runner ref is usually absent there — the
    control-socket probe (``identify`` against ``get_hermes_home()``) answers
    the real reachability question instead. Either path answering counts.
    Cheap and never raises; the registry TTL-caches the result process-wide,
    so the probe runs once per cache window, not per tool listing.
    """
    try:
        from gateway.run import _gateway_runner_ref

        if _gateway_runner_ref() is not None:
            return True
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        from gateway.control_socket import identify_gateway

        return isinstance(
            identify_gateway(get_hermes_home(), timeout=_SOCKET_PROBE_TIMEOUT_S), dict
        )
    except Exception:
        return False


def _forwarded_process_note(target_pid: int, forwarding_pid: Optional[int]) -> str:
    """Self-describing note for a forwarded restart result (both transports)."""
    forwarder = f" (pid {forwarding_pid})" if forwarding_pid else ""
    return (
        "Restart queued, not completed: 'process' names the newest generation "
        f"(pid {target_pid}), which took the request and spawns its own successor. "
        f"The generation that forwarded it{forwarder} is superseded and will not "
        "perform the restart; that successor process rewrites this result with "
        "the 'Restarted successfully' completion notice."
    )


def _result_json(runner: Any, status: dict) -> str:
    payload = {
        "success": True,
        "status": status.get("status"),
        "process": f"pid {os.getpid()}",
        "active_agents": status.get("active_agents", 0),
        "via_service": status.get("via_service"),
        # Self-describing queue state: `success` only means the restart was
        # committed, never completed. `process` names the OLD gateway (this
        # draining one); the successor rewrite is the completion claim.
        "queued": True,
        "note": _QUEUED_NOTE,
    }
    forwarded_to = status.get("forwarded_to")
    if isinstance(forwarded_to, int) and forwarded_to > 0:
        payload["forwarded_to"] = forwarded_to
        if str(status.get("status")) == "forwarded":
            # A forwarded restart runs on the NEWEST generation, not this
            # superseded forwarder — naming this process would point the
            # agent at a process that will not restart.
            payload["process"] = f"pid {forwarded_to}"
            payload["note"] = _forwarded_process_note(forwarded_to, os.getpid())
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


async def _queue_user_restart(
    runner: Any, source: Any, message_id: Optional[str], *, restart_hub: bool = False,
) -> dict:
    """Queue the restart through the ONE shared gateway sequence.

    Thin delegation to ``gateway.restart.queue_user_restart`` — the
    gateway-owned helper the ``/restart`` slash handler also calls — so this
    path owns no requester-setup choreography of its own and imports no core
    privates; the two user-restart entry points cannot drift apart. Must run
    on the gateway event loop (``request_restart`` schedules its drain task
    there), and returns the shared status dict shape: ``status``
    ("restarting" / "already_in_progress" / "forwarded"), ``active_agents`` (counted
    before the drain closes in), and ``via_service`` (None when nothing was
    started this call).
    """
    from gateway.restart import queue_user_restart

    return await queue_user_restart(
        runner, source, message_id, restart_hub=restart_hub
    )


def _socket_result_json(ack: dict) -> str:
    """Success payload for a control-socket ack (same keys as the in-process path).

    The gateway's PID comes from the ACK, never from this worker process.
    The turn-control stamp is identical to `_result_json` so the tool executor
    ends the turn the same way on either transport.
    """
    payload = {
        "success": True,
        "status": ack.get("status"),
        "active_agents": ack.get("active_agents", 0),
        "via_service": ack.get("via_service"),
        # Same self-describing queue state as `_result_json`, unless the
        # caller already composed a process-specific note (the ack
        # rewrite's chained / forwarded note).
        "queued": True,
        "note": ack.get("note") or _QUEUED_NOTE,
    }
    forwarded_to = ack.get("forwarded_to")
    if isinstance(forwarded_to, int) and forwarded_to > 0:
        payload["forwarded_to"] = forwarded_to
    process = ack.get("pid")
    if isinstance(process, int) and not isinstance(process, bool) and process > 0:
        payload["process"] = f"pid {process}"
    control_field = turn_control_field_for(ack.get("status"))
    if control_field:
        payload.update(control_field)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _restart_requester_from_session() -> Optional[dict]:
    """Build the restart-notify requester shape from the worker's bound chat."""
    from gateway.session_context import delivered_via_upstream_relay, get_session_env

    requester = {}
    for field in (
        "platform", "chat_id", "chat_type", "thread_id", "user_id", "scope_id",
        "message_id",
    ):
        value = get_session_env(f"HERMES_SESSION_{field.upper()}", "").strip()
        if value:
            requester[field] = value
    if not requester.get("platform") or not requester.get("chat_id"):
        return None
    if delivered_via_upstream_relay():
        requester["delivered_via_upstream_relay"] = True
    return requester

def _serving_identity_from_session() -> Optional[int]:
    """The gateway pid serving THIS turn, from the session's serving stamp.

    ``None`` when the stamp is absent (a gateway whose build predates it,
    or a surface that never binds one) — callers must keep their legacy
    behavior in that case, not refuse.
    """
    from gateway.session_context import get_session_env

    try:
        pid = int(get_session_env("HERMES_SESSION_GATEWAY_PID", "").strip() or 0)
    except ValueError:
        return None
    return pid if pid > 0 else None

def _live_generation_rows() -> Optional[list]:
    """Live generation-registry rows (the hub's mirror file), or None.

    The same spawn-ordered, fingerprint-verified snapshot the gateway's own
    forward decision reads — one bounded file read. None on any failure:
    an unreadable registry proves nothing, and the owner guard keeps
    refusing rather than guessing.
    """
    try:
        from gateway.overlap_registry import read_live_rows

        return read_live_rows()
    except Exception:
        return None


def _positive_pid(value: Any) -> bool:
    """True for a usable process id (bools are ints — exclude them)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _registry_rows_attest_forward(rows: Any, serving_pid: int, answer_pid: int) -> bool:
    """Does the live generation registry prove the array forward?

    Rows are the spawn-ordered, fingerprint-verified successor records of
    ``gateway.overlap_registry.read_live_rows``, so the LAST row is the
    newest live generation. The forward is proven when the answerer IS
    that newest registered generation and the requesting serving pid is
    positively listed — as a registered successor or as some row's
    predecessor (the original generation that spawned the line). A
    requester the registry never mentions is an unrelated process:
    unprovable, keep refusing.

    Shared contract with ``internal/servingidentity`` (Go twin), pinned by
    the shared honesty table.
    """
    row_pids: list = []
    listed: set = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        pid = row.get("pid")
        if not _positive_pid(pid):
            continue
        row_pids.append(pid)
        listed.add(pid)
        predecessor = row.get("predecessor_pid")
        if _positive_pid(predecessor):
            listed.add(predecessor)
    if not row_pids:
        return False
    return row_pids[-1] == answer_pid and serving_pid in listed


def socket_owner_action(serving_pid: Optional[int], identify: Any,
                        registry_rows: Any = None) -> str:
    """Pure decision: may a restart be queued through this socket answerer?

    ``"proceed"`` — the answerer is the serving process, or nothing is
    provable (no stamp / unparseable probe keeps today's behavior rather
    than refusing old installs);
    ``"refuse"`` — a different process answered and cannot attest this
    conversation's server as its hub-ready successor (the live incident:
    the draining predecessor owning the socket while its successor serves);
    ``"attested-chain"`` — a different process answered but attests the
    serving process as its hub-ready successor: queueing is correct and the
    result must name the serving process;
    ``"attested-forward"`` — a different, NEWER process answered: it
    reports role ``"serving"`` and the generation registry proves it the
    newest live generation of an array that positively lists the
    requesting server, so this is a forward to the newest — the array
    design (a restart on a draining generation forwards to the newest);
    queueing is correct and the result must name the newest answerer.

    ``registry_rows`` are the live generation-registry rows
    (``gateway.overlap_registry.read_live_rows``). Shared contract with
    ``internal/servingidentity`` (Go twin); pinned by the shared honesty
    table.
    """
    if not serving_pid or not isinstance(identify, dict):
        return "proceed"
    answer_pid = identify.get("pid")
    if not isinstance(answer_pid, int) or isinstance(answer_pid, bool):
        return "proceed"
    if answer_pid == serving_pid:
        return "proceed"
    overlap = identify.get("overlap")
    overlap = overlap if isinstance(overlap, dict) else {}
    if (
        overlap.get("role") == "draining-predecessor"
        and overlap.get("successor_pid") == serving_pid
        and overlap.get("successor_ready") is True
    ):
        return "attested-chain"
    if (
        overlap.get("role") == "serving"
        and _registry_rows_attest_forward(registry_rows, serving_pid, answer_pid)
    ):
        return "attested-forward"
    return "refuse"

# Refusal clause per reported overlap role. Both twins render the same
# table byte-identically (pinned by the shared honesty table); a role the
# table does not know renders the unknown-role clause — never a claim about
# the answerer's build. The empty-role entry is the legacy answerer: no
# overlap block at all.
_SOCKET_REFUSAL_CLAUSES = {
    "": "reports no restart-handoff state (its build predates overlap reporting)",
    "draining-predecessor": (
        "is draining for a restart whose successor is not confirmed as this "
        "conversation's server"
    ),
    "serving": (
        "is serving, not draining for a restart, and is not proven to share "
        "this conversation's generation array"
    ),
    "overlap-successor": (
        "is an overlap successor that does not attest this conversation's server"
    ),
    "unknown": "could not classify its own restart-handoff state",
}
_UNKNOWN_ROLE_CLAUSE = _SOCKET_REFUSAL_CLAUSES["unknown"]

def socket_owner_refusal(serving_pid: int, identify: Any) -> str:
    """The refusal message for an unprovable socket owner (pure builder).

    The owner clause is truthful per reported role — "its build predates
    overlap reporting" only for an answerer with no overlap block, never
    for a current-build serving answerer. Wording is a shared contract
    with ``internal/servingidentity`` (Go twin), pinned byte-identical by
    the shared honesty table.
    """
    overlap = identify.get("overlap") if isinstance(identify, dict) else {}
    overlap = overlap if isinstance(overlap, dict) else {}
    role = str(overlap.get("role") or "")
    owner_clause = _SOCKET_REFUSAL_CLAUSES.get(role, _UNKNOWN_ROLE_CLAUSE)
    answer_pid = identify.get("pid") if isinstance(identify, dict) else 0
    return (
        "No restart was queued: the gateway control socket is owned by pid "
        f"{answer_pid}, which {owner_clause}, but this conversation is served by "
        f"pid {serving_pid}. A restart queued through pid {answer_pid} would not "
        f"target the process running this conversation. It is already served by "
        f"pid {serving_pid}; if it still needs a restart, retry after the current "
        "handoff completes or run `hermes gateway restart` from an operator shell."
    )



def _socket_owner_guard(
    serving_pid: Optional[int], identify: Any, registry_rows: Any = None
) -> Optional[str]:
    """Refusal result when the socket owner is not the serving process.

    The ack's ``pid`` names the ANSWERING process — the socket owner. During
    an overlap restart that owner is the draining predecessor, and queueing a
    restart there chains onto a drain this conversation cannot influence (the
    field incident: the chained restart waited forever). Queueing is allowed
    only when the answerer is the serving process itself, positively
    attests THIS serving process as its hub-ready successor, or is proven
    the newest generation of this conversation's array (a draining
    generation's forward — ``registry_rows``).
    """
    if socket_owner_action(serving_pid, identify, registry_rows) != "refuse":
        return None
    return _error_json(socket_owner_refusal(serving_pid, identify))


def _socket_ack_names_restarting_process(
    ack: dict, serving_pid: Optional[int], identify: Any, registry_rows: Any = None
) -> dict:
    """Rewrite an overlap ack so ``process`` names the process that will restart.

    The ack's ``pid`` names the ANSWERING process — the socket owner, which
    during an overlap restart is a draining generation. Three proven shapes
    rename it: a ``forwarded`` ack already names the newest generation
    (``forwarded_to`` — the one that took the request and spawns its own
    successor); a ``chained`` ack from an older build is proven by the
    identify attestation (the guard's ``attested-chain``); and an
    ``attested-forward`` ask — a restart requested from an OLDER draining
    generation of the array — names the newest answerer, with the note
    stating the conversation's own process is the superseded forwarder.
    Either way rendering the answerer as ``process`` would point the agent
    at a dying process.
    """
    status = str(ack.get("status") or "")
    action = socket_owner_action(serving_pid, identify, registry_rows)
    if status == "forwarded":
        target = ack.get("forwarded_to")
        proven = _positive_pid(target)
    elif status == "chained":
        target = serving_pid
        proven = action == "attested-chain"
    elif action == "attested-forward":
        target = identify.get("pid") if isinstance(identify, dict) else None
        proven = _positive_pid(target)
    else:
        return ack
    if not proven:
        return ack
    answer_pid = ack.get("pid")
    if not _positive_pid(answer_pid):
        answer_pid = identify.get("pid") if isinstance(identify, dict) else None
    ack = dict(ack)
    ack["pid"] = target
    if status == "chained":
        ack["note"] = (
            f"Restart chained for the serving process: 'process' names pid {target}, "
            f"the process serving this conversation. The control socket is still "
            f"owned by the draining predecessor (pid {answer_pid}); the chained "
            f"restart runs on pid {target} once that handoff completes."
        )
    else:
        forwarder = serving_pid if action == "attested-forward" else answer_pid
        ack["note"] = _forwarded_process_note(target, forwarder)
    return ack


def _restart_via_control_socket(*, restart_hub: bool = False) -> str:
    """Queue the restart through the gateway's control socket (out-of-process path).

    The transport ``hermes gateway restart`` already uses: the socket verb
    runs the SAME ``queue_user_restart`` core on the gateway loop, so an
    agent turn in a restart-safe worker process (no in-process runner)
    reaches the identical drain pipeline. The socket's owner is verified
    against the session's serving stamp first: during an overlap handoff the
    draining predecessor owns the socket, and queueing there would chain a
    restart that waits on its drain. A contested owner is re-tried once
    with the live generation-registry evidence — a restart asked from an
    OLDER draining generation of the same array forwards to the NEWEST
    owner (the array design). Never raises — every failure maps to a typed
    error result.
    """
    from hermes_constants import get_hermes_home

    serving_pid = _serving_identity_from_session()
    identify = None
    if serving_pid:
        try:
            from gateway.control_socket import identify_gateway

            identify = identify_gateway(
                get_hermes_home(), timeout=_SOCKET_PROBE_TIMEOUT_S)
        except Exception:
            identify = None
    registry_rows = None
    guard = _socket_owner_guard(serving_pid, identify)
    if guard is not None:
        # The refusal may be a draining-generation ask the hub's registry
        # can prove: re-decide once with the live generation evidence, and
        # keep it for the ack rewrite (it names the newest answerer).
        registry_rows = _live_generation_rows()
        guard = _socket_owner_guard(serving_pid, identify, registry_rows)
    if guard is not None:
        return guard
    try:
        from gateway.control_socket import request_gateway_restart

        request_kwargs: dict[str, Any] = {
            "restart_hub": bool(restart_hub),
            "timeout": _SOCKET_RESTART_TIMEOUT_S,
        }
        requester = _restart_requester_from_session()
        if requester is not None:
            request_kwargs["requester"] = requester
        ack = request_gateway_restart(get_hermes_home(), **request_kwargs)
    except Exception as exc:
        logger.warning("restart tool: control-socket transport failed: %s", exc)
        return _error_json(_NO_GATEWAY_ERROR)
    if not isinstance(ack, dict):
        return _error_json(_NO_GATEWAY_ERROR)
    status = str(ack.get("status") or "")
    if not ack.get("queued") and status not in {"already_in_progress", "forwarded"}:
        # The gateway answered but refused (e.g. a drain already shutting
        # down, or the hand-off not observed in time) — surface its reason.
        return _error_json(
            f"The running gateway refused the restart: {ack.get('reason') or status or 'no reason given'}"
        )
    return _socket_result_json(
        _socket_ack_names_restarting_process(ack, serving_pid, identify, registry_rows))


def handle_restart(args: dict, **_: Any) -> str:
    """Queue a gateway restart via the shared /restart drain path.

    Cron sessions, outdated runners, a dead gateway loop, and an invocation on
    the gateway loop itself are refused. Without an in-process runner (agent
    turns run in worker processes) the gateway's control socket queues the
    same restart core instead. An already-committed restart returns
    ``already_in_progress`` without queueing another; a generic shutdown drain
    returns the existing error. Otherwise the shared queue is submitted to the
    gateway loop and its status becomes the tool result.
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
    restart_hub = is_truthy_value(args.get("restart_hub"))
    if runner is None:
        # Agent turns run in restart-safe worker processes outside the
        # GatewayRunner process — the normal gateway-chat case. The control
        # socket is the transport to the same restart core from there.
        return _restart_via_control_socket(restart_hub=restart_hub)

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
    if committed and not draining:
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
    if draining and not committed:
        return _error_json(
            "Gateway shutdown is already in progress; no restart was started."
        )
    # A committed overlap drain reaches the shared queue: with a pending
    # successor it returns ``chained``; without one it keeps the existing
    # already-in-progress result. Generic shutdowns were refused above.

    session_key = get_session_env("HERMES_SESSION_KEY", "").strip()
    get_cached_source = getattr(runner, "_get_cached_session_source", None)
    source = get_cached_source(session_key) if callable(get_cached_source) else None
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip() or None
    future, submit_error = _submit_to_loop(
        _queue_user_restart(runner, source, message_id, restart_hub=restart_hub), loop
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
