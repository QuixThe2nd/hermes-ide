"""Durable ``/v1/runs`` admission, status, events, and control handlers."""

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

try:
    from aiohttp import web
    from aiohttp.web_request import RequestKey
except ImportError:
    web = None  # type: ignore[assignment]
    RequestKey = None  # type: ignore[assignment,misc]

from gateway.platforms.api_server_room_grants import _json_error, _room_grant_error_response
from gateway.platforms.api_server_run_idempotency import TERMINAL_STATUSES


logger = logging.getLogger("gateway.platforms.api_server")
_ROOM_RETENTION_REQUEST_KEY = (
    RequestKey("hermes.room_run_retention_until", float) if RequestKey is not None
    else "hermes.room_run_retention_until")
# Forwarded subagent lifecycle fields; free-text ones are secret-redacted.
_SUBAGENT_EVENT_KEYS = (
    "goal", "task_count", "task_index", "subagent_id", "child_session_id", "delegation_id", "parent_id",
    "depth", "model", "tool_count", "status", "summary", "duration_seconds", "input_tokens",
    "output_tokens", "reasoning_tokens", "api_calls", "cost_usd", "files_read", "files_written",
    "output_tail")
_SUBAGENT_TEXT_KEYS = ("goal", "summary", "output_tail")
# Terminal usage payload: (wire key, agent attribute), in wire order.
_USAGE_FIELDS = (
    ("input_tokens", "session_prompt_tokens"), ("output_tokens", "session_completion_tokens"),
    ("total_tokens", "session_total_tokens"))
# Tool-progress event -> SSE payload fields (tool_name, preview, kwargs); key order is wire format.
_FIXED_EVENT_FIELDS = {
    "tool.started": lambda tool, preview, kw: {"tool": tool, "preview": preview},
    "tool.completed": lambda tool, preview, kw: {
        "tool": tool, "duration": round(kw.get("duration", 0), 3), "error": kw.get("is_error", False)},
    "reasoning.available": lambda tool, preview, kw: {"text": preview or ""}}

# Presentation bounds for the pending-clarify card this surface serves.
# They bound what leaves the process on the session clarify routes (and
# the run status/SSE mirrors of the card) regardless of what the agent
# asked: 8 choices x 500 chars, 2000 chars of question, 128-char ids.
_CLARIFY_MAX_CHOICES = 8
_CLARIFY_MAX_QUESTION_CHARS = 2000
_CLARIFY_MAX_CHOICE_CHARS = 500
_CLARIFY_ID_MAX_CHARS = 128
# One clarify answer: a non-empty string, or a bounded list of them
# (multi-select). Answers longer than this are refused, never truncated
# — a truncated answer would silently change the agent's decision.
_CLARIFY_MAX_RESPONSE_CHARS = 8192
_CLARIFY_MAX_RESPONSE_ITEMS = 16


def _redact_clarify_text(text: object) -> str:
    """Redact secrets from text bound for a clarify card.

    The question and choice labels are model-generated text that can
    echo credentials (a proposed command, a masked key); the card leaves
    the process on the session clarify routes, the run status, and the
    SSE stream, so it gets the same forced redaction error text gets.
    Lazy import keeps this module importable without the agent package;
    on import failure the text still passes through the bounds below —
    bounded-but-unredacted beats losing the card entirely.
    """
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(str(text), force=True)
    except Exception:  # pragma: no cover - defensive
        return str(text)


def _bounded_clarify_card(
    clarify_id: str,
    question: str,
    choices: Optional[List[str]],
    multi_select: bool,
) -> Dict[str, Any]:
    """Render one pending clarify as the bounded, redacted card the
    session clarify routes serve. The card is read-only presentation:
    ids, question text, choice labels and the multi-select flag, all
    redacted and clamped so no secret-bearing, payload-heavy, or
    oversized agent text crosses the API."""
    bounded: List[str] = []
    for choice in list(choices or [])[:_CLARIFY_MAX_CHOICES]:
        text = _redact_clarify_text(choice).strip()
        if text:
            bounded.append(text[:_CLARIFY_MAX_CHOICE_CHARS])
    return {
        "clarify_id": str(clarify_id)[:_CLARIFY_ID_MAX_CHARS],
        "question": _redact_clarify_text(question)[
            :_CLARIFY_MAX_QUESTION_CHARS
        ],
        "choices": bounded or None,
        "multi_select": bool(multi_select),
    }


def _remember_room_retention(request: "web.Request", claims: dict[str, Any]) -> None:
    value = float(claims.get("status_expires_at") or claims.get("expires_at") or 0)
    try:
        request[_ROOM_RETENTION_REQUEST_KEY] = value
    except (AttributeError, TypeError):
        setattr(request, "_hermes_room_run_retention_until", value)


def _room_retention_until(request: "web.Request") -> float:
    try:
        value = request.get(_ROOM_RETENTION_REQUEST_KEY, 0)
    except AttributeError:
        value = getattr(request, "_hermes_room_run_retention_until", 0)
    return max(0.0, float(value or 0))


def _run_event(run_id: str, name: str, **fields: Any) -> Dict[str, Any]:
    """Build one SSE event payload (key order is part of the wire format)."""
    return {"event": name, "run_id": run_id, "timestamp": time.time(), **fields}


def _run_not_found(_openai_error, run_id: str) -> "web.Response":
    return _json_error(_openai_error, f"Run not found: {run_id}", code="run_not_found", status=404)


def _uses_room_run_auth(self, request: "web.Request") -> bool:
    return request.path.endswith("/v1/runs") and bool(self._room_grant_token(request))


def _initialize_run_state(self, *, store_factory) -> None:
    """Initialize adapter-owned durable and live ``/v1/runs`` state."""
    self._run_idempotency_store = store_factory()
    self._run_owner_pid = os.getpid()
    try:
        from gateway.status import get_process_start_time
        self._run_owner_started = int(get_process_start_time(self._run_owner_pid) or 0)
    except Exception:
        self._run_owner_started = 0
    # All keyed by run_id: SSE queues (+creation time for the TTL sweep), connected
    # subscribers, live agent/task refs for cooperative stop (the executor thread may
    # outlive the request, hence the separate stopping set), pollable statuses, and
    # approval session keys (approval core resolves by session key, clients by run_id).
    self._run_idempotency_ids: set[str] = set()
    self._run_stream_subscribers: set[str] = set()
    self._stopping_run_ids: set[str] = set()
    (
        self._run_owners, self._run_streams, self._run_streams_created, self._active_run_agents,
        self._active_run_tasks, self._run_statuses, self._run_approval_sessions,
    ) = ({} for _ in range(7))
    # Pending run clarifies this API surface registered in
    # tools.clarify_gateway: clarify_id -> {run_id, session_id, profile}.
    # The registry itself stays process-wide in clarify_gateway; this map
    # only records which run/profile each entry belongs to so the session
    # clarify routes can fail closed on cross-run/cross-profile ids.
    self._run_clarify_registrations: Dict[str, Dict[str, str]] = {}
    # Control gate for the clarify lifecycle transitions that must be
    # atomic ACROSS threads (agent executor thread vs the event loop):
    # the clarify callback's admission+register+waiting-status section,
    # its settle section, and /stop's stopping-marker+cancel section.
    # Reentrant so those sections can call each other while holding it.
    # Fixed lock ordering: this gate is ALWAYS acquired before
    # ``clarify_gateway``'s registry lock (register/resolve run inside a
    # gate section) and never the reverse.
    self._run_clarify_lock = threading.RLock()


def _http_routes(self) -> list[tuple[str, str, Any]]:
    return [
        ("POST", "/v1/runs", self._handle_runs), ("GET", "/v1/runs/{run_id}", self._handle_get_run),
        ("GET", "/v1/runs/{run_id}/events", self._handle_run_events),
        ("POST", "/v1/runs/{run_id}/approval", self._handle_run_approval),
        ("POST", "/v1/runs/{run_id}/steer", self._handle_steer_run),
        ("POST", "/v1/runs/{run_id}/stop", self._handle_stop_run),
        # Session-scoped clarify surface: the run worker parks in
        # tools.clarify_gateway and these routes are how an external UI
        # (Mission Control) sees and answers the pending question for a
        # canonical session id.
        (
            "GET",
            "/api/sessions/{session_id}/clarify",
            self._handle_session_clarify_get,
        ),
        (
            "POST",
            "/api/sessions/{session_id}/clarify",
            self._handle_session_clarify_post,
        ),
    ]


def _idempotency_capabilities(self, *, store_type) -> dict[str, Any]:
    return {
        "supported": True,
        "durable": self._run_idempotency_store.durable,
        "retention_seconds": store_type.RETENTION_SECONDS}


def _close_run_state(self) -> None:
    try:
        if getattr(self, "_run_idempotency_store", None) is not None:
            self._run_idempotency_store.close()
    except Exception:
        logger.debug("Failed to close run idempotency store for %s", self.name, exc_info=True)


def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
    """Update pollable run status without exposing private agent objects."""
    now = time.time()
    current = self._run_statuses.get(run_id, {})
    previous_status = str(current.get("status") or "")
    field_names = set(fields)
    current.update({"object": "hermes.run", "run_id": run_id, "status": status, "updated_at": now})
    current.setdefault("created_at", fields.pop("created_at", now))
    current.update(fields)
    if status != "waiting_for_approval":
        current.pop("approval", None)
    if status != "waiting_for_clarify":
        current.pop("clarify", None)
    self._run_statuses[run_id] = current
    should_persist = (
        status != previous_status
        or status in TERMINAL_STATUSES
        or bool(field_names & {"output", "error", "usage", "pending_steer", "session_id"}))
    if run_id in self._run_idempotency_ids and should_persist:
        try:
            self._run_idempotency_store.update_status(run_id, current)
        except Exception:
            logger.exception("[api_server] failed to persist idempotent run status %s", run_id)
    return current


def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop", *, _api_server):
    """Return a callback that pushes structured events to the run SSE queue."""
    redact_sensitive_text = _api_server.redact_sensitive_text

    def _push(event: Dict[str, Any]) -> None:
        self._set_run_status(
            run_id, self._run_statuses.get(run_id, {}).get("status", "running"), last_event=event.get("event"))
        q = self._run_streams.get(run_id)
        if q is not None:
            with suppress(Exception):
                loop.call_soon_threadsafe(q.put_nowait, event)

    def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
        # _thinking / subagent.tool / subagent_progress are deliberately dropped (UI noise);
        # lifecycle boundaries must land so clients can observe delegate_task failures.
        fields = _FIXED_EVENT_FIELDS.get(event_type)
        if fields is not None:
            _push(_run_event(run_id, event_type, **fields(tool_name, preview, kwargs)))
        elif event_type in {"subagent.start", "subagent.complete"}:
            event = _run_event(run_id, event_type)
            if preview is not None:
                event["preview"] = redact_sensitive_text(str(preview), force=True)
            for key in _SUBAGENT_EVENT_KEYS:
                value = kwargs.get(key)
                if value is not None:
                    # Free text may carry child tool output: force secret redaction on this public stream.
                    redact = key in _SUBAGENT_TEXT_KEYS and isinstance(value, str)
                    event[key] = redact_sensitive_text(value, force=True) if redact else value
            _push(event)
        # _thinking, subagent.tool, and subagent_progress are intentionally
        # not forwarded on the /v1/runs stream: they are high-volume UI
        # noise. Lifecycle boundaries (start/complete) still need to land
        # so clients can observe delegate_agent timeouts and failures.

    return _callback


def _make_run_clarify_callback(
    self,
    run_id: str,
    session_id: str,
    profile: str,
    put_event,
):
    """Build the blocking clarify callback for one admitted API run.

    Mirrors the native gateway's ``_clarify_callback_sync`` contract (see
    gateway/run.py): the agent's worker thread calls into the ``clarify``
    tool, which invokes this callback and blocks until a response
    arrives. The pending question is registered in the process-wide
    ``tools.clarify_gateway`` under the run's *canonical session id* —
    the exact id the 202 response exposed, so a client that navigated to
    that session can find the card on the session clarify route — and in
    the per-run registration map so those routes can fail closed on
    cross-run/cross-session/cross-profile clarify ids.

    ``put_event`` is the run's active-transport enqueue (already guarded
    against a retired stream), so the card also reaches SSE/status
    pollers as ``waiting_for_clarify``. The worker may block for the
    whole clarify timeout; the HTTP request that admitted the run has
    long since returned 202.
    """

    def _clarify_callback(
        question: str, choices, multi_select: bool = False
    ) -> str:
        from tools import clarify_gateway as _clarify_mod
        from tools.clarify_tool import TIMEOUT_RESPONSE
        import uuid as _uuid

        # Admission, registry write, and the waiting status form ONE
        # atomic section on the run clarify gate — the same reentrant
        # lock /stop's stopping-marker+cancel section takes. Without it,
        # a /stop landing between the register and the status write
        # would cancel a registration the map had not recorded yet (the
        # agent then blocks for the whole clarify timeout with a prompt
        # nothing can answer), or the waiting status could overwrite
        # "stopping" and the settle path would resurrect the run to
        # "running". A callback that arrives after the stop (or after
        # the run went terminal) returns the canonical timeout sentinel
        # without registering anything.
        with self._run_clarify_lock:
            if (
                run_id in self._stopping_run_ids
                or (self._run_statuses.get(run_id) or {}).get("status")
                in {
                    "stopping",
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                }
            ):
                return TIMEOUT_RESPONSE
            clarify_id = _uuid.uuid4().hex[:10]
            _clarify_mod.register(
                clarify_id=clarify_id,
                session_key=session_id,
                question=str(question or ""),
                choices=[str(c) for c in choices] if choices else None,
                multi_select=bool(multi_select),
            )
            self._run_clarify_registrations[clarify_id] = {
                "run_id": run_id,
                "session_id": session_id,
                "profile": profile or "default",
            }
            card = _bounded_clarify_card(
                clarify_id, question, choices, bool(multi_select)
            )
            self._set_run_status(
                run_id,
                "waiting_for_clarify",
                last_event="clarify.request",
                clarify=card,
            )
        put_event({
            "event": "clarify.request",
            "run_id": run_id,
            "timestamp": time.time(),
            **card,
        })
        try:
            response = _clarify_mod.wait_for_response(
                clarify_id, _clarify_mod.get_clarify_timeout()
            )
        finally:
            # The registry entry is reaped here even on cancellation:
            # wait_for_response always removes its own indices, and the
            # registration must not outlive the entry it names. The
            # settle (waiting_for_clarify -> running) runs inside the
            # gate so a concurrent /stop can never slip between the
            # check and the write: a run already moved to stopping or a
            # terminal status is never resurrected to running.
            with self._run_clarify_lock:
                self._run_clarify_registrations.pop(clarify_id, None)
                if (
                    self._run_statuses.get(run_id, {}).get("status")
                    == "waiting_for_clarify"
                ):
                    self._set_run_status(
                        run_id, "running", last_event="clarify.responded"
                    )
            put_event({
                "event": "clarify.responded",
                "run_id": run_id,
                "timestamp": time.time(),
            })
        if not response:
            return TIMEOUT_RESPONSE
        return response

    return _clarify_callback


def _cancel_run_clarifies(self, run_id: str) -> None:
    """Release every pending clarify this run registered.

    Used when a run ends or is stopped while its worker is still parked
    in a clarify wait: the empty-string sentinel unblocks the thread
    (which then reports "user did not respond"), and the registration
    map never keeps an entry whose run is gone. Precise to the run — a
    concurrent run sharing the same session id keeps its own pending
    question. Callable inside the stop gate's section: the lock is
    reentrant, and holding the gate across the pops means no admission
    for this run can interleave between the marker and the cancel."""
    with self._run_clarify_lock:
        doomed = [
            clarify_id
            for clarify_id, meta in self._run_clarify_registrations.items()
            if meta.get("run_id") == run_id
        ]
        for clarify_id in doomed:
            self._run_clarify_registrations.pop(clarify_id, None)
    if not doomed:
        return
    from tools import clarify_gateway as _clarify_mod

    for clarify_id in doomed:
        try:
            _clarify_mod.resolve_gateway_clarify(clarify_id, "")
        except Exception:
            logger.debug(
                "[api_server] clarify cancellation failed for run %s",
                run_id,
                exc_info=True,
            )


def _session_clarify_cards(
    self, session_id: str, profile: str
) -> List[Dict[str, Any]]:
    """Every pending clarify card for one exact session+profile,
    oldest first.

    Only entries an API run of this session registered (and whose
    registration profile matches) are visible; anything else pending in
    the process-wide registry under the same key — a native-gateway
    prompt, another profile's run — reads as no card at all. More than
    one card means more than one run of this session is parked on a
    question: the callers fail closed on that instead of exposing or
    resolving a guessed card."""
    from tools import clarify_gateway as _clarify_mod

    with self._run_clarify_lock:
        registrations = dict(self._run_clarify_registrations)
    cards: List[Dict[str, Any]] = []
    for entry in _clarify_mod.pending_entries_for_session(session_id):
        meta = registrations.get(entry.clarify_id)
        if (
            meta is not None
            and meta.get("session_id") == session_id
            and meta.get("profile") == (profile or "default")
        ):
            cards.append(
                _bounded_clarify_card(
                    entry.clarify_id,
                    entry.question,
                    entry.choices,
                    entry.multi_select,
                )
            )
    return cards


def _sessions_waiting(self, profile: str) -> List[Dict[str, str]]:
    """``[{session_id, kind}, ...]`` this profile is explicitly waiting on.

    Two disjoint owners, one fail-closed rule each:

    * API-run clarifies — the same registrations ``_session_clarify_cards``
      reads, joined against the registry's *actual* pending entries: a
      registration whose entry has resolved (or never landed) reports
      nothing, so waiting evidence vanishes the moment the wait settles
      even when the waiter's reap lags. Session/profile come from the
      registration map, exactly as the per-session clarify routes do.
    * Native gateway waits — clarify prompts and restart confirmations
      registered by the gateway itself, whose entries carry
      registration-time owner metadata (canonical ``{profile,
      session_id}`` captured from the owning turn). Entries without that
      metadata are invisible here: their identity is never guessed from
      the routing ``session_key``.

    The answer names sessions only — never a question, choices,
    response, clarify_id, or run_id — and confers no ability to answer;
    the per-session clarify routes stay the only answer path.
    """
    from tools import clarify_gateway as _clarify_mod

    profile_key = (profile or "default").strip() or "default"
    with self._run_clarify_lock:
        registrations = dict(self._run_clarify_registrations)
    waiting: List[Dict[str, str]] = []
    seen: set = set()
    for clarify_id, meta in registrations.items():
        if (meta.get("profile") or "default") != profile_key:
            continue
        # Pending means registration AND still-unresolved event — not
        # mere map membership.
        if _clarify_mod.get_pending_entry(clarify_id) is None:
            continue
        session_id = str(meta.get("session_id") or "").strip()
        if not session_id:
            continue
        if session_id not in seen:
            seen.add(session_id)
            waiting.append({"session_id": session_id, "kind": "clarify"})
    for wait in _clarify_mod.pending_waits_for_profile(profile_key):
        if wait.get("session_id") not in seen:
            seen.add(wait.get("session_id"))
            waiting.append(wait)
    return waiting


async def _handle_sessions_waiting(
    self,
    request: "web.Request",
    *,
    _api_server,
) -> "web.Response":
    """GET /api/sessions/waiting — batch waiting-on-the-human status.

    One narrow, authenticated, profile-scoped, read-only endpoint for
    inbox surfaces (Mission Control's Your turn): instead of one
    per-session clarify probe per row, a single bounded request names
    every session of THIS profile that is parked on an explicit wait
    (API-run clarify, native clarify, native restart confirmation). The
    response carries only canonical session identity and explicit
    pending presence — no prompt content and no answer capability, which
    stay exclusive to the existing per-session clarify GET/POST.
    """
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    profile = _request_profile_name(_api_server)
    return web.json_response(
        {
            "object": "hermes.sessions.waiting",
            "waiting": self._sessions_waiting(profile),
        }
    )


def _room_permission_for(request: "web.Request") -> str:
    if request.path.endswith("/stop"):
        return "stop"
    if request.path.endswith("/approval"):
        return "approve"
    return "status" if request.method == "GET" else "dispatch"


def _run_idempotency_scope(
    self,
    request: "web.Request",
    *,
    _api_server,
) -> str:
    """Opaque auth/profile namespace; never persist bearer credentials."""
    if self._room_grant_token(request):
        claims = self._room_grant_claims(request, permission=_room_permission_for(request))
        _remember_room_retention(request, claims)
        parts = (claims[k] for k in (
            "room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
            "member_id", "target_install_id", "target_profile"))
    else:
        parts = (_api_server._api_request_profile.get() or "default",
                 self._expected_api_key() or "unauthenticated-test-listener")
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def _check_run_auth(self, request: "web.Request", *, permission: str, _api_server) -> "web.Response | None":
    if not self._room_grant_token(request):
        return self._check_auth(request)
    try:
        self._room_grant_claims(request, permission=permission)
    except Exception as exc:
        return _room_grant_error_response(exc, _openai_error=_api_server._openai_error)
    return None


def _owner_alive(owner_pid: int, owner_started: int) -> bool:
    """True when the recorded owner pid still exists and is the same process incarnation."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
        return owner_pid > 0 and bool(_pid_exists(owner_pid)) and (
            not owner_started or int(get_process_start_time(owner_pid) or 0) == owner_started)
    except Exception:
        return False


def _durable_run_status(self, request: "web.Request", run_id: str) -> Dict[str, Any] | None:
    """Hydrate a scoped run status and fail stale owners closed."""
    status = self._run_statuses.get(run_id)
    if status is not None:
        if run_id in self._run_idempotency_ids:
            scope = self._run_idempotency_scope(request)
            self._run_idempotency_store.extend_retention(scope, run_id, _room_retention_until(request))
        return status
    scope = self._run_idempotency_scope(request)
    record = self._run_idempotency_store.status_for_run(
        scope, run_id, retention_until=_room_retention_until(request))
    if record is None:
        return None
    status = dict(record["status"])
    if status.get("status") not in TERMINAL_STATUSES and not _owner_alive(
        int(record.get("owner_pid") or 0), int(record.get("owner_started") or 0)):
        status.update(
            status="interrupted", error="The gateway restarted before this run settled.",
            last_event="run.interrupted", updated_at=time.time())
        self._run_idempotency_store.update_status(run_id, status)
    self._run_statuses[run_id] = status
    self._run_idempotency_ids.add(run_id)
    self._run_owners[run_id] = scope
    return status


def _resolve_conversation_history(
    self, body: dict, raw_input: Any, *, _openai_error
) -> "tuple[List[Dict[str, str]], Any, Any, web.Response | None]":
    """Return ``(history, instructions, stored_session_id, error)``; precedence:
    ``conversation_history`` > ``previous_response_id`` chain > all-but-last ``input`` messages."""
    instructions = body.get("instructions")
    previous_response_id = body.get("previous_response_id")
    conversation_history: List[Dict[str, str]] = []
    raw_history = body.get("conversation_history")
    if raw_history:
        if not isinstance(raw_history, list):
            return [], instructions, None, _json_error(
                _openai_error, "'conversation_history' must be an array of message objects", status=400)
        for i, entry in enumerate(raw_history):
            if not isinstance(entry, dict) or {"role", "content"} - set(entry):
                return [], instructions, None, _json_error(
                    _openai_error, f"conversation_history[{i}] must have 'role' and 'content' fields",
                    status=400)
            conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
        if previous_response_id:
            logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")
    stored_session_id = None
    if not conversation_history and previous_response_id:
        stored = self._response_store.get(previous_response_id)
        if stored:
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            if instructions is None:
                instructions = stored.get("instructions")
    if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
        for msg in raw_input[:-1]:
            if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                content = msg["content"]
                if isinstance(content, list):  # flatten multi-part content blocks to text
                    content = " ".join(p.get("text", "") for p in content
                                       if isinstance(p, dict) and p.get("type") == "text")
                conversation_history.append({"role": msg["role"], "content": str(content)})
    return conversation_history, instructions, stored_session_id, None


def _accepted_response(run_id: str, status: str, gateway_session_key, *, replayed: bool,
                       session_id: str = "") -> "web.Response":
    """202 admission response; replays are flagged via ``Idempotency-Replayed``."""
    headers = {"Idempotency-Replayed": "true"} if replayed else {}
    if gateway_session_key:
        headers["X-Hermes-Session-Key"] = gateway_session_key
    return web.json_response(
        {
            "run_id": run_id,
            "status": status,
            # Additive field (existing callers ignore unknown keys): the
            # canonical session id this run owns — the same id the agent
            # persists. Lets clients navigate/poll the exact session
            # immediately instead of correlating rows, and rides the replay
            # too: a client that lost the original 202 must be able to
            # navigate/poll the exact session deterministically.
            "session_id": session_id,
            "replayed": replayed,
        },
        status=202,
        headers=headers,
    )


def _replay_or_conflict(self, request, outcome, record, gateway_session_key, _openai_error) -> "web.Response":
    """409 for a fingerprint conflict, else a 202 replay of the already-admitted run."""
    if outcome == "conflict":
        return _json_error(
            _openai_error, "Idempotency-Key was already used with a different request payload",
            code="idempotency_key_conflict", status=409)
    original_id = str(record["run_id"])
    status = self._durable_run_status(request, original_id) or record["status"]
    return _accepted_response(
        original_id, status.get("status", "queued"), gateway_session_key, replayed=True,
        session_id=status.get("session_id") or "")


@dataclass(slots=True)
class _RunLaunch:
    """State for an admitted run's background task; contextvars are captured here
    because the task outlives the request (and its middleware profile scope)."""

    owner: Any
    run_id: str
    queue: "asyncio.Queue[Optional[Dict]]"
    session_id: str
    gateway_session_key: Optional[str]
    declared_selected: bool
    user_message: str
    conversation_history: List[Dict[str, str]]
    # #98619: only continuation paths that reload session history may grant wake authority —
    # a previous_response_id continuation consumes its ResponseStore snapshot instead, and a
    # caller-supplied conversation_history is authoritative for the turn; neither consumes a
    # SessionDB delivery row, so both stay default-denied.
    session_history_delivery: bool
    agent_kwargs: dict  # ``_create_agent`` keyword arguments (prompt, model overrides, route, room policy)
    request_profile: Any
    browser_control_principal: Any
    browser_control_transport_family: Any

    @property
    def approval_session_key(self) -> str:
        # Isolated per run: session ids are conversation scopes, not authorization namespaces.
        return self.run_id

    def put_event(self, event: Optional[Dict]) -> None:
        """Enqueue only while this run still owns live transport state."""
        if self.owner._run_streams.get(self.run_id) is self.queue:
            self.queue.put_nowait(event)


def _forget_run(self, run_id: str, *tables) -> None:
    """Drop *run_id* from the given run-keyed dicts/sets, then release its owner stamp."""
    for table in tables:
        (table.discard if isinstance(table, set) else lambda k: table.pop(k, None))(run_id)
    self._release_run_owner_if_forgotten(run_id)


def _retire_live_run(self, run_id: str) -> None:
    """Retire agent/task/approval control state once the executor-backed task is done."""
    _forget_run(self, run_id, self._active_run_agents, self._active_run_tasks, self._run_approval_sessions,
                self._stopping_run_ids)


def _drop_run_transport(self, run_id: str) -> None:
    _forget_run(self, run_id, self._run_streams, self._run_streams_created)


async def _resolve_live_session_id(self, session_id: str) -> str:
    """Adopt the live compression-continuation tip for a client-addressed session (#98619):
    a /v1/runs run bound to a pre-rotation id would otherwise load a stale history slice and
    write its turn into the closed parent (CompressionSessionClosedError). Same canonical
    resolution ``/api/sessions/{id}/messages`` reads use; fails open to the original id."""
    db = await self._ensure_session_db_async()
    resolver = getattr(db, "resolve_resume_session_id", None) if db is not None else None
    if not callable(resolver):
        return session_id
    try:
        resolved = await asyncio.to_thread(resolver, session_id)
        return str(resolved) if resolved else session_id
    except Exception:
        logger.debug("/v1/runs live-session resolve failed for %s", session_id, exc_info=True)
        return session_id


async def _handle_runs(self, request: "web.Request", *, _api_server) -> "web.Response":
    """POST /v1/runs — start an agent run, return run_id immediately."""
    _openai_error = _api_server._openai_error
    # Long-term memory scope header (see chat_completions for details).
    gateway_session_key, key_err = self._parse_session_key_header(request)
    if key_err is not None:
        return key_err
    try:
        body = await request.json()
    except Exception:
        return _json_error(_openai_error, "Invalid JSON", status=400)
    body, room_error = await self._normalize_room_dispatch(request, body)
    if room_error is not None:
        return room_error
    room_dispatch, room_execution_policy = (
        v if isinstance(v, dict) else None for v in (
            (body.get("hosted_room_dispatch"), body.get("_room_execution_policy"))
            if isinstance(body, dict) else (None, None)))
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if len(idempotency_key) > 255 or any(ord(ch) < 33 or ord(ch) > 126 for ch in idempotency_key):
        return _json_error(
            _openai_error, "Idempotency-Key must be 1-255 visible ASCII characters",
            code="invalid_idempotency_key", status=400)
    idempotency_scope = idempotency_fingerprint = ""
    if idempotency_key:
        idempotency_scope = self._run_idempotency_scope(request)
        idempotency_fingerprint = hashlib.sha256(json.dumps(
            {"body": body, "gateway_session_key": gateway_session_key or ""},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
    raw_input = body.get("input")
    if not raw_input:
        return _json_error(_openai_error, "Missing 'input' field", status=400)
    if isinstance(raw_input, str):
        user_message = raw_input
    else:
        user_message = raw_input[-1].get("content", "") if isinstance(raw_input, list) else ""
    if not user_message:
        return _json_error(_openai_error, "No user message found in input", status=400)
    conversation_history, instructions, stored_session_id, history_err = (
        _resolve_conversation_history(self, body, raw_input, _openai_error=_openai_error))
    if history_err is not None:
        return history_err
    previous_response_id = body.get("previous_response_id")
    session_id = body.get("session_id") or stored_session_id
    route = self._resolve_route(body.get("model"))
    agent_overrides = _api_server._request_agent_overrides(body, virtual_model=self._model_name)
    selection_error = self._request_route_conflict_error(
        session_id=session_id, gateway_session_key=gateway_session_key,
        requested_model=agent_overrides.get("requested_model"),
        requested_provider=agent_overrides.get("requested_provider"), route=route)
    if selection_error:
        return _json_error(_openai_error, selection_error, status=400)
    # A lost-acceptance replay must resolve even while the original run holds the last
    # concurrency slot; this read reserves nothing (the atomic reserve below closes the race).
    if idempotency_key:
        outcome, record = self._run_idempotency_store.lookup(
            idempotency_scope, idempotency_key, idempotency_fingerprint,
            retention_until=_room_retention_until(request))
        if outcome == "conflict" or (outcome == "reused" and record is not None):
            return _replay_or_conflict(self, request, outcome, record, gateway_session_key, _openai_error)
    # Enforce concurrency only for a genuinely new run.
    limited = self._concurrency_limited_response()
    if limited is not None:
        return limited
    run_id = f"run_{uuid.uuid4().hex}"
    self._run_owners[run_id] = self._run_idempotency_scope(request)
    # Same precedence as /v1/responses: body session_id > response chain > X-Hermes-Session-Key
    # conversation > run_id (which would otherwise re-key every affinity surface per run).
    # An explicit or chained session owns its routing key and is never rebound to the header.
    _declared_selected = not session_id and bool(gateway_session_key)
    selected_session_id = session_id or (
        self._declared_conversation_session(gateway_session_key) if _declared_selected else None)
    # A client-addressed id from before a compression rotation must adopt the live tip (#98619):
    # history loads from it, the turn writes to it, and a detached delivery row persisted to it
    # is what the next same-id run consumes below.
    if selected_session_id:
        selected_session_id = await _resolve_live_session_id(self, str(selected_session_id))
    session_id = selected_session_id or run_id
    # History loads for the session the request actually selected — including one resolved from
    # a declared X-Hermes-Session-Key, whose persisted delivery rows must reach the next
    # same-key run's context (#98619).  previous_response_id continuations keep their
    # ResponseStore snapshot as history (they cannot consume a SessionDB delivery row and are
    # accordingly denied wake capability in _run_agent_sync); the fresh run_id fallback has
    # nothing persisted to load yet.  Wake authority is fixed here, before the load can
    # overwrite ``conversation_history``: a caller-supplied history is authoritative for this
    # turn, never consumes the SessionDB delivery row, and is denied on the same contract.
    session_history_delivery = not previous_response_id and not conversation_history
    if not conversation_history and selected_session_id and not previous_response_id:
        conversation_history = await self._conversation_history_for_session(str(selected_session_id))
    q = self._run_streams[run_id] = asyncio.Queue()
    created_at = self._run_streams_created[run_id] = time.time()
    self._run_approval_sessions[run_id] = run_id  # approval session key (see _RunLaunch)
    initial_status = self._set_run_status(
        run_id, "queued", created_at=created_at, session_id=session_id, model=body.get("model", self._model_name))
    if idempotency_key:
        outcome, record = self._run_idempotency_store.reserve(
            idempotency_scope, idempotency_key, idempotency_fingerprint, run_id, initial_status,
            owner_pid=self._run_owner_pid, owner_started=self._run_owner_started,
            retention_until=_room_retention_until(request))
        if outcome != "created":
            _forget_run(
                self, run_id, self._run_streams, self._run_streams_created, self._run_approval_sessions,
                self._run_statuses, self._run_owners)
            return _replay_or_conflict(self, request, outcome, record, gateway_session_key, _openai_error)
        self._run_idempotency_ids.add(run_id)
    launch = _RunLaunch(
        self, run_id, q, session_id, gateway_session_key, _declared_selected, user_message,
        conversation_history, session_history_delivery,
        agent_kwargs=dict(
            ephemeral_system_prompt=instructions, session_id=session_id, gateway_session_key=gateway_session_key,
            route=route, room_dispatch=room_dispatch, room_execution_policy=room_execution_policy,
            **{k: agent_overrides.get(k) for k in ("requested_model", "requested_provider", "model_options")}),
        request_profile=_api_server._api_request_profile.get(),
        browser_control_principal=_api_server._api_request_browser_control_principal.get(),
        browser_control_transport_family=_api_server._api_request_browser_control_transport_family.get())
    self._activate_admitted_request()
    task = self._active_run_tasks[run_id] = asyncio.create_task(_execute_run(self, launch, _api_server=_api_server))
    with suppress(TypeError):
        self._background_tasks.add(task)  # tracked for shutdown drain
    if hasattr(task, "add_done_callback"):
        task.add_done_callback(self._background_tasks.discard)
    return _accepted_response(
        run_id, "started", gateway_session_key, replayed=False, session_id=session_id)


def _run_agent_sync(self, run: _RunLaunch, agent, approval_notify, *, _api_server):
    """Executor-thread body of one run; returns ``(result, usage)``."""
    from gateway.session_context import clear_session_vars
    from gateway.hosted_room_execution_policy import (
        RoomExecutionPolicy, bind_room_execution_policy, reset_room_execution_policy)
    # No eager slash-worker pre-warm: slash.exec spawns one on demand (its error path already relies on that
    # respawn to recover from a dead worker). Each worker child runs its own MCP discovery (#61891), so
    # pre-warming one per session forks the full stdio MCP fleet — ~20 OS processes per retained session on
    # a config with a few stdio servers — even for sessions that never run a worker-routed command. Sessions
    # held by a live transport are never reaped, so with the desktop app open for days those fleets
    # accumulate until the OS refuses new process spawns.
    from tools.approval import register_gateway_notify, unregister_gateway_notify
    from tools.approval_context import reset_current_session_key, set_current_session_key
    session_id = run.session_id
    effective_task_id = session_id or run.run_id
    # (token, reset) pairs unwound in the finally block; bound only once each step succeeds.
    resets: list[tuple[Any, Callable]] = []
    with self._profile_scope(run.request_profile):
        try:
            # Contextvars, not process env: concurrent runs must not share identity.
            resets.append((set_current_session_key(run.approval_session_key), reset_current_session_key))
            # chat_id carries the raw session id like _run_agent() does; without it
            # tools.async_delegation sees no HERMES_SESSION_CHAT_ID and forces delegations sync.
            session_tokens = self._bind_api_server_session(
                chat_id=session_id or "", session_key=run.approval_session_key, session_id=session_id or "",
                browser_control_principal=run.browser_control_principal,
                browser_control_transport_family=run.browser_control_transport_family,
                # #98619 audited opt-in: the /v1/runs session id is wake-capable only when its
                # own continuation path reloads session history — an explicit body/chained
                # session id or a declared X-Hermes-Session-Key conversation (both load
                # SessionDB in _handle_runs), or the run_id fallback the client can post back
                # as body.session_id.  A previous_response_id continuation consumes its
                # ResponseStore snapshot instead and can never see a SessionDB delivery row,
                # so it stays default-denied until a merge contract exists for that chain;
                # likewise a caller-supplied conversation_history is authoritative for the
                # turn and never reads the delivery row, so it is denied the same way.
                session_history_delivery="1" if run.session_history_delivery else "")
            if session_tokens:
                resets.append((session_tokens, clear_session_vars))
            if run.agent_kwargs["room_dispatch"] is not None:
                policy = RoomExecutionPolicy.from_mapping(run.agent_kwargs["room_execution_policy"] or {})
                resets.append((bind_room_execution_policy(policy), reset_room_execution_policy))
            register_gateway_notify(run.approval_session_key, approval_notify)
            # /v1/runs owns its agent lifecycle (no TurnRunner): record process ownership
            # so stop/cancel reaps only the background processes this run created.
            _api_server._publish_turn_process_ownership(agent, effective_task_id)
            r = agent.run_conversation(
                user_message=run.user_message, conversation_history=run.conversation_history,
                task_id=effective_task_id)
        finally:
            # Clear ownership now so a later stop can't reap work this run left running.
            _api_server._clear_turn_process_ownership(agent)
            # Declared-conversation binding, same precedence gate as _run_agent.
            if run.declared_selected:
                self._bind_declared_conversation(
                    getattr(agent, "session_id", None) or session_id, run.gateway_session_key)
            try:
                unregister_gateway_notify(run.approval_session_key)
            finally:
                for token, reset in resets:
                    with suppress(Exception):
                        reset(token)
        return r, {key: getattr(agent, attr, 0) or 0 for key, attr in _USAGE_FIELDS}


def _make_approval_notify(self, run: _RunLaunch, *, _api_server) -> Callable[[Dict[str, Any]], None]:
    """Approval-request bridge: redact, stamp the event envelope, park the run status, enqueue."""
    run_id, q, loop = run.run_id, run.queue, asyncio.get_running_loop()

    def _approval_notify(approval_data: Dict[str, Any]) -> None:
        event = dict(approval_data or {})
        # Clients must never receive the raw flagged command: redact before it hits the stream.
        # Redact credentials from the command before it enters the SSE/API event stream — same egress bug as
        # #48456, second transport: API/desktop clients would otherwise receive the raw command Tirith
        # flagged. Reuse the gateway seam.
        if "command" in event:
            from gateway.run import _redact_approval_command
            event["command"] = _redact_approval_command(event.get("command"))
        event.update(_run_event(run_id, "approval.request", choices=_api_server._approval_event_choices(
            smart_denied=bool(event.get("smart_denied")),
            allow_session=event.get("allow_session") is not False,
            allow_permanent=event.get("allow_permanent") is not False)))
        self._set_run_status(run_id, "waiting_for_approval", last_event="approval.request", approval=event)
        with suppress(Exception):
            loop.call_soon_threadsafe(q.put_nowait, event)

    return _approval_notify


async def _execute_run(self, run: _RunLaunch, *, _api_server) -> None:
    """Drive one admitted run, publish its terminal event/status, release live state."""
    _redact_api_error_text = _api_server._redact_api_error_text
    run_id, loop = run.run_id, asyncio.get_running_loop()

    def _text_cb(delta: Optional[str]) -> None:
        if delta is None or run_id not in self._run_streams:
            return
        with suppress(Exception):
            loop.call_soon_threadsafe(run.put_event, _run_event(run_id, "message.delta", delta=delta))

    def _finish(status: str, extra: Optional[dict] = None, **fields: Any) -> None:
        """Terminal status, then best-effort ``run.<status>`` event; key order is wire shape."""
        extra = extra or {}
        self._set_run_status(run_id, status, **fields, last_event=f"run.{status}", **extra)
        with suppress(Exception):
            run.put_event(_run_event(run_id, f"run.{status}", **fields, **extra))

    try:
        self._set_run_status(run_id, "running")
        if run_id in self._stopping_run_ids:
            _finish("cancelled")
            return
        with self._profile_scope(run.request_profile):
            agent = self._create_agent(
                stream_delta_callback=_text_cb, tool_progress_callback=self._make_run_event_callback(run_id, loop),
                **run.agent_kwargs)
        self._active_run_agents[run_id] = agent

        def _put_event_from_worker(event: Optional[Dict]) -> None:
            # The clarify callback runs on the executor thread; the queue
            # belongs to the event loop (same bridge _approval_notify uses).
            with suppress(Exception):
                loop.call_soon_threadsafe(run.put_event, event)

        # Same wiring the native gateway gives its agents (gateway/run.py sets
        # agent.clarify_callback after construction): the ``clarify`` tool reads
        # it off the agent instance. Without it a run that asks a question would
        # auto-answer via the headless default instead of pausing for the
        # session's clarify card. Keyed to the canonical session id, so the
        # session clarify routes can serve and resolve it.
        agent.clarify_callback = self._make_run_clarify_callback(
            run_id, run.session_id, run.request_profile or "default", _put_event_from_worker)
        approval_notify = _make_approval_notify(self, run, _api_server=_api_server)
        result, usage = await loop.run_in_executor(
            None, lambda: _run_agent_sync(self, run, agent, approval_notify, _api_server=_api_server))
        if not isinstance(result, dict):
            result = {}
        if run_id in self._stopping_run_ids and result.get("interrupted") is True:
            _finish("cancelled")
        elif result.get("failed"):
            # Non-retryable client errors (401/400) return failed=True rather than raising.
            _finish("failed", error=_redact_api_error_text(result.get("error") or "agent run failed"))
        else:
            # Undelivered steer text rides on the terminal event/status for client replay.
            extra = {"pending_steer": result["pending_steer"]} if result.get("pending_steer") else {}
            _finish("completed", extra, output=result.get("final_response", ""), usage=usage)
    except asyncio.CancelledError:
        _finish("cancelled")
        raise
    except _api_server._ProviderAuthResolutionError as exc:
        # Same controlled provider-auth message the _run_agent() endpoints give.
        logger.warning("Provider authentication failed for run=%s: %s", run_id, exc)
        _finish("failed", error=f"⚠️ Provider authentication failed: {exc}")
    except Exception as exc:
        logger.exception("[api_server] run %s failed", run_id)
        _finish("failed", error=_redact_api_error_text(exc))
    finally:
        # On cancellation (/stop) the executor thread may still block on an approval
        # Event; unregistering releases it. Idempotent on normal completion.
        _unregister_approval_notify(run.approval_session_key)
        # Same release for a worker still parked on a clarify wait: no pending
        # entry or registration outlives the run.
        with suppress(Exception):
            self._cancel_run_clarifies(run_id)
        with suppress(Exception):
            run.put_event(None)  # sentinel: close the SSE stream
        _retire_live_run(self, run_id)


def _unregister_approval_notify(approval_session_key: Optional[str]) -> None:
    """Best-effort release of a run's approval waiter (no-op without a key)."""
    with suppress(Exception):
        from tools.approval import unregister_gateway_notify
        if approval_session_key:
            unregister_gateway_notify(approval_session_key)


def _release_run_owner_if_forgotten(self, run_id: str) -> None:
    """Drop the owner stamp only once nothing keyed by *run_id* survives: ownership must
    outlive every surface it protects (retired on different clocks); ownerless = fail-closed."""
    live = (self._run_statuses, self._active_run_agents, self._active_run_tasks, self._run_streams,
            self._run_approval_sessions)
    if not any(run_id in table for table in live):
        self._run_owners.pop(run_id, None)


def _request_owns_run(self, request: "web.Request", run_id: str) -> bool:
    scope = self._run_idempotency_scope(request)
    owner = self._run_owners.get(run_id)
    if owner is not None:
        return owner == scope
    # No in-memory owner: only a durable record under the caller's scope admits it.
    # Under multiplex_profiles every profile holds a valid key, so ownerless = allow-all.
    # Run state that exists without an owner stamp is an unanswered authorization question, not a run anyone
    # may control — under gateway.multiplex_profiles every served profile holds a valid key, so admitting it
    # would make the boundary allow-all (#93689).
    return self._run_idempotency_store.owns_run(scope, run_id)


def _load_owned_run(self, request, *, _api_server, permission: Optional[str], active_fallback: bool):
    """Authenticate (*permission* -> room-grant aware; ``None`` -> API key only) and resolve
    ``(run_id, status, agent, task, error)``; *active_fallback* reports a live in-process run
    without pollable status as ``running`` instead of 404."""
    auth_err = self._check_run_auth(request, permission=permission) if permission else self._check_auth(request)
    if auth_err:
        return None, None, None, None, auth_err
    _openai_error = _api_server._openai_error
    run_id = request.match_info["run_id"]
    if not self._request_owns_run(request, run_id):
        return run_id, None, None, None, _run_not_found(_openai_error, run_id)
    agent = self._active_run_agents.get(run_id)
    task = self._active_run_tasks.get(run_id)
    status = self._durable_run_status(request, run_id)
    if status is None and active_fallback and (agent is not None or task is not None):
        status = self._set_run_status(run_id, "running")
    if status is None:
        return run_id, None, agent, task, _run_not_found(_openai_error, run_id)
    return run_id, status, agent, task, None


async def _handle_get_run(self, request: "web.Request", *, _api_server) -> "web.Response":
    """GET /v1/runs/{run_id} — return pollable run status for external UIs."""
    _, status, _, _, err = _load_owned_run(
        self, request, _api_server=_api_server, permission="status", active_fallback=True)
    return err or web.json_response(status)


async def _handle_run_events(self, request: "web.Request", *, _api_server) -> "web.StreamResponse":
    """GET /v1/runs/{run_id}/events — stream structured agent lifecycle events."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    run_id = request.match_info["run_id"]
    if not self._request_owns_run(request, run_id):
        return _run_not_found(_api_server._openai_error, run_id)
    # Allow subscribing slightly before the run is registered (race window).
    # Confirm the force-kill actually reaped the process before we clear its PID file / scoped locks.
    # SIGKILL can fail to take (e.g. an uninterruptible-sleep or zombie-reaping parent), and if we blindly
    # clear the metadata and start a fresh instance we end up with two live gateways fighting over the same
    # token — the duplicate-gateway failure in #19471.
    for _ in range(20):
        if run_id in self._run_streams:
            break
        await asyncio.sleep(0.05)
    else:
        return _run_not_found(_api_server._openai_error, run_id)
    q = self._run_streams[run_id]
    self._run_stream_subscribers.add(run_id)
    response = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
            if event is None:  # run finished
                await response.write(b": stream closed\n\n")
                break
            await response.write(_api_server._sse_frame(event))
    except Exception as exc:
        logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
    finally:
        self._run_stream_subscribers.discard(run_id)
        _drop_run_transport(self, run_id)
    return response


def _mark_run_event(self, run_id: str, name: str, **fields: Any) -> None:
    """Record a control-plane event on the run status and (best effort) its SSE stream."""
    self._set_run_status(run_id, "running", last_event=name)
    q = self._run_streams.get(run_id)
    if q is not None:
        with suppress(Exception):
            q.put_nowait(_run_event(run_id, name, **fields))


_APPROVAL_CHOICE_ALIASES = {"approve": "once", "approved": "once", "allow": "once"}


async def _handle_run_approval(self, request: "web.Request", *, _api_server) -> "web.Response":
    """POST /v1/runs/{run_id}/approval — resolve a pending run approval."""
    _openai_error = _api_server._openai_error
    run_id, _, _, _, err = _load_owned_run(
        self, request, _api_server=_api_server, permission="approve", active_fallback=False)
    if err is not None:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json_error(_openai_error, "Invalid JSON", status=400)
    raw_choice = str(body.get("choice", "")).strip().lower()
    choice = _APPROVAL_CHOICE_ALIASES.get(raw_choice, raw_choice)
    room_scoped = bool(self._room_grant_token(request))
    raw_request_id = body.get("request_id")
    request_id = raw_request_id.strip() if isinstance(raw_request_id, str) else ""
    # Room grants may resolve exactly one request and never widen to session/always.
    allowed = {"once", "deny"} if room_scoped else {"once", "session", "always", "deny"}
    resolve_all = any(_api_server._coerce_request_bool(body.get(k), default=False) for k in ("all", "resolve_all"))
    approval_session_key = self._run_approval_sessions.get(run_id)
    for failed, message, code, status in (
        (raw_request_id is not None and (not request_id or len(request_id) > 256),
         "Approval request_id is invalid.", "invalid_approval_request", 400),
        (choice not in allowed,
         "Invalid approval choice; expected one of: " + ", ".join(sorted(allowed)),
         "invalid_approval_choice", 400),
        (room_scoped and resolve_all,
         "Room approvals can resolve only one exact request", "invalid_approval_scope", 400),
        (room_scoped and not request_id,
         "Room approvals require the exact request_id.", "approval_request_required", 400),
        (not approval_session_key,
         f"Run has no active approval session: {run_id}", "approval_not_active", 409)):
        if failed:
            return _json_error(_openai_error, message, code=code, status=status)
    try:
        from tools.approval import resolve_gateway_approval
        resolved = resolve_gateway_approval(
            approval_session_key, choice, resolve_all=resolve_all, request_id=request_id or None)
    except Exception as exc:
        logger.exception("[api_server] approval resolution failed for run %s", run_id)
        return _json_error(_openai_error, str(exc), status=500)
    if resolved <= 0:
        return _json_error(
            _openai_error, f"Run has no pending approval: {run_id}", code="approval_not_pending", status=409)
    request_id_field = {"request_id": request_id} if request_id else {}
    _mark_run_event(self, run_id, "approval.responded", choice=choice, **request_id_field, resolved=resolved)
    return web.json_response({
        "object": "hermes.run.approval_response", "run_id": run_id, "choice": choice, **request_id_field,
        "resolved": resolved})


async def _handle_steer_run(self, request: "web.Request", *, _api_server) -> "web.Response":
    """POST /v1/runs/{run_id}/steer — inject guidance into a running agent."""
    _openai_error = _api_server._openai_error
    run_id, status, agent, _, err = _load_owned_run(
        self, request, _api_server=_api_server, permission=None, active_fallback=False)
    if err is not None:
        return err
    # /stop keeps agent refs during cooperative shutdown, so the status gate (not the
    # agent ref) is what rejects stop-then-steer.
    if status.get("status") != "running" or not hasattr(agent, "steer"):
        return _json_error(
            _openai_error, f"Run is not currently accepting steer input: {run_id}",
            code="run_not_accepting_steer", status=409)
    body, err = await self._read_json_body(request)
    if err:
        return err
    raw_text = body.get("input") or body.get("message") or body.get("text") or ""
    steer_text = _api_server._normalize_chat_content(raw_text).strip()
    if not steer_text:
        return _json_error(
            _openai_error, "Missing non-empty steer text; expected 'input', 'message', or 'text'.",
            code="invalid_steer_input", status=400)
    try:
        accepted = bool(agent.steer(steer_text))
    except Exception as exc:
        logger.exception("[api_server] steer failed for run %s", run_id)
        return _json_error(_openai_error, _api_server._redact_api_error_text(exc), code="steer_failed", status=500)
    if not accepted:
        return _json_error(
            _openai_error, f"Run did not accept steer text: {run_id}", code="steer_not_accepted", status=409)
    _mark_run_event(self, run_id, "run.steered", accepted=True)
    return web.json_response({"object": "hermes.run.steer", "run_id": run_id, "accepted": True})


async def _handle_stop_run(self, request: "web.Request", *, _api_server) -> "web.Response":
    """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
    _openai_error = _api_server._openai_error
    run_id, status, agent, task, err = _load_owned_run(
        self, request, _api_server=_api_server, permission="stop", active_fallback=True)
    if err is not None:
        return err
    if status.get("status") in TERMINAL_STATUSES:
        return web.json_response(status)
    if agent is None and task is None:
        return _json_error(
            _openai_error, f"Run is not active in this gateway process: {run_id}",
            code="run_not_active", status=409)

    # Atomic stop section on the run clarify gate — the same reentrant
    # lock the clarify callback's admission+register+waiting section and
    # its settle section take. Holding it across BOTH the stopping
    # marker and the clarify cancel is what makes the cross-thread
    # stop/clarify races impossible: a clarify callback on the executor
    # thread either completes its whole registration first (this cancel
    # then releases that entry, and the settle that follows no-ops on
    # the now-stopping status) or it sees the marker below and returns
    # the canonical timeout sentinel without ever touching the registry.
    with self._run_clarify_lock:
        self._set_run_status(run_id, "stopping", last_event="run.stopping")
        self._stopping_run_ids.add(run_id)

        # Release any clarify the worker is parked on right now — the
        # run's own finally block would also do it, but not until the
        # interrupted worker unwinds. Stopping must leave no pending
        # entry behind.
        try:
            self._cancel_run_clarifies(run_id)
        except Exception:
            pass

    if agent is not None:
        with suppress(Exception):
            _api_server.request_hard_interrupt(agent, "Stop requested via API")
        # Reap only this run's background processes (epoch-gated inside, so a concurrent
        # run on the same session_id keeps its own); no-op if the run already finished.
        _api_server._reap_disconnected_agent_processes(agent, source="api_server_run_stop")
    return web.json_response({"run_id": run_id, "status": "stopping"})


def _request_profile_name(_api_server) -> str:
    """Canonical profile name for the in-flight request.

    The ``/p/<profile>/`` middleware stamps a ContextVar before the
    handler runs; unprefixed requests are the default profile.
    """
    try:
        return _api_server._api_request_profile.get() or "default"
    except Exception:  # pragma: no cover - defensive
        return "default"


async def _handle_session_clarify_get(
    self,
    request: "web.Request",
    *,
    _api_server,
) -> "web.Response":
    """GET /api/sessions/{session_id}/clarify — oldest pending question.

    Profile-scoped like every ``/api/sessions`` route: only clarifies a
    run of *this* session started under *this* profile are visible, so
    one profile's composer can neither see nor answer another's prompt.
    With more than one such run parked on a question the route fails
    closed (409 clarify_ambiguous): which card is "the" card is the
    client's ambiguity to resolve by stopping a run, never ours to
    guess, and a guessed card could answer the wrong waiter.
    """
    _openai_error = _api_server._openai_error

    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err

    session_id = request.match_info.get("session_id") or ""
    profile = _request_profile_name(_api_server)
    cards = self._session_clarify_cards(session_id, profile)
    if len(cards) > 1:
        return web.json_response(
            _openai_error(
                "Multiple pending clarifies for this session; stop or "
                "resolve the others until exactly one remains",
                code="clarify_ambiguous",
            ),
            status=409,
        )
    card = cards[0] if cards else None
    return web.json_response(
        {
            "object": "hermes.session.clarify",
            "session_id": session_id,
            "pending_clarify": card,
        }
    )


async def _handle_session_clarify_post(
    self,
    request: "web.Request",
    *,
    _api_server,
) -> "web.Response":
    """POST /api/sessions/{session_id}/clarify — answer the pending question.

    Fails closed: the ``clarify_id`` must be exactly the one the GET
    served for this session, the registration must belong to this
    session *and* profile, and the entry must still be pending. Stale,
    cross-session, cross-profile, ambiguous, and lost-race answers all
    409 without revealing whether the id ever existed.
    """
    _openai_error = _api_server._openai_error

    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err

    session_id = request.match_info.get("session_id") or ""
    profile = _request_profile_name(_api_server)

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            _openai_error("Invalid JSON", code="invalid_json"), status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            _openai_error("Request body must be a JSON object.", code="invalid_json"),
            status=400,
        )

    raw_clarify_id = body.get("clarify_id")
    if (
        not isinstance(raw_clarify_id, str)
        or not raw_clarify_id.strip()
        or len(raw_clarify_id) > _CLARIFY_ID_MAX_CHARS
    ):
        return web.json_response(
            _openai_error(
                "clarify_id must be a non-empty string.", code="invalid_clarify_id",
            ),
            status=400,
        )
    clarify_id = raw_clarify_id.strip()

    raw_response = body.get("response")
    response_items: Optional[List[str]] = None
    response_text: Optional[str] = None
    if isinstance(raw_response, list):
        if len(raw_response) > _CLARIFY_MAX_RESPONSE_ITEMS:
            return web.json_response(
                _openai_error(
                    f"response supports at most {_CLARIFY_MAX_RESPONSE_ITEMS} items.",
                    code="invalid_clarify_response",
                ),
                status=400,
            )
        for item in raw_response:
            if not isinstance(item, str):
                return web.json_response(
                    _openai_error(
                        "response items must be strings.",
                        code="invalid_clarify_response",
                    ),
                    status=400,
                )
            if len(item) > _CLARIFY_MAX_RESPONSE_CHARS:
                return web.json_response(
                    _openai_error(
                        "response items must be at most "
                        f"{_CLARIFY_MAX_RESPONSE_CHARS} characters.",
                        code="invalid_clarify_response",
                    ),
                    status=400,
                )
        items = [item.strip() for item in raw_response if item.strip()]
        if not items:
            return web.json_response(
                _openai_error(
                    "response must contain at least one non-empty item.",
                    code="invalid_clarify_response",
                ),
                status=400,
            )
        response_items = items
    elif isinstance(raw_response, str):
        text = raw_response.strip()
        if not text:
            return web.json_response(
                _openai_error(
                    "response must be a non-empty string.", code="invalid_clarify_response",
                ),
                status=400,
            )
        if len(text) > _CLARIFY_MAX_RESPONSE_CHARS:
            # Refused, never truncated: a silently shortened answer
            # could change the agent's decision.
            return web.json_response(
                _openai_error(
                    "response must be at most "
                    f"{_CLARIFY_MAX_RESPONSE_CHARS} characters.",
                    code="invalid_clarify_response",
                ),
                status=400,
            )
        response_text = text
    else:
        return web.json_response(
            _openai_error(
                "response must be a string or a list of strings.",
                code="invalid_clarify_response",
            ),
            status=400,
        )

    from tools.clarify_gateway import get_pending_entry, resolve_gateway_clarify

    # Same fail-closed rule as GET: with several pending cards for this
    # exact session+profile, an exact clarify_id still names one of
    # several live waiters and releasing the wrong one is unrecoverable
    # — the client must first reduce the session to a single pending
    # clarify (stop the extra run) before any answer is accepted.
    if len(self._session_clarify_cards(session_id, profile)) > 1:
        return web.json_response(
            _openai_error(
                "Multiple pending clarifies for this session; stop or "
                "resolve the others until exactly one remains",
                code="clarify_ambiguous",
            ),
            status=409,
        )

    with self._run_clarify_lock:
        registration = self._run_clarify_registrations.get(clarify_id)
    if (
        not isinstance(registration, dict)
        or registration.get("session_id") != session_id
        or registration.get("profile") != profile
    ):
        return web.json_response(
            _openai_error(
                "Clarify is not pending for this session.",
                code="clarify_not_pending",
            ),
            status=409,
        )
    entry = get_pending_entry(clarify_id)
    if entry is None or entry.session_key != session_id:
        return web.json_response(
            _openai_error(
                "Clarify is not pending for this session.",
                code="clarify_not_pending",
            ),
            status=409,
        )

    # A list answer only means anything for a multi-select prompt; the
    # clarify tool parses it back into items via the JSON array form.
    if response_items is not None:
        if not entry.multi_select:
            return web.json_response(
                _openai_error(
                    "This clarify accepts a single response.",
                    code="invalid_clarify_response",
                ),
                status=400,
            )
        response = json.dumps(response_items, ensure_ascii=False)
    else:
        response = response_text

    if not resolve_gateway_clarify(clarify_id, response):
        # Lost the race with a second answer or a cancel; the waiting
        # run already has its (first) response.
        return web.json_response(
            _openai_error(
                "Clarify is not pending for this session.",
                code="clarify_not_pending",
            ),
            status=409,
        )
    return web.json_response(
        {
            "object": "hermes.session.clarify_response",
            "session_id": session_id,
            "clarify_id": clarify_id,
            "resolved": True,
        }
    )


async def _sweep_orphaned_runs(self) -> None:
    """Periodically expire transport buffers and terminal status records."""
    while True:
        await asyncio.sleep(60)
        self._sweep_orphaned_runs_once(time.time())


def _sweep_orphaned_runs_once(self, now: Optional[float] = None) -> None:
    """Expire old SSE buffers without treating transport age as run age."""
    if now is None:
        now = time.time()
    for run_id, created_at in list(self._run_streams_created.items()):
        if now - created_at <= self._RUN_STREAM_TTL or run_id in self._run_stream_subscribers:
            continue
        logger.debug("[api_server] sweeping expired run transport %s", run_id)
        task = self._active_run_tasks.get(run_id)
        # Transport TTL bounds buffering; live control state survives until the task returns.
        _drop_run_transport(self, run_id)
        if task is None or task.done():
            _unregister_approval_notify(self._run_approval_sessions.get(run_id))
            # Release any clarify the run left parked — the registration map
            # never keeps an entry whose run is gone.
            with suppress(Exception):
                self._cancel_run_clarifies(run_id)
            _retire_live_run(self, run_id)
    for run_id, status in list(self._run_statuses.items()):
        if (status.get("status") in {"completed", "failed", "cancelled"}
                and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL):
            _forget_run(self, run_id, self._run_statuses, self._run_idempotency_ids)
