"""Hindsight memory plugin — MemoryProvider with knowledge graph, entity resolution
and multi-strategy retrieval; cloud (API key), local_external, or local_embedded.

Config: $HERMES_HOME/hindsight/config.json (profile-scoped), else ~/.hindsight/
config.json (legacy, shared), else env: HINDSIGHT_API_KEY / BANK_ID / BUDGET /
API_URL / MODE / TIMEOUT / IDLE_TIMEOUT / RETAIN_TAGS / RETAIN_OBSERVATION_SCOPES /
RETAIN_SOURCE / RETAIN_USER_PREFIX / RETAIN_ASSISTANT_PREFIX, and
HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT (config.json port_health_grace_timeout).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import contextvars
import inspect
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus
from agent.secret_scope import get_secret
from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from tools.registry import tool_error

from .embedded import (
    _RETRIABLE_CONNECTION_MARKERS, _build_embedded_profile_env,
    _check_local_runtime, _embedded_llm_api_key, _embedded_profile_env_path,
    _export_port_health_grace_timeout, _load_simple_env, _local_runtime_hint, _materialize_embedded_profile_env,
)
from .settings import (
    _DEFAULT_API_URL, _DEFAULT_IDLE_TIMEOUT, _DEFAULT_LOCAL_URL, _DEFAULT_RETAIN_SOURCE,
    _DEFAULT_TIMEOUT, _HINDSIGHT_GLYPH, _MIN_CLIENT_VERSION, _MIN_VERSION_FOR_UPDATE_MODE_APPEND,
    _PROVIDER_DEFAULT_MODELS, _VALID_BUDGETS, _daemon_llm_provider,
    _normalize_min_scores, _normalize_observation_scopes, _normalize_retain_tags,
    _parse_int_setting, _resolve_bank_id_template,
)

logger = logging.getLogger(__name__)

_LOCAL_MODES = {"local", "local_embedded"}
_RETAIN_CONTEXT_DEFAULT = "conversation between Hermes Agent and the User"

# How long a first-client build may take before the waiting caller gives up.
# _build_client() may lazy-install the SDK, and tools.lazy_deps's
# _venv_pip_install allows 300s for that — far beyond any single HTTP
# request. Bounding the build wait by the *request* timeout abandoned
# still-valid setups mid-install and orphaned the clients they eventually
# created, so the setup wait is kept at least at the install allowance
# (see _client_setup_timeout) and never below the request timeout.
_CLIENT_SETUP_TIMEOUT = 300  # seconds — keep >= lazy_deps._venv_pip_install
# Bank defaults pushed once per provider lifetime, best-effort, on first
# client creation (initialize() deliberately never builds a client). The
# retain mission steers extraction toward durable knowledge and away from
# transient session state; the directives below steer recall the same way.
_DEFAULT_BANK_RETAIN_MISSION = (
    "Preserve durable knowledge about infrastructure, services, projects, tools, "
    "and user preferences and decisions. Skip transient task state: PR/issue "
    "numbers, commit SHAs, in-progress work, session chatter, bot greetings, "
    "model fallback events, and availability messages. Compress verbose "
    "operational detail into concise declarative facts."
)
_DEFAULT_BANK_DIRECTIVES: tuple[Dict[str, Any], ...] = (
    {
        "name": "prefer-newer-facts",
        "priority": 10,
        "content": (
            "When multiple facts cover the same topic and conflict, prefer the "
            "most recently observed/occurred fact. Older facts about the same "
            "entity (old IPs, old ports, superseded configs) must not override "
            "newer ones."
        ),
    },
    {
        "name": "ignore-session-dumps",
        "priority": 10,
        "content": (
            "Ignore facts that describe one-off session events: bot small talk, "
            "waiting/standby states, model fallbacks during a single chat, test "
            "sessions, transient task status. They are noise, not durable "
            "knowledge, unless they record a durable decision or fix."
        ),
    },
)


def _ensure_client_dependency() -> None:
    """Lazily install the Hindsight client (``tools.lazy_deps``) before importing it."""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("memory.hindsight", prompt=False)
    except ImportError:
        pass
    except Exception as exc:
        raise ImportError(str(exc)) from exc


def _cloud_api_key(config: dict) -> str:
    return config.get("apiKey") or config.get("api_key") or get_secret("HINDSIGHT_API_KEY", "")


def _maybe_upgrade_client() -> None:
    """Auto-upgrade an outdated hindsight-client via the environment-aware lazy_deps
    installer (sealed hosted venvs redirect to the durable target)."""
    try:
        from importlib.metadata import version as pkg_version
        from packaging.version import Version
        installed = pkg_version("hindsight-client")
        if Version(installed) < Version(_MIN_CLIENT_VERSION):
            logger.warning("hindsight-client %s is outdated (need >=%s), attempting upgrade...",
                           installed, _MIN_CLIENT_VERSION)
            from tools.lazy_deps import install_specs
            outcome = install_specs([f"hindsight-client>={_MIN_CLIENT_VERSION}"], timeout=120)
            if outcome.ok:
                logger.info("hindsight-client upgraded to >=%s", _MIN_CLIENT_VERSION)
            elif outcome.blocked:
                logger.warning("Auto-upgrade unavailable: %s. Run: uv pip install 'hindsight-client>=%s'",
                               outcome.reason, _MIN_CLIENT_VERSION)
            else:
                logger.warning("Auto-upgrade failed: %s. Run: uv pip install 'hindsight-client>=%s'",
                               (outcome.stderr or "").strip() or "install error", _MIN_CLIENT_VERSION)
    except Exception:
        pass  # packaging not available or other issue — proceed anyway


# update_mode='append' capability (Hindsight >= 0.5.0), cached per API URL per
# process so every provider on the same API shares one /version round trip.
_append_capability_cache: Dict[str, bool] = {}
_append_capability_lock = threading.Lock()


def _fetch_hindsight_api_version(api_url: str, api_key: str | None = None,
                                 timeout: float = 5.0) -> str | None:
    """GET ``<api_url>/version`` -> version string, or None on any failure (= legacy API)."""
    import urllib.request
    if not api_url:
        return None
    url = api_url.rstrip("/") + "/version"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Hindsight /version probe failed for %s: %s", url, exc)
        return None
    version = (data.get("version") or data.get("api_version")) if isinstance(data, dict) else None
    return str(version) if version else None


def _check_api_supports_update_mode_append(api_url: str, api_key: str | None = None) -> bool:
    """Cached ``update_mode='append'`` check for *api_url*. False on any probe failure
    (safe default: per-process document_id, no update_mode = resume-overwrite fix intact).

    Probes once per URL per process. See #6654.
    """
    if not api_url:
        return False
    with _append_capability_lock:
        if api_url in _append_capability_cache:
            return _append_capability_cache[api_url]
    version = _fetch_hindsight_api_version(api_url, api_key)
    try:  # missing/invalid version -> unsupported
        from packaging.version import Version
        supported = bool(version) and Version(version) >= Version(_MIN_VERSION_FOR_UPDATE_MODE_APPEND)
    except Exception:
        supported = False
    with _append_capability_lock:
        # A concurrent probe may have filled the cache meanwhile; its answer wins.
        supported = _append_capability_cache.setdefault(api_url, supported)
    if supported:
        logger.debug("Hindsight API %s version %s supports update_mode='append'", api_url, version)
    else:
        logger.warning("Hindsight API at %s reports version %r, older than %s. "
                       "Falling back to per-process document_id — retains across "
                       "processes/sessions create separate documents instead of "
                       "appending to a session-scoped one. Upgrade Hindsight to "
                       "%s+ to enable update_mode='append' deduplication.",
                       api_url, version, _MIN_VERSION_FOR_UPDATE_MODE_APPEND, _MIN_VERSION_FOR_UPDATE_MODE_APPEND)
    return supported


# One long-lived event loop per process for Hindsight async calls; ephemeral
# loops would leak aiohttp sessions.
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()

# Pushed to the per-provider retain queue to wake the writer for a clean exit.
_WRITER_SENTINEL = object()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived event loop running on a background thread."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()
        _loop = loop
        started = threading.Event()

        def _run(loop=loop):
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        _loop_thread = threading.Thread(target=_run, daemon=True, name="hindsight-loop")
        _loop_thread.start()
        # Block until the loop is actually spinning. There is a window between
        # thread start and run_forever() where is_running() is still False; a
        # second thread calling _get_loop() in that window would mint a second
        # loop, orphaning the first along with every client session bound to
        # it (all later calls on the orphan block until their timeout).
        started.wait(timeout=5.0)
        return loop


def _on_hindsight_loop_thread() -> bool:
    """Return True when the calling thread IS the shared Hindsight loop thread."""
    thread = _loop_thread
    return thread is not None and threading.current_thread() is thread


def _run_sync(coro, timeout: float = _DEFAULT_TIMEOUT):
    """Schedule *coro* on the shared loop and block until done."""
    from agent.async_utils import safe_schedule_threadsafe
    future = safe_schedule_threadsafe(coro, _get_loop())
    if future is None:
        raise RuntimeError("Hindsight loop unavailable")
    return future.result(timeout=timeout)


class _ClientSetup:
    """Completion handshake for a first-client build on the owning loop.

    _build_client() can legitimately take far longer than one HTTP request
    (lazy dependency install, embedded daemon start), so the caller waiting
    for it and the coroutine producing it need an explicit agreement on who
    owns the finished client. Exactly one side does:

    * the waiting caller, when its wait finishes in time — ``claim()``;
    * the coroutine, when every caller has given up — ``offer()`` then
      reports False and the coroutine closes the client itself, on the
      owning loop, so a timed-out setup can never strand a completed
      client nobody installed or released (embedded finalization is
      disabled — the daemon/session it holds would otherwise never be
      freed).

    ``settled`` means a SAFE terminal disposition, not mere coroutine
    completion: the build failed before producing a client (``fail()``),
    or an abandoned client was actually closed (``closed()``, or
    ``note_retry_outcome(attempt, True)`` after a reconciliation close). A close
    that raised leaves ``settled`` clear with the exact client still
    tracked (``close_failed()``), so no replacement may be treated as
    installed while the displaced client may still be live — the join
    retry in _join_abandoned_client_setup() or shutdown() must close it
    first.

    Safe settlement is deliberately kept distinct from attempt progress.
    State changes notify an internal condition, so a reconciler blocked in
    ``wait_reconcilable()`` wakes the moment there is something for it to
    do — a close failure recorded for retry, or an in-flight
    reconciliation attempt it must wait on — instead of sleeping out its
    full setup budget on ``settled`` alone; a failure recorded just after
    its initial state check wakes it too. At most ONE reconciliation
    close may be in flight per generation: ``launch_retry()`` hands that
    single slot to exactly one caller, and the attempt's wrapper reports
    through ``note_retry_outcome()`` even when the launching caller's own
    bounded wait has already expired — a completed result is recorded on
    the generation, never lost with the abandoned Future. An attempt whose
    Future goes terminal WITHOUT that report is handled by a
    done-callback instead — but a terminal Future is NOT proof the close
    coroutine stopped: a Future cancelled after its coroutine started goes
    terminal immediately while the loop-side cancellation cleanup is
    still running and may still touch the client. The wrapper therefore
    records ``note_retry_started()`` BEFORE it can touch the client, and
    the done-callback frees the slot only for an attempt that provably
    never started (cancelled pre-start, loop torn down before the first
    step). An attempt that started keeps the slot until its wrapper
    records the final outcome AFTER ``_aclose_client`` and all
    cancellation/finalization complete — no later caller or shutdown can
    launch an overlapping close meanwhile. A wrapper whose
    ``note_retry_started()`` is rejected (its slot was already released
    pre-start) returns without ever touching the client, so a stale
    coroutine that nonetheless runs can never produce a duplicate close.
    If a started attempt can never report (loop torn down mid-run), the
    exact state stays tracked and in-flight rather than overlapping or
    authorizing replacement. All release paths are keyed on the attempt's
    identity, so a late callback or stale wrapper report from a dead
    attempt can never clear or overwrite a NEWER in-flight attempt.
    """

    def __init__(self) -> None:
        # Condition (not a bare Event) so waiters can block on the union
        # of "safely terminal" / "close failed, retry me" / "retry in
        # flight" without polling, and so every transition below wakes
        # them under the same lock that guards the state they re-check.
        self._cond = threading.Condition()
        self._client: Any = None
        self._abandoned = False
        # "" while the build/cleanup is still in flight; "closed" once the
        # abandoned client is safely released; "close-failed" while the
        # tracked client still needs a reconciliation close attempt.
        self._outcome = ""
        # Set once disposition is SAFE-terminal: the build failed with no
        # client to release, or an abandoned client was closed. The next
        # builder waits on this before treating a replacement as installed.
        self.settled = threading.Event()
        # The single in-flight reconciliation close (the Future returned by
        # safe_schedule_threadsafe). Recorded by launch_retry() before
        # anyone waits, cleared together with the outcome — so "in flight"
        # always means the outcome is genuinely still pending, never merely
        # unreported. Two release paths keep that invariant:
        # note_retry_outcome() when the wrapper records an outcome, and a
        # done-callback (_release_dead_retry_slot) when the Future goes
        # terminal without one. Both check the attempt identity
        # (_retry_attempt), so a stale release can never free the slot a
        # newer attempt owns.
        self._retry_future: "concurrent.futures.Future | None" = None
        self._retry_attempt: Any = None
        # True once the in-flight attempt's wrapper coroutine has provably
        # STARTED (recorded under this same condition before the first
        # await, so it is ordered against any done-callback). A terminal
        # Future alone says nothing about the coroutine: cancellation
        # makes the Future terminal immediately while the loop-side
        # cleanup is still running. The done-callback
        # (_release_dead_retry_slot) frees the slot only while this is
        # False — the attempt provably never started — and a started
        # attempt keeps the slot until note_retry_outcome() records the
        # final outcome after all cancellation/finalization completed.
        self._retry_started = False
        # Stable identity for lookup after a confirmed close clears _client.
        self._owned_client: Any = None

    def offer(self, client) -> bool:
        """Loop side: present the finished client.

        True when a caller may still claim it; False when every caller
        abandoned the wait — the offerer must then close *client* itself.
        """
        with self._cond:
            self._client = client
            return not self._abandoned

    def claim(self):
        """Caller side: take the offered client (None if there is none)."""
        with self._cond:
            client = self._client
            self._client = None
            return client

    def abandon(self) -> bool:
        """Caller side: stop waiting for the build.

        False when the client actually arrived in time (the caller should
        ``claim()`` it instead — a client still sitting in the slot is
        unclaimed by construction, because claiming is what empties it);
        True when the coroutine is the only side left to dispose of the
        outcome.
        """
        with self._cond:
            if self._client is not None:
                return False
            self._abandoned = True
            return True

    def hand_back(self, client) -> None:
        """Caller side: return a client that arrived but may not be installed.

        Used when shutdown fences a finished first-client handoff: the setup
        completed (or the caller received the offered client), but nobody is
        allowed to install or use it anymore. The client is tracked in the
        same recorded state a failed loop-side close leaves — outcome
        ``"close-failed"``, i.e. a close is owed and not yet made — so the
        generation's single-slot reconciliation protocol owns its
        exactly-once release on the owning loop, and ``settled`` stays
        clear until that close is confirmed. A generation that already
        reached a safe terminal disposition (or a None client) has nothing
        to track and is left untouched.
        """
        if client is None:
            return
        with self._cond:
            if self.settled.is_set():
                return
            self._owned_client = client
            self._client = client
            self._outcome = "close-failed"
            self._cond.notify_all()

    def fail(self) -> None:
        """Loop side: the build raised — nothing to hand over or close."""
        with self._cond:
            self.settled.set()
            self._cond.notify_all()

    def closed(self) -> None:
        """Loop side: the abandoned client was closed successfully."""
        with self._cond:
            self._client = None
            self._outcome = "closed"
            self.settled.set()
            self._cond.notify_all()

    def close_failed(self) -> None:
        """Loop side: closing the abandoned client raised.

        The exact client stays tracked (``tracked_client()``) so a later
        reconciliation — the join retry or shutdown — can retry the close,
        and ``settled`` stays clear so that reconciliation is mandatory:
        coroutine completion alone must not authorize a replacement while
        the displaced client may still be live. Waiters are woken
        immediately: a recorded close failure is something a reconciler
        must act on now, not after its full setup wait.
        """
        with self._cond:
            if self._client is not None:
                self._outcome = "close-failed"
                self._cond.notify_all()

    def tracked_client(self):
        """Caller side: the client a failed close left potentially live."""
        with self._cond:
            return self._client if self._outcome == "close-failed" else None

    def owned_client(self):
        """The client this generation was handed for close, even after settlement."""
        with self._cond:
            return self._owned_client

    def wait_reconcilable(self, timeout: float) -> str:
        """Caller side: block until this generation needs a reconciler.

        Returns one of:

        * ``"settled"``  — safe terminal disposition; nothing left to do;
        * ``"retry"``    — a close failure is recorded, the client is
          tracked, and NO reconciliation attempt is in flight: the caller
          may take the generation's single retry slot;
        * ``"in-flight"`` — another reconciler's close attempt is running:
          wait on it (``retry_future()``), never launch a duplicate;
        * ``""``         — still building/cleaning up; the caller's
          bounded wait expired, so it must fail closed.

        Sleeping on the condition rather than on ``settled`` alone is what
        makes a close failure recorded just after the initial state check
        wake the caller too — there is no lost-wakeup window between the
        check and the sleep.
        """
        with self._cond:
            self._cond.wait_for(
                lambda: (
                    self.settled.is_set()
                    or self._outcome == "close-failed"
                    or self._retry_future is not None
                ),
                timeout=timeout,
            )
            if self.settled.is_set():
                return "settled"
            if self._retry_future is not None:
                return "in-flight"
            if self._outcome == "close-failed":
                return "retry"
            return ""

    def launch_retry(self, schedule: Callable[[Any], "concurrent.futures.Future | None"]) -> bool:
        """Caller side: take the single reconciliation-close slot.

        Generates a unique attempt token and invokes *schedule(token)*
        (which must run one short close attempt on the owning loop and
        report through ``note_retry_outcome(token, ...)`` when it finishes)
        only when the slot is genuinely free: the generation is not
        settled, no attempt is in flight, and a close-failed client is
        tracked. The returned Future is recorded BEFORE the caller starts
        waiting on it, so a caller whose bounded wait later expires leaves
        the exact attempt tracked for the next reconciler. A done-callback
        on the Future releases the slot if the attempt ever goes terminal
        without its wrapper recording an outcome AND without its coroutine
        having provably started (cancelled pre-start, loop torn down
        before the first step) — the generation then returns to the
        retryable fail-closed state with the exact client still tracked
        instead of being wedged "in-flight" by a dead Future. A STARTED
        attempt keeps the slot through its Future's terminal state: the
        wrapper's note_retry_outcome() after cancellation/finalization is
        the only release, so no overlapping close can be scheduled while
        the cancelled attempt's cleanup may still touch the client.
        Returns False — scheduling nothing — in every refused
        case, so two reconcilers can never issue concurrent closes for one
        client.
        """
        with self._cond:
            if (
                self._retry_future is not None
                or self.settled.is_set()
                or self._outcome != "close-failed"
                or self._client is None
            ):
                return False
            attempt = object()
            future = schedule(attempt)
            if future is None:
                return False
            self._retry_attempt = attempt
            self._retry_future = future
            self._retry_started = False
            # Attached AFTER the attempt is recorded: if the Future is
            # already terminal (cancelled inside schedule()), the callback
            # fires synchronously here — re-acquiring this Condition's
            # RLock from the same thread — and frees the slot at once.
            future.add_done_callback(self._release_dead_retry_slot)
            self._cond.notify_all()
            return True

    def retry_future(self):
        """Caller side: the in-flight reconciliation attempt, if any."""
        with self._cond:
            return self._retry_future

    def note_retry_started(self, attempt: Any) -> bool:
        """Loop side (reconciliation wrapper): record that the attempt's
        coroutine has provably STARTED, before it can touch the client.

        Runs as the wrapper's first statement — under this condition and
        before the first await — so it is ordered against the Future's
        done-callback no matter which thread that callback fires on. From
        this point the attempt is in flight until note_retry_outcome()
        records the final result: a Future cancelled mid-close goes
        terminal while its loop-side cancellation cleanup is still
        running, and terminal-without-outcome must not free the slot for
        an overlapping close.

        Returns False — and the wrapper must then return WITHOUT touching
        the client — when the attempt's slot was already released or
        reassigned (a genuinely pre-start cancellation freed it, or a
        newer attempt owns it): a stale coroutine that the loop runs
        anyway can never produce a duplicate close.
        """
        with self._cond:
            if self._retry_attempt is not attempt:
                return False
            self._retry_started = True
            return True

    def note_retry_outcome(self, attempt: Any, released: bool) -> None:
        """Loop side (reconciliation wrapper): record the attempt's outcome
        and free the single retry slot, atomically.

        Runs whenever the attempt's coroutine finishes — including when
        the launching caller's own bounded wait already expired, which is
        the point: success settles the generation safe (client reference
        cleared), failure re-records the close failure (client still
        tracked, a later reconciler may retry). The completed result lands
        on the generation instead of dying with the abandoned Future.

        Keyed on the attempt token handed out by launch_retry(): a STALE
        wrapper report — from an attempt whose slot was already released
        (its Future cancelled before the coroutine started, or a newer
        attempt owns the slot now) — is ignored, so it can never clear or
        overwrite a live attempt or settle a generation its own close did
        not confirm.
        """
        with self._cond:
            if self._retry_attempt is not attempt:
                return
            self._retry_future = None
            self._retry_attempt = None
            if self._outcome == "close-failed":
                if released:
                    self._client = None
                    self._outcome = "closed"
                    self.settled.set()
                # else: stays "close-failed" with the client tracked.
            self._cond.notify_all()

    def _release_dead_retry_slot(self, future: "concurrent.futures.Future") -> None:
        """Done-callback: free the retry slot if the attempt died unrecorded
        AND unstarted.

        Fires when the recorded reconciliation Future goes terminal for ANY
        reason — including in the cancelling thread, synchronously, the
        moment a mid-close cancellation lands. A terminal Future is not
        proof the close coroutine stopped: after a mid-close cancel the
        loop-side wrapper is still running its cancellation/finalization
        and may still touch the client. So the slot is freed here only
        when the attempt provably never started (``_retry_started`` still
        False under this condition): cancelled before its coroutine's
        first step, or the loop torn down before it ran. The generation
        then returns to the retryable fail-closed state — ``settled``
        stays clear (nothing was confirmed closed) and the exact client
        stays tracked — instead of being wedged "in-flight" by a dead
        Future.

        A STARTED attempt keeps its slot through Future cancellation: its
        wrapper's note_retry_outcome() — issued only after
        ``_aclose_client`` and all cancellation cleanup completed — is
        the sole release, so no later caller or shutdown can schedule an
        overlapping close while the cancelled cleanup is still live. If
        the wrapper can never report (loop torn down mid-run), the exact
        state stays tracked and in-flight: bounded and fail-closed, never
        overlapped. The identity check makes a late callback from an old
        attempt harmless: it can never free the slot a NEWER in-flight
        attempt owns.
        """
        with self._cond:
            if (
                self._retry_future is not None
                and self._retry_future is future
                and not self._retry_started
            ):
                self._retry_future = None
                self._retry_attempt = None
                self._cond.notify_all()


# ---------------------------------------------------------------------------
# Backward-compatible alias — instances use self._run_sync() instead.
# ---------------------------------------------------------------------------

def _context_thread(target, name: str) -> threading.Thread:
    """Daemon thread running *target* in a snapshot of the spawner's contextvars.
    Threads start with an EMPTY Context; under multiplex_profiles get_secret fails
    closed without the profile's secret scope + HERMES_HOME override. (The shared
    loop needs no wrap: run_coroutine_threadsafe inherits the submitter's context.)"""
    return threading.Thread(target=contextvars.copy_context().run, args=(target,), daemon=True, name=name)


RETAIN_SCHEMA = {
    "name": "hindsight_retain",
    "description": (
        "Store information to long-term memory. Hindsight automatically "
        "extracts structured facts, resolves entities, and indexes for retrieval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to store."},
            "context": {"type": "string", "description": "Short label (e.g. 'user preference', 'project decision')."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Optional per-call tags to merge with configured default retain tags."},
            "occurred_at": {"type": "string", "description": (
                "When the remembered event actually happened, as an ISO-8601 date "
                "or datetime (e.g. '2026-08-20' or '2026-08-20T14:30:00+02:00'). "
                "Pass this whenever the memory references a specific event time "
                "('yesterday', 'last Tuesday', 'on March 3rd') so Hindsight can "
                "anchor it on the timeline. Omit for timeless facts/preferences."
            )},
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "hindsight_recall",
    "description": (
        "Search long-term memory. Returns memories ranked by relevance using "
        "semantic search, keyword matching, entity graph traversal, and reranking."
    ),
    "parameters": {"type": "object", "required": ["query"],
                   "properties": {"query": {"type": "string", "description": "What to search for."}}},
}

REFLECT_SCHEMA = {
    "name": "hindsight_reflect",
    "description": (
        "Synthesize a reasoned answer from long-term memories. Unlike recall, "
        "this reasons across all stored memories to produce a coherent response."
    ),
    "parameters": {"type": "object", "required": ["query"],
                   "properties": {"query": {"type": "string", "description": "The question to reflect on."}}},
}


def _load_config() -> dict:
    """$HERMES_HOME/hindsight/config.json (profile-scoped), else ~/.hindsight/config.json
    (legacy, shared), else environment variables."""
    for path in (get_hermes_home() / "hindsight" / "config.json", Path.home() / ".hindsight" / "config.json"):
        if path.exists():
            with contextlib.suppress(Exception):
                return json.loads(path.read_text(encoding="utf-8"))
    return {
        "mode": os.environ.get("HINDSIGHT_MODE", "cloud"),
        "apiKey": get_secret("HINDSIGHT_API_KEY", ""),
        "timeout": _parse_int_setting(os.environ.get("HINDSIGHT_TIMEOUT"), _DEFAULT_TIMEOUT),
        "idle_timeout": _parse_int_setting(os.environ.get("HINDSIGHT_IDLE_TIMEOUT"), _DEFAULT_IDLE_TIMEOUT),
        "retain_tags": os.environ.get("HINDSIGHT_RETAIN_TAGS", ""),
        "observation_scopes": os.environ.get("HINDSIGHT_RETAIN_OBSERVATION_SCOPES", ""),
        "retain_source": os.environ.get("HINDSIGHT_RETAIN_SOURCE", _DEFAULT_RETAIN_SOURCE),
        "retain_user_prefix": os.environ.get("HINDSIGHT_RETAIN_USER_PREFIX", "User"),
        "retain_assistant_prefix": os.environ.get("HINDSIGHT_RETAIN_ASSISTANT_PREFIX", "Assistant"),
        "banks": {"hermes": {"bankId": os.environ.get("HINDSIGHT_BANK_ID", "hermes"),
                             "budget": os.environ.get("HINDSIGHT_BUDGET", "mid"), "enabled": True}},
    }


def _event_timestamp() -> str:
    """Configured Hermes event time with an explicit UTC offset."""
    event_time = _hermes_now()
    # hermes_time.now() is aware; guard a replacement clock emitting offset-less dates.
    if event_time.tzinfo is None or event_time.utcoffset() is None:
        event_time = event_time.astimezone()
    return event_time.isoformat(timespec="seconds")


def _mint_document_id(session_id: str) -> str:
    """Per-process document id: reusing session_id alone overwrote the document on
    /resume (the reloaded session's first retain replaced the stored content)."""
    return f"{session_id}-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


# initialize() kwargs copied verbatim (str, stripped) onto ``self._<name>``.
_SESSION_KWARGS = (
    "platform", "user_id", "user_name", "chat_id", "chat_name", "chat_type",
    "thread_id", "agent_identity", "agent_workspace",
)
# Retain metadata keys, each stamped from the attribute of the same name when set.
_METADATA_ATTRS = (
    "session_id", "platform", "user_id", "user_name", "chat_id", "chat_name",
    "chat_type", "thread_id", "agent_identity",
)
_SYSTEM_PROMPT_TAILS = {
    "context": "Relevant memories are automatically injected into context.",
    "tools": ("Use hindsight_recall to search, hindsight_reflect for synthesis, "
              "hindsight_retain to store facts."),
    "hybrid": ("Relevant memories are automatically injected into context. "
               "Use hindsight_recall to search, hindsight_reflect for synthesis, "
               "hindsight_retain to store facts."),
}


class HindsightMemoryProvider(MemoryProvider):
    """Hindsight long-term memory with knowledge graph and multi-strategy retrieval."""

    # Each server-side op status poll is a round trip — coarser than the 0.05s queue poll.
    _RETAIN_OP_POLL_INTERVAL_S = 0.5

    def backup_paths(self) -> List[str]:
        """Legacy shared config + embedded-mode profile env files live under ~/.hindsight."""
        with contextlib.suppress(Exception):
            return [str(Path.home() / ".hindsight")]
        return []

    def __init__(self):
        self._config = self._api_key = self._client = None
        self._api_url, self._llm_base_url, self._mode = _DEFAULT_API_URL, "", "cloud"
        self._timeout, self._idle_timeout = _DEFAULT_TIMEOUT, _DEFAULT_IDLE_TIMEOUT
        self._bank_id, self._budget, self._bank_id_template = "hermes", "mid", ""
        self._bank_mission, self._bank_retain_mission = "", None
        self._memory_mode = "hybrid"  # "context", "tools", or "hybrid"
        self._prefetch_method = "recall"  # "recall" or "reflect"
        for name in _SESSION_KWARGS:
            setattr(self, f"_{name}", "")
        self._session_id = self._parent_session_id = self._document_id = ""
        # Serializes client (re)construction so concurrent first-use callers
        # (foreground tool thread, prefetch thread, writer thread) can't each
        # build a client and leak all but the last one.
        self._client_lock = threading.Lock()
        # Serializes PUBLICATION of a client into ``self._client`` against
        # shutdown()'s close sweep. Callers may hold _client_lock across
        # their whole bounded setup/reconciliation wait, so shutdown must
        # not queue behind that lock — this one is only ever held for a
        # check-and-assign (a handful of instructions) on every path: the
        # caller-thread and owner-loop publishes, _install_client, and the
        # sweep's read-and-drop. The owning loop only ever TRY-acquires it,
        # so no path can park the loop thread. A publisher that observes
        # ``_shutting_down`` set inside this serialization publishes nothing
        # and owns the client's exactly-once release; one that published
        # before the sweep acquired it leaves the client for the sweep to
        # close. Either order, each client gets exactly one closer.
        self._publish_lock = threading.RLock()
        # Clients already claimed for shutdown/recreate close — survives
        # generation clear so a late _install_client cannot launch a second
        # close after shutdown confirmed the first.
        self._shutdown_claimed_client_ids: set[int] = set()
        self._shutdown_settled_client_ids: set[int] = set()
        # A first-client setup whose waiting caller timed out but whose
        # coroutine is still building (or cleaning up) on the owning loop.
        # Its eventual client gets closed by that coroutine; the next
        # builder joins it first (see _join_abandoned_client_setup) —
        # bounded, and fail-closed while the generation is unsettled — so
        # no replacement is installed while the displaced client may still
        # be live. A close that failed keeps the exact client (and any
        # in-flight reconciliation attempt) tracked here until a join or
        # shutdown settles the generation safely; the reference is never
        # dropped while the outcome is unresolved.
        self._abandoned_setup: _ClientSetup | None = None
        # Additional unresolved generations when the primary abandoned slot
        # is already occupied. The primary slot (_abandoned_setup) is never
        # overwritten while a generation there is still unsettled.
        self._extra_abandoned_setups: list[_ClientSetup] = []
        # Owner-loop pending registrations queued without blocking on
        # _publish_lock contention; drained into the official lists
        # whenever _publish_lock is held.
        self._pending_abandoned_setups: list[_ClientSetup] = []
        # The first-setup generation currently in its offer/claim/install
        # handoff: set by _await_client_setup() (under _client_lock, before
        # the build is scheduled) and cleared only after the caller has
        # INSTALLED the finished client (or the setup failed / became
        # abandoned). This keeps an active first setup observably in flight
        # through the whole ownership handoff — including the window where
        # the setup Future is already ready but the caller has not yet
        # assigned self._client — so the owner-loop _get_client() branch
        # fails closed promptly instead of building and installing a second
        # client the resuming caller would then overwrite (stranding the
        # loop-built one live, uninstalled, and unclosed).
        self._active_setup: _ClientSetup | None = None
        # Bank defaults (retain mission + seeded directives) apply once, on
        # first client creation — see _apply_bank_defaults().
        self._bank_defaults_applied = False
        self._status_callback: Optional[Callable[[str], None]] = None

        # Retain: single-writer model — sync_turn() enqueues, one writer thread
        # drains sequentially (ad-hoc threads raced interpreter shutdown:
        # "cannot schedule new futures" / "Unclosed client session").
        self._retain_queue: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._sync_thread = None  # legacy alias external callers may join; points at the writer
        self._shutting_down = threading.Event()
        self._atexit_registered = False
        self._retain_tags: List[str] = []
        self._tags: list[str] | None = None
        self._retain_source = _DEFAULT_RETAIN_SOURCE
        self._retain_user_prefix, self._retain_assistant_prefix = "User", "Assistant"
        self._turn_counter = self._turn_index = 0
        self._session_turns: list[str] = []  # ALL turns for the session
        self._last_retained_turn_count = 0  # append-mode delta watermark
        # Server-side async retain ops still in flight: aretain_batch returns on
        # *acceptance*, not durability, so the prefetch gates on these via
        # get_operation_status (a drained local queue is not a read-after-write signal).
        self._pending_retain_ops: set[str] = set()
        self._pending_retain_ops_lock = threading.Lock()
        self._retain_ops_bank_id = ""
        self._apply_retain_policy({})

        # Recall: pending prefetch block + count, and the indicator state (recall_status()).
        self._prefetch_result, self._prefetch_count = "", 0
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._last_recall_returned, self._last_recall_count = False, 0
        self._apply_recall_settings({})

    @property
    def name(self) -> str:
        return "hindsight"

    def is_available(self) -> bool:
        try:
            cfg = _load_config()
            mode = cfg.get("mode", "cloud")
            if mode in _LOCAL_MODES:
                return _check_local_runtime()[0]
            return mode == "local_external" or bool(
                _cloud_api_key(cfg) or cfg.get("api_url") or os.environ.get("HINDSIGHT_API_URL", ""))
        except Exception:
            return False

    def unavailable_reason(self) -> str:
        """Install hint for an unavailable local_embedded runtime (is_available() gates
        initialize() out, so the hint it would log never fires; agent_init shows this).

        ``is_available()`` returns False for local modes when the embedded runtime can't be imported, so
        ``initialize()`` — and the hint it would log — is never reached (#7718). Surface the install
        guidance here, where agent_init warns about an unavailable provider.
        """
        try:
            if _load_config().get("mode", "cloud") not in _LOCAL_MODES:
                return ""
        except Exception:
            return ""
        available, reason = _check_local_runtime()
        return "" if available else _local_runtime_hint(reason).strip()

    def save_config(self, values, hermes_home):
        """Merge *values* into $HERMES_HOME/hindsight/config.json."""
        from utils import atomic_json_write
        config_path = Path(hermes_home) / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_path.exists():
            with contextlib.suppress(Exception):
                existing = json.loads(config_path.read_text(encoding="utf-8"))
        existing.update(values)
        atomic_json_write(config_path, existing, mode=0o600)

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Custom setup wizard — installs only the deps needed for the selected mode."""
        from .setup import run_setup
        run_setup(self, hermes_home, config)

    def get_config_schema(self):
        return [
            {"key": "mode", "description": "Connection mode", "default": "cloud", "choices": ["cloud", "local_embedded", "local_external"]},
            # Cloud mode
            {"key": "api_url", "description": "Hindsight Cloud API URL", "default": _DEFAULT_API_URL, "when": {"mode": "cloud"}},
            {"key": "api_key", "description": "Hindsight Cloud API key", "secret": True, "env_var": "HINDSIGHT_API_KEY", "url": "https://ui.hindsight.vectorize.io", "when": {"mode": "cloud"}},
            # Local external mode
            {"key": "api_url", "description": "Hindsight API URL", "default": _DEFAULT_LOCAL_URL, "when": {"mode": "local_external"}},
            {"key": "api_key", "description": "API key (optional)", "secret": True, "env_var": "HINDSIGHT_API_KEY", "when": {"mode": "local_external"}},
            # Local embedded mode
            {"key": "llm_provider", "description": "LLM provider", "default": "openai", "choices": ["openai", "anthropic", "gemini", "groq", "openrouter", "minimax", "ollama", "lmstudio", "openai_compatible"], "when": {"mode": "local_embedded"}},
            {"key": "llm_base_url", "description": "Endpoint URL (e.g. http://192.168.1.10:8080/v1)", "default": "", "when": {"mode": "local_embedded", "llm_provider": "openai_compatible"}},
            {"key": "llm_api_key", "description": "LLM API key (optional for openai_compatible)", "secret": True, "env_var": "HINDSIGHT_LLM_API_KEY", "when": {"mode": "local_embedded"}},
            {"key": "llm_model", "description": "LLM model", "default": "gpt-4o-mini", "default_from": {"field": "llm_provider", "map": _PROVIDER_DEFAULT_MODELS}, "when": {"mode": "local_embedded"}},
            {"key": "bank_id", "description": "Memory bank name (static fallback when bank_id_template is unset)", "default": "hermes"},
            {"key": "bank_id_template", "description": "Optional template to derive bank_id dynamically. Placeholders: {profile}, {workspace}, {platform}, {user}, {session}, {chat}. Example: hermes-{profile} or winnie-{platform}-{chat}", "default": ""},
            {"key": "bank_mission", "description": "Mission/purpose description for the memory bank"},
            {"key": "bank_retain_mission", "description": "Custom extraction prompt for memory retention"},
            {"key": "recall_budget", "description": "Recall thoroughness", "default": "mid", "choices": ["low", "mid", "high"]},
            {"key": "memory_mode", "description": "Memory integration mode", "default": "hybrid", "choices": ["hybrid", "context", "tools"]},
            {"key": "recall_prefetch_method", "description": "Auto-recall method", "default": "recall", "choices": ["recall", "reflect"]},
            {"key": "retain_tags", "description": "Default tags applied to retained memories (comma-separated)", "default": ""},
            {"key": "observation_scopes", "description": "How observations are scoped during consolidation: 'combined' (default — one pass over all tags), 'per_tag' (one isolated observation per tag), 'all_combinations' (every tag subset — expensive), or a JSON list of tag-lists for explicit custom scopes. Empty uses Hindsight's 'combined' default.", "default": ""},
            {"key": "retain_source", "description": "Metadata source value attached to retained memories (identifies the client that stored them)", "default": _DEFAULT_RETAIN_SOURCE},
            {"key": "retain_user_prefix", "description": "Label used before user turns in retained transcripts", "default": "User"},
            {"key": "retain_assistant_prefix", "description": "Label used before assistant turns in retained transcripts", "default": "Assistant"},
            {"key": "recall_tags", "description": "Tags to filter when searching memories (comma-separated)", "default": ""},
            {"key": "recall_tags_match", "description": "Tag matching mode for recall", "default": "any", "choices": ["any", "all", "any_strict", "all_strict"]},
            {"key": "recall_types", "description": "Fact types to surface on recall — applies to both auto-recall and the hindsight_recall tool (comma-separated or list). Defaults to observation-only — observations are Hindsight's consolidated, deduplicated, evidence-grounded knowledge layer; raw world/experience facts are the supporting evidence observations already summarize. Set to e.g. 'observation,world,experience' to also include raw facts.", "default": "observation"},
            {"key": "recall_min_scores", "description": "Per-stage score floors for recall (inclusive, AND-ed) — drop results scoring below any floor. Keys: semantic, keyword, reranker, final. Object or JSON string, e.g. {\"final\": 0.5} or {\"semantic\": 0.2, \"final\": 0.5}. Unset/empty = no floors.", "default": ""},
            {"key": "recall_max_results", "description": "Keep only the top N ranked recall results after score filtering (0 = no cap)", "default": 0},
            {"key": "auto_recall", "description": "Automatically recall memories before each turn", "default": True},
            {"key": "recall_sync", "description": "Recall synchronously against the current message before each turn (higher relevance, adds recall latency to the turn). Default off: recall runs in the background and is injected on the next turn.", "default": False},
            {"key": "recall_indicator", "description": "Show a '👁️ Hindsight — recalled N memories' status line when auto-recall injects memory (turn off for customer-facing agents)", "default": True},
            {"key": "retain_indicator", "description": "Show a '👁️ Hindsight — saving to memory…' status line when a turn is saved to memory (turn off for customer-facing agents)", "default": True},
            {"key": "auto_retain", "description": "Automatically retain conversation turns", "default": True},
            {"key": "retain_every_n_turns", "description": "Retain every N turns (1 = every turn)", "default": 1},
            {"key": "retain_async","description": "Process retain asynchronously on the Hindsight server", "default": True},
            {"key": "prefetch_waits_for_retain", "description": "Have the background next-turn prefetch wait for the just-completed retain to become recall-visible on the server (local queue drain + async operation completion) before recalling, so recall includes the just-completed turn (runs off the reply path, adds no response latency)", "default": True},
            {"key": "prefetch_retain_drain_timeout", "description": "Max seconds the background prefetch waits for the retain to become recall-visible (queue drain + server-side completion) before recalling anyway", "default": 10.0},
            {"key": "retain_context", "description": "Context label for retained memories", "default": "conversation between Hermes Agent and the User"},
            {"key": "recall_max_tokens", "description": "Maximum tokens for recall results", "default": 4096},
            {"key": "recall_max_input_chars", "description": "Maximum input query length for auto-recall", "default": 800},
            {"key": "recall_prompt_preamble", "description": "Custom preamble for recalled memories in context"},
            {"key": "timeout", "description": "API request timeout in seconds", "default": _DEFAULT_TIMEOUT},
            {"key": "idle_timeout", "description": "Embedded daemon idle timeout in seconds (0 disables auto-shutdown)", "default": _DEFAULT_IDLE_TIMEOUT, "when": {"mode": "local_embedded"}},
            {"key": "port_health_grace_timeout", "description": "Seconds to wait for a slow daemon /health before treating it as stale (raise on busy/low-resource hosts; blank uses the 30s default)", "default": "", "when": {"mode": "local_embedded"}},
        ]

    # -- client -------------------------------------------------------------

    def _int_setting(self, key: str, env_var: str, default: int, env_default=None) -> int:
        """Config value if set (explicit 0 preserved), else env var, else default."""
        value = self._config.get(key)
        return _parse_int_setting(os.environ.get(env_var, env_default) if value is None else value, default)

    def _new_embedded_client(self):
        available, reason = _check_local_runtime()
        if not available:
            raise RuntimeError("Hindsight local runtime is unavailable" + (f": {reason}" if reason else ""))
        _ensure_client_dependency()
        from hindsight import HindsightEmbedded
        HindsightEmbedded.__del__ = lambda self: None
        cfg = self._config
        llm_provider = _daemon_llm_provider(cfg.get("llm_provider", ""))
        logger.debug("Creating HindsightEmbedded client (profile=%s, provider=%s)",
                     cfg.get("profile", "hermes"), llm_provider)
        self._idle_timeout = self._int_setting(
            "idle_timeout", "HINDSIGHT_IDLE_TIMEOUT", _DEFAULT_IDLE_TIMEOUT, env_default=self._idle_timeout,
        )
        kwargs = dict(profile=cfg.get("profile", "hermes"), llm_provider=llm_provider,
                      llm_api_key=_embedded_llm_api_key(cfg), llm_model=cfg.get("llm_model", ""),
                      idle_timeout=self._idle_timeout)
        if self._llm_base_url:
            kwargs["llm_base_url"] = self._llm_base_url
        return HindsightEmbedded(**kwargs)

    def _new_cloud_client(self):
        _ensure_client_dependency()
        from hindsight_client import Hindsight
        kwargs = {"base_url": self._api_url, "timeout": float(self._timeout or _DEFAULT_TIMEOUT)}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        logger.debug("Creating Hindsight cloud client (url=%s, has_key=%s, timeout=%s)",
                     self._api_url, bool(self._api_key), kwargs["timeout"])
        return Hindsight(**kwargs)

    def _get_client(self):
        """Return the cached Hindsight client (created once, reused).

        The client is constructed ON the shared Hindsight event loop, never on
        the calling thread. The SDK client (and its lazily-created aiohttp
        ClientSession) binds to whatever event loop is current where it is
        first used; if that happens on a caller thread's loop, every later
        call that _run_sync() schedules onto the shared loop fails with
        aiohttp's "Timeout context manager should be used inside a task"
        (aiohttp.helpers.TimerContext finds no current task on the session's
        loop). Construction on the loop keeps construct/use/retry/close all on
        one owning loop no matter which thread triggers the first call
        (foreground tool, prefetch, writer, bank defaults, session switch).
        """
        if self._client is None:
            if _on_hindsight_loop_thread():
                # Checked BEFORE any lock: nothing on the owning loop thread
                # may wait — see _get_client_on_owning_loop.
                self._get_client_on_owning_loop()
            else:
                with self._client_lock:
                    if self._client is None:
                        # run_coroutine_threadsafe snapshots the submitter's
                        # contextvars context (profile secret scope under
                        # multiplex_profiles), so get_secret() inside
                        # _build_client still resolves profile-scoped keys.
                        client = self._await_client_setup()
                        # The handoff ends HERE, not when the setup Future
                        # became ready: _active_setup stays set across the
                        # whole offer/claim/install window so the owner-loop
                        # branch (which takes no blocking lock) keeps
                        # failing closed until the client is actually
                        # installed. The lock is held continuously, so no
                        # loop-side install can interleave. The install
                        # itself is a fenced publish: _publish_lock
                        # serializes this check-and-assign against
                        # shutdown()'s read-and-drop sweep, so a shutdown
                        # that began while this caller was parked (or in
                        # the ready-but-uninstalled window) leaves the slot
                        # EMPTY — the finished client is disposed of through
                        # the generation instead of installed after the
                        # sweep has already passed (see
                        # _reject_client_after_shutdown).
                        with self._publish_lock:
                            if (
                                client is not None
                                and not self._shutting_down.is_set()
                            ):
                                self._client = client
                                published = True
                            else:
                                published = False
                        if published:
                            self._active_setup = None
                        else:
                            # Raises: a client produced once shutdown began
                            # is never installed or returned as usable.
                            self._reject_client_after_shutdown(client)
        # First client creation is also where bank defaults go out —
        # initialize() stays lazy (it never builds a client), so this is the
        # earliest point the Banks API is actually reachable. No-op on every
        # later call via the _bank_defaults_applied flag.
        self._apply_bank_defaults()
        return self._client

    def _drain_pending_abandoned_setups_unlocked(self) -> None:
        """Merge owner-loop pending registrations; caller holds publish lock."""
        pending = self._pending_abandoned_setups
        if not pending:
            return
        for setup in pending:
            self._register_abandoned_setup_unlocked(setup)
            tracked = setup.tracked_client() or setup.owned_client()
            identity = self._client_identity(tracked)
            if identity is not None:
                self._shutdown_claimed_client_ids.add(identity)
        self._pending_abandoned_setups = []

    def _generation_from_pending(self, client) -> _ClientSetup | None:
        """Lock-free scan of owner-loop pending registrations."""
        if client is None:
            return None
        for setup in list(self._pending_abandoned_setups):
            if setup.owned_client() is client or setup.tracked_client() is client:
                return setup
        return None

    def _abandoned_setup_snapshot_unlocked(self) -> list:
        """Return a consistent copy; caller must hold ``_publish_lock``."""
        self._drain_pending_abandoned_setups_unlocked()
        items: list = []
        if self._abandoned_setup is not None:
            items.append(self._abandoned_setup)
        items.extend(self._extra_abandoned_setups)
        return items

    def _abandoned_setup_snapshot(self) -> list:
        """Return a consistent copy of every abandoned generation."""
        with self._publish_lock:
            return self._abandoned_setup_snapshot_unlocked()

    def _iter_abandoned_setups(self):
        """Yield every abandoned generation the provider is tracking."""
        for setup in self._abandoned_setup_snapshot():
            yield setup

    def _has_unsettled_abandoned_setup(self) -> bool:
        """True while any tracked abandoned generation is not safely settled."""
        for setup in self._iter_abandoned_setups():
            if not setup.settled.is_set():
                return True
        return False

    def _register_abandoned_setup_unlocked(self, setup: _ClientSetup) -> None:
        """Record an unresolved generation; caller must hold ``_publish_lock``.

        Identity-idempotent: registering the same setup object twice is a
        no-op so a duplicate register cannot leave a stale extra entry after
        the primary is cleared.
        """
        if self._abandoned_setup is setup:
            return
        if setup in self._extra_abandoned_setups:
            return
        if self._abandoned_setup is None:
            self._abandoned_setup = setup
        else:
            self._extra_abandoned_setups.append(setup)

    def _register_abandoned_setup(self, setup: _ClientSetup) -> None:
        """Record an unresolved generation without displacing a prior one."""
        with self._publish_lock:
            self._register_abandoned_setup_unlocked(setup)

    def _clear_abandoned_setup_unlocked(self, setup: _ClientSetup) -> None:
        """Drop a safely settled generation; caller must hold ``_publish_lock``."""
        if setup in self._pending_abandoned_setups:
            self._pending_abandoned_setups.remove(setup)
        if self._abandoned_setup is setup:
            self._abandoned_setup = None
        elif setup in self._extra_abandoned_setups:
            self._extra_abandoned_setups.remove(setup)

    def _clear_abandoned_setup(self, setup: _ClientSetup) -> None:
        """Drop a safely settled generation from provider tracking."""
        owned = setup.owned_client()
        with self._publish_lock:
            self._clear_abandoned_setup_unlocked(setup)
            if owned is not None and setup.settled.is_set():
                self._shutdown_settled_client_ids.add(id(owned))
                self._shutdown_claimed_client_ids.add(id(owned))

    def _client_identity(self, client) -> int | None:
        return id(client) if client is not None else None

    def _is_client_shutdown_settled(self, client) -> bool:
        identity = self._client_identity(client)
        if identity is None:
            return True
        with self._publish_lock:
            return identity in self._shutdown_settled_client_ids

    def _mark_client_shutdown_settled_unlocked(self, client) -> None:
        identity = self._client_identity(client)
        if identity is not None:
            self._shutdown_settled_client_ids.add(identity)
            self._shutdown_claimed_client_ids.add(identity)

    def _generation_for_client_unlocked(self, client) -> _ClientSetup | None:
        """Find a tracked generation owning *client*; caller holds publish lock."""
        if client is None:
            return None
        for setup in self._abandoned_setup_snapshot_unlocked():
            if setup.owned_client() is client or setup.tracked_client() is client:
                return setup
        return None

    def _generation_for_client(self, client) -> _ClientSetup | None:
        found = self._generation_from_pending(client)
        if found is not None:
            return found
        with self._publish_lock:
            self._drain_pending_abandoned_setups_unlocked()
            return self._generation_for_client_unlocked(client)

    def _claim_client_for_close_on_owning_loop(
        self, client
    ) -> tuple[_ClientSetup | None, bool]:
        """Claim or register a close generation without blocking the owning loop.

        Returns ``(setup, publish_lock_was_contended)``. *setup* is None when
        the client is already shutdown-settled (checked only after a
        successful try-acquire). On spin exhaustion the client is tracked via
        the lock-free pending list instead of blocking on _publish_lock.
        """
        _SPIN_MAX = 64
        for _ in range(_SPIN_MAX):
            if self._publish_lock.acquire(blocking=False):
                try:
                    identity = self._client_identity(client)
                    if (
                        identity is not None
                        and identity in self._shutdown_settled_client_ids
                    ):
                        return None, False
                    setup = self._claim_client_for_close_unlocked(client)
                    return setup, False
                finally:
                    self._publish_lock.release()
            time.sleep(0)
        setup = _ClientSetup()
        setup.hand_back(client)
        self._pending_abandoned_setups.append(setup)
        return setup, True

    def _claim_client_for_close_unlocked(self, client) -> _ClientSetup:
        """Return or create the single generation that owns *client*'s close."""
        existing = self._generation_for_client_unlocked(client)
        identity = self._client_identity(client)
        if existing is not None:
            if identity is not None:
                self._shutdown_claimed_client_ids.add(identity)
            return existing
        setup = _ClientSetup()
        setup.hand_back(client)
        self._register_abandoned_setup_unlocked(setup)
        if identity is not None:
            self._shutdown_claimed_client_ids.add(identity)
        return setup

    def _claim_client_for_close(self, client) -> _ClientSetup:
        with self._publish_lock:
            return self._claim_client_for_close_unlocked(client)

    def _launch_tracked_close(self, setup: _ClientSetup, client) -> bool:
        """Schedule one reconciliation close when the slot is free."""
        from agent.async_utils import safe_schedule_threadsafe

        if setup.settled.is_set():
            return True
        target = client or setup.owned_client() or setup.tracked_client()
        if target is None:
            return True
        if setup.retry_future() is not None:
            return True
        return setup.launch_retry(
            lambda attempt: safe_schedule_threadsafe(
                self._reconciliation_close_coro(setup, target, attempt),
                _get_loop(),
            )
        )

    def _is_on_owning_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is _get_loop()
        except RuntimeError:
            return False

    def _release_loser_client(self, client) -> None:
        """Track and close a displaced client through the generation protocol."""
        if client is None:
            return
        if self._is_client_shutdown_settled(client):
            return
        setup = self._claim_client_for_close(client)
        if self._is_on_owning_loop():
            self._launch_tracked_close(setup, client)
            return
        self._reconcile_close_attempt(setup)
        if setup.settled.is_set():
            self._clear_abandoned_setup(setup)

    def _get_client_on_owning_loop(self) -> None:
        """Loop-thread twin of the first-use branch: never waits, fails closed.

        Runs ON the shared Hindsight loop thread (e.g. an operation lambda
        that itself needs the first client), so it must not execute ANY
        blocking wait: no blocking _client_lock acquire (a caller thread may
        hold it through its bounded setup/join/reconciliation wait — parking
        the loop behind it stalls every scheduled operation for the whole
        wait), no Event/Condition wait, no Future.result, and no _run_sync
        (the loop would deadlock on a future only it can run; building in
        place is exactly the no-recursion path this branch exists for).

        Reconciling an abandoned first-use generation is caller-thread work
        (_join_abandoned_client_setup waits there, bounded, off the loop),
        so while one exists without a SAFE terminal disposition this branch
        must NOT build or install a replacement beside it — the displaced
        client may still be live. The same applies to an ACTIVE first setup:
        from the moment a caller thread schedules the build until it has
        installed the finished client (_active_setup), the slot's ownership
        is mid-handoff — including the window where the setup Future is
        already ready but the caller has not yet assigned self._client.
        Building here then would either overwrite the caller's client or be
        overwritten by it, stranding the loser live and unclosed. Both cases
        fail CLOSED instead: promptly (no blocking lock, no wait), leaving
        the exact generation, tracked client, and any in-flight close
        attempt recorded for a caller-thread caller, which performs the
        bounded reconciliation and only then builds the replacement.

        A build that started BEFORE any handoff was registered can still
        lose the slot race: the install below happens only under a
        NON-BLOCKING _client_lock acquire with a full recheck. Caller
        threads hold that lock continuously from their final ``_client is
        None`` check through install + handoff clear, so the try-acquire
        either runs entirely before the caller's handoff (the caller then
        sees this client and never starts a setup) or fails while the
        handoff owns the slot. Either way the loser is released on the
        owning loop via create_task — never stored over the winner, never
        orphaned, exactly one terminal owner per constructed client.

        Shutdown fences this path twice: no build starts once
        ``_shutting_down`` is set (fail-closed before constructing
        anything), and a build already in flight when the flag landed is
        refused at the fenced publish (see _publish_lock) and released
        exactly once on this loop through a tracked generation — never
        installed, never returned, never orphaned.
        """
        if self._client is not None:
            return
        if self._shutting_down.is_set():
            # Fail fast BEFORE building: once shutdown begins, no owner-loop
            # first-client setup may construct a client at all. (A build
            # already in flight when the flag landed is fenced at the
            # install below.)
            raise RuntimeError(
                "Hindsight client cannot be built from the owning loop "
                "while shutdown is in progress (provider fail-closed: "
                "no client built)"
            )
        if self._active_setup is not None:
            raise RuntimeError(
                "Hindsight client cannot be built from the owning loop "
                "while a first-client setup/install handoff is in flight "
                "(provider fail-closed: the caller thread owns the slot "
                "through installation — no replacement built)"
            )
        if self._has_unsettled_abandoned_setup():
            raise RuntimeError(
                "Hindsight client cannot be built from the owning loop "
                "while an abandoned client setup is still unsettled "
                "(provider fail-closed: cleanup pending; the exact "
                "generation, tracked client, and any in-flight close "
                "attempt stay recorded for a caller-thread "
                "reconciliation — no replacement built)"
            )
        for setup in list(self._iter_abandoned_setups()):
            if setup.settled.is_set():
                self._clear_abandoned_setup(setup)
        client = self._build_client()
        installed = False
        if self._client_lock.acquire(blocking=False):
            try:
                if self._client is None and self._active_setup is None:
                    # The slot is still free and no handoff can conflict.
                    # (A settled generation that arrived during the build
                    # is confirmed released; its record can go.) The
                    # publish is itself fenced: _publish_lock (TRY-acquired
                    # — this thread IS the owning loop) serializes the
                    # check-and-assign against shutdown()'s read-and-drop
                    # sweep and re-checks _shutting_down inside that
                    # serialization, so a shutdown that began while this
                    # build ran finds the slot empty — never a client
                    # installed after the sweep already passed. The
                    # unsettled-abandoned recheck must not block on
                    # _publish_lock — under contention skip install and
                    # fall through to the tracked-release paths below.
                    if self._publish_lock.acquire(blocking=False):
                        try:
                            unsettled = any(
                                not s.settled.is_set()
                                for s in self._abandoned_setup_snapshot_unlocked()
                            )
                            if (
                                not unsettled
                                and self._client is None
                                and not self._shutting_down.is_set()
                            ):
                                for settled in list(
                                    self._abandoned_setup_snapshot_unlocked()
                                ):
                                    if settled.settled.is_set():
                                        self._clear_abandoned_setup(settled)
                                self._client = client
                                installed = True
                        finally:
                            self._publish_lock.release()
            finally:
                self._client_lock.release()
        if installed:
            return
        if self._shutting_down.is_set():
            # Shutdown fenced this in-place build out of the slot: the
            # client must be closed exactly once on this (owning) loop,
            # with the outcome tracked — never installed, never orphaned.
            # Scheduling only; the loop must not wait on its own close.
            self._release_fenced_client_on_owning_loop(client)
            raise RuntimeError(
                "Hindsight client built on the owning loop was fenced out "
                "by shutdown (provider fail-closed: client tracked for "
                "exactly-one release on the owning loop, no client "
                "installed or returned)"
            )
        # A caller thread owns the slot through its setup/install handoff
        # (or installed a client while this in-place build ran): the
        # caller's side wins, and this duplicate is released on the owning
        # loop — never stored over the winner, never orphaned. The close
        # is tracked on a generation and scheduled without blocking the
        # loop. If NO client is installed now, fail closed rather than hand
        # back None.
        setup, contended = self._claim_client_for_close_on_owning_loop(client)
        if contended:
            if setup is not None:
                self._launch_tracked_close(setup, client)
            raise RuntimeError(
                "Hindsight client slot is owned by an in-flight "
                "setup/install handoff on a caller thread (provider "
                "fail-closed: could not record duplicate build for "
                "tracked release on the owning loop)"
            )
        if setup is None:
            return
        if not self._launch_tracked_close(setup, client):
            raise RuntimeError(
                "Hindsight client slot is owned by an in-flight "
                "setup/install handoff on a caller thread (provider "
                "fail-closed: duplicate build tracked but close could "
                "not be scheduled on the owning loop)"
            )
        if self._client is None:
            raise RuntimeError(
                "Hindsight client slot is owned by an in-flight "
                "setup/install handoff on a caller thread (provider "
                "fail-closed: duplicate build released on the owning loop, "
                "no overwrite)"
            )

    def _release_fenced_client_on_owning_loop(self, client) -> None:
        """Track and schedule one exactly-once release for a client that an
        owner-loop build produced while shutdown fenced the install.

        Runs ON the owning loop thread. A fresh generation is recorded with
        the client handed back (the recorded "close owed" state) and
        registered as the provider's abandoned setup BEFORE the close is
        scheduled — the outcome stays tracked fail-closed even if the loop
        dies before the attempt runs, and a concurrent reconciler (shutdown
        or a caller-thread join) shares the generation's single in-flight
        slot instead of overlapping this close. The attempt is scheduled,
        never awaited: the loop must not block on anything, least of all
        its own close.
        """
        setup, _contended = self._claim_client_for_close_on_owning_loop(client)
        if setup is None:
            return
        self._launch_tracked_close(setup, client)

    def _client_setup_timeout(self) -> float:
        """Wait budget for first-client construction — NOT the request timeout.

        _build_client() may lazy-install the SDK (tools.lazy_deps's
        _venv_pip_install allows 300s) or start an embedded daemon, both far
        slower than any Hindsight HTTP request. Bounding that wait by the
        request timeout let a slow-but-valid setup outlive its caller: the
        caller failed, the client the coroutine eventually created was
        installed by nobody and closed by nobody, and the next caller built a
        second one. Setup therefore waits at least the lazy-install allowance
        (and never less than the configured request timeout, which stays the
        bound for normal retain/recall/reflect operations).
        """
        request_timeout = float(self._timeout or _DEFAULT_TIMEOUT)
        return max(request_timeout, float(_CLIENT_SETUP_TIMEOUT))

    def _await_client_setup(self):
        """Build the first client on the owning loop, setup-safely.

        _build_client() runs in a coroutine on the shared Hindsight loop
        (loop affinity — see _get_client) under a _ClientSetup handshake
        instead of a plain _run_sync: the wait uses _client_setup_timeout(),
        and if it still expires, the setup is marked abandoned so the
        coroutine itself closes the client it completes with, ON the owning
        loop — a completed client can never remain orphaned (installed by
        nobody, closed by nobody, invisible to shutdown()). If even that
        close raises, the setup stays unsettled with the exact client
        tracked, so the next builder's join (or shutdown) must reconcile it
        before any replacement is treated as installed.

        The generation is also registered as the provider's ACTIVE setup
        (_active_setup) before the build is scheduled, and stays registered
        until the caller has INSTALLED the returned client (the caller
        clears it, under _client_lock, right after the assignment). That
        keeps the whole offer/claim/install handoff observably in flight —
        including the window where the setup Future is ready but the caller
        has not yet assigned self._client — so the owner-loop _get_client()
        branch fails closed instead of building a second client the
        resuming caller would overwrite. The registration moves, never
        disappears unsettled: on timeout it becomes the abandoned
        generation (assigned BEFORE the active registration is cleared, so
        the owner-loop branch never observes a gap with neither recorded).

        Runs on a caller thread holding _client_lock; nothing in the
        coroutine takes that lock, so the bounded waits below cannot
        deadlock against loop-side work.

        Shutdown fences both sides of the handshake. A setup that has not
        started refuses to start once ``_shutting_down`` is set; a build
        that completes after the flag landed closes its own client ON the
        owning loop and returns None instead of a usable client (a failed
        close stays recorded on the generation); and a client that reached
        the waiting caller before the flag landed is handed to _get_client
        uninstalled — its fenced publish (see _get_client) then disposes of
        it through the generation and fails the caller closed. No side may
        install or return a first client that shutdown has already begun
        to reject.
        """
        self._join_abandoned_client_setup()
        if self._shutting_down.is_set():
            raise RuntimeError(
                "Hindsight is shutting down; no first client will be "
                "built (provider fail-closed: setup refused after "
                "shutdown began)"
            )
        setup = _ClientSetup()
        self._active_setup = setup
        try:
            async def _setup():
                try:
                    client = self._build_client()
                except BaseException:
                    setup.fail()
                    raise
                if self._shutting_down.is_set():
                    # Shutdown began while this first client was being
                    # built: nobody may install or return it as usable, and
                    # the caller-side close sweep has already passed
                    # self._client. Hand it to the generation FIRST (a
                    # disposal close that fails must leave the exact client
                    # tracked) and release it through the generation's
                    # single-slot reconciliation — scheduled here on the
                    # owning loop, never awaited: the loop must not wait on
                    # its own close, and the slot makes any concurrent
                    # reconciler (the caller's disposition, a join, or a
                    # later shutdown) WAIT on this attempt instead of
                    # overlapping it.
                    setup.hand_back(client)
                    launched = setup.launch_retry(
                        lambda attempt: safe_schedule_threadsafe(
                            self._reconciliation_close_coro(setup, client, attempt),
                            _get_loop(),
                        )
                    )
                    if not launched:
                        # Unreachable for this generation (its slot is
                        # free); kept so a defensive failure still closes
                        # the client instead of wedging it un-scheduled.
                        if await self._aclose_client(client):
                            setup.closed()
                        else:
                            setup.close_failed()
                    return None
                if setup.offer(client):
                    return client
                # Every caller gave up while the build kept running — nobody
                # will install this client, and shutdown() can no longer see
                # it. Release it right here, on the owning loop. A failed close
                # is recorded as such (client tracked, settled left clear) so
                # it authorizes nothing: reconciliation must close it first.
                if await self._aclose_client(client):
                    setup.closed()
                else:
                    setup.close_failed()
                return client

            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(_setup(), _get_loop())
            if future is None:
                raise RuntimeError("Hindsight loop unavailable")
            wait = self._client_setup_timeout()
            try:
                client = future.result(timeout=wait)
            except concurrent.futures.TimeoutError:
                if not setup.abandon():
                    # Completed in the instant between the wait expiring and
                    # the abandon check — take what arrived instead of
                    # failing. The active registration stays: the caller
                    # clears it after publishing this client.
                    client = setup.claim()
                    if client is None:
                        # Nothing arrived after all: the active registration
                        # becomes the abandoned generation BEFORE it is
                        # cleared, so the owner-loop branch never sees a
                        # window with the handoff recorded nowhere.
                        self._register_abandoned_setup(setup)
                        raise TimeoutError(
                            f"Hindsight client setup did not complete within "
                            f"{wait:.0f}s; the client it completes with is "
                            "tracked for cleanup on the Hindsight loop, and "
                            "no replacement is built until that cleanup "
                            "settles"
                        ) from None
                    # A client arrived in the abandon window: fall through
                    # uninstalled — the fenced publish in _get_client()
                    # decides whether it may still be published.
                else:
                    # The active registration becomes the abandoned generation
                    # BEFORE it is cleared, so the owner-loop branch never sees
                    # a window with the handoff recorded nowhere.
                    self._register_abandoned_setup(setup)
                    raise TimeoutError(
                        f"Hindsight client setup did not complete within "
                        f"{wait:.0f}s; the client it completes with is "
                        "tracked for cleanup on the Hindsight loop, and "
                        "no replacement is built until that cleanup "
                        "settles"
                    ) from None
            # Whatever the coroutine produced — the finished client, or None
            # when it already disposed of a shutdown-fenced one — goes back
            # to _get_client() uninstalled; its publish-vs-shutdown fence
            # (under _publish_lock) installs the client or drives the
            # generation's exactly-once release and fails this caller
            # closed.
            return client
        except BaseException:
            if self._active_setup is setup:
                self._active_setup = None
            raise

    def _reject_client_after_shutdown(self, client) -> None:
        """Dispose of a finished first client the shutdown fence blocked.

        Runs on the caller thread inside _client_lock (never on the owning
        loop). *client* is the client the setup completed with, or None
        when the setup coroutine already disposed of it on the owning loop
        (it closes a shutdown-fenced build itself; a failed close stays
        recorded on the generation). The generation is handed the client
        and registered as the provider's abandoned setup, then driven
        through the same bounded single-slot reconciliation a join or
        shutdown uses — exactly one close attempt on the owning loop — and
        only a CONFIRMED close clears it; anything else keeps the exact
        generation and tracked client recorded fail-closed. Whatever the
        outcome, this raises: a client produced after shutdown began is
        never installed or returned as usable.
        """
        setup = self._active_setup
        if setup is None:
            setup = _ClientSetup()
        if client is not None:
            setup.hand_back(client)
        self._active_setup = None
        self._register_abandoned_setup(setup)
        settled = self._reconcile_close_attempt(setup)
        if settled:
            self._clear_abandoned_setup(setup)
        else:
            logger.warning(
                "Hindsight: a client finished after shutdown began could "
                "not be confirmed released; keeping the generation and its "
                "tracked client recorded (fail-closed)"
            )
        raise RuntimeError(
            "Hindsight shutdown began during the first-client setup; the "
            "finished client was not installed or returned as usable "
            "(provider fail-closed: the client is released on the owning "
            "Hindsight loop)"
        ) from None

    def _join_abandoned_client_setup(self) -> None:
        """Reconcile a previously abandoned setup, BOUNDED, before another
        client is treated as installed.

        The abandoned setup's coroutine closes the client it completes with
        (see _await_client_setup); building a replacement before that would
        briefly leave two live clients, with nobody left to release the
        displaced one — shutdown() would close only the replacement. But
        this join runs on a caller thread holding _client_lock, and neither
        _build_client() nor the late client's aclose() is mechanically
        guaranteed to finish, so an unbounded wait here would let one wedged
        generation hang every later caller forever.

        The join therefore wakes on any recorded progress — safe settlement
        OR a close failure that needs this caller — instead of sleeping on
        ``settled`` alone: a close the abandoned coroutine already failed
        to make is retried promptly, not after the full setup wait. Each
        join drives at most ONE bounded owning-loop reconciliation attempt
        (via _reconcile_close_attempt, which shares the generation's single
        in-flight slot with any concurrent reconciler) and fails CLOSED
        when the generation cannot be shown safely terminal within the
        budget: the exact generation, tracked client, and any in-flight
        attempt stay recorded for the next caller (or shutdown), no
        replacement is constructed, and the caller gets a bounded
        cleanup-pending error.

        Cannot deadlock: the abandoned coroutine never takes _client_lock,
        and the reconciliation close is scheduled onto the owning loop from
        this caller thread (never from the loop itself, which only ever
        runs the short _aclose_client attempt and records its outcome). The
        lock-held retry window is bounded by the request timeout — the
        same exposure _await_client_setup's own bounded wait already has.
        """
        wait = self._client_setup_timeout()
        for setup in list(self._iter_abandoned_setups()):
            if setup.settled.is_set():
                self._clear_abandoned_setup(setup)
                continue
            state = setup.wait_reconcilable(timeout=wait)
            if state == "settled":
                self._clear_abandoned_setup(setup)
                continue
            if state == "":
                raise TimeoutError(
                    f"Hindsight abandoned client setup has not finished "
                    f"cleaning up within {wait:.0f}s; provider left "
                    "fail-closed (cleanup pending, no replacement built)"
                ) from None
            if not self._reconcile_close_attempt(setup):
                raise TimeoutError(
                    f"Hindsight abandoned client setup did not reach a safe "
                    f"disposition within {wait:.0f}s; provider left "
                    "fail-closed (close failed or still in flight, client "
                    "tracked, no replacement built)"
                ) from None
            if setup.settled.is_set():
                self._clear_abandoned_setup(setup)

    def _reconciliation_close_coro(self, setup: _ClientSetup, client, attempt):
        """Return the coroutine for one reconciliation close attempt.

        Runs ON the owning loop. Its FIRST act — before any await, so it is
        ordered against the Future's done-callback — is
        setup.note_retry_started(attempt): if that is rejected the attempt's
        slot was already released pre-start (or reassigned), and the
        coroutine returns WITHOUT touching the client, so a stale attempt
        the loop runs anyway can never produce a duplicate close. Once
        started, the attempt stays in flight through ANY Future
        cancellation: the outcome is reported through
        setup.note_retry_outcome(attempt, ...) only after _aclose_client
        AND every cancellation/finalization step completed — a close whose
        Future was cancelled mid-run keeps its slot (no overlapping close
        may be scheduled) until this report lands, and the report happens
        no matter how the launching caller's own wait ended, so a close
        that outlives its caller still settles (or re-records) the
        generation and the completed result is never lost. The token keys
        both reports to THIS attempt: if the slot was already released (the
        attempt's Future cancelled before this coroutine started, a newer
        attempt in flight), the reports are ignored rather than clobbering
        the live state.
        """
        async def _close_and_record():
            if not setup.note_retry_started(attempt):
                return
            try:
                released = await self._aclose_client(client)
            except BaseException:
                # Includes CancelledError: a mid-close cancellation means
                # the close did NOT provably complete — record failure (the
                # client stays tracked, the generation stays retryable)
                # rather than settling, and only after the
                # cancellation/finalization above has fully unwound.
                released = False
            setup.note_retry_outcome(attempt, released)

        return _close_and_record()

    def _reconcile_close_attempt(self, setup: _ClientSetup) -> bool:
        """Drive the generation's single reconciliation close, bounded.

        Waits on an attempt another reconciler already launched, or takes
        the generation's single retry slot (launch_retry) and launches one:
        a short _aclose_client scheduled onto the owning loop, which
        records its own outcome when it finishes. Returns True only when
        the generation is safely settled. Returns False — leaving the
        fail-closed state intact — when the caller's request-timeout
        bounded wait expired with the attempt still in flight (the exact
        attempt/client stay tracked; a later reconciler rechecks that SAME
        attempt and never schedules a duplicate close), when the close was
        tried and failed (client stays tracked, ``settled`` stays clear),
        or when nothing could be scheduled.

        Never blocks the owning loop, never takes _client_lock, never
        raises.
        """
        from agent.async_utils import safe_schedule_threadsafe

        if setup.settled.is_set():
            return True
        client = setup.tracked_client()
        if client is not None:
            setup.launch_retry(
                lambda attempt: safe_schedule_threadsafe(
                    self._reconciliation_close_coro(setup, client, attempt),
                    _get_loop(),
                )
            )
        future = setup.retry_future()
        if future is None:
            # The attempt already finished between the state check and
            # here (settled above covers the safe case), or the close
            # could not be scheduled onto the loop, or a dead attempt's
            # done-callback already released the slot — either way,
            # nothing to wait on.
            return setup.settled.is_set()
        try:
            future.result(timeout=float(self._timeout or _DEFAULT_TIMEOUT))
        except concurrent.futures.TimeoutError:
            return False  # attempt still in flight; it stays tracked
        except concurrent.futures.CancelledError:
            # The attempt's Future went terminal. That is NOT proof the
            # close stopped: if the coroutine had already started, its
            # loop-side cancellation cleanup may still be running and the
            # slot stays held until its wrapper records the final outcome
            # (no overlapping close may be scheduled meanwhile). If it
            # provably never started, its done-callback has already
            # released the slot back to the retryable fail-closed state.
            # Either way: not settled, never wedged, never overlapped.
            return setup.settled.is_set()
        except Exception:
            # The wrapper died without recording (loop torn down mid-run).
            return setup.settled.is_set()
        return setup.settled.is_set()

    def _build_client(self):
        """Construct the SDK client object. Must run ON the owning loop.

        Dispatcher only — the embedded/cloud bodies live in _new_embedded_client /
        _new_cloud_client. Every caller (the fail-closed first-setup handshake,
        _get_client_on_owning_loop) relies on construction happening on the
        shared Hindsight loop (loop affinity — see _get_client), so this must
        stay a synchronous call made from a coroutine already running there.
        """
        if self._mode == "local_embedded":
            return self._new_embedded_client()
        return self._new_cloud_client()

    async def _aclose_client(self, client) -> bool:
        """Coroutine twin of _close_client() — close a client on the owning loop.

        The client's aiohttp session is bound to the loop it was constructed on
        (see _get_client), so it must be released there too — closing it from
        any other thread raises "attached to a different loop" before aiohttp
        frees the session, which later surfaces as "Unclosed client session".

        Runs as a coroutine ON the shared loop: scheduled by _close_client
        from a caller thread, or awaited directly by loop-side cleanup that
        must not block on its own loop (the abandoned-setup path in
        _await_client_setup). Never raises.

        Returns True when *client* is released (or was None); False when the
        close attempt itself failed. Callers that must guarantee
        cleanup-before-replacement (the abandoned-setup handshake) use that
        outcome to keep the client tracked and fail closed — coroutine
        completion alone never counts as cleanup.
        """
        if client is None:
            return True
        try:
            if self._mode == "local_embedded":
                # HindsightEmbedded.close() delegates to its sync client.close().
                # Close the embedded inner async client on the shared loop
                # first, then let the wrapper clean up daemon/UI bookkeeping.
                # Only when _client was explicitly set on the wrapper (skip
                # MagicMock auto-attributes in unit tests that seed a plain
                # mock as provider._client).
                inner_client = client.__dict__.get("_client")
                if inner_client is not None and hasattr(inner_client, "aclose"):
                    await inner_client.aclose()
                    try:
                        client._client = None
                    except Exception:
                        pass
                try:
                    client.close()
                except RuntimeError:
                    pass
            else:
                aclose = getattr(client, "aclose", None)
                if callable(aclose):
                    result = aclose()
                    if inspect.isawaitable(result):
                        await result
            return True
        except Exception:
            logger.debug("Hindsight: closing a displaced client failed", exc_info=True)
            return False

    def _close_client(self, client) -> None:
        """Close a client this provider is done with, on the owning loop.

        Never raises. Callers MUST NOT hold _client_lock here: the close does
        I/O on the shared loop, whose thread may itself need _client_lock to
        build a client (see _get_client), so blocking on it while holding the
        lock could deadlock.
        """
        if client is None:
            return
        try:
            self._run_sync(self._aclose_client(client))
        except Exception:
            logger.debug("Hindsight: closing a displaced client failed", exc_info=True)

    def _install_client(self, client):
        """Cache *client* unless a newer client was installed concurrently.

        Returns the client now cached: *client* when the slot was still free,
        otherwise the one another thread installed while *client* was being
        built — the loser is closed instead of being stored over the winner
        (which would orphan the winner's session and strand callers on a
        client bound to a daemon the winner already replaced). A shutdown
        that began before the publish fences it out the same way: the
        client is closed on the owning loop and never published (the
        publish is serialized against the sweep by _publish_lock, so the
        sweep cannot race past it and leave an unclosed client behind).
        """
        shutdown_fenced = False
        fenced_client = None
        with self._client_lock:
            current = self._client
            if current is client:
                # Normal path: _get_client() cached this one itself.
                return client
            if current is None:
                with self._publish_lock:
                    if (
                        self._client is None
                        and not self._shutting_down.is_set()
                    ):
                        self._client = client
                        return client
                    if (
                        self._client is None
                        and self._shutting_down.is_set()
                    ):
                        shutdown_fenced = True
                        fenced_client = client
        if shutdown_fenced:
            if self._is_client_shutdown_settled(fenced_client):
                return current
            logger.debug(
                "Hindsight: shutdown fenced client publish; tracking for "
                "exactly-once release on the owning loop"
            )
            setup = self._claim_client_for_close(fenced_client)
            if self._is_on_owning_loop():
                self._launch_tracked_close(setup, fenced_client)
            else:
                self._reconcile_close_attempt(setup)
                if setup.settled.is_set():
                    self._clear_abandoned_setup(setup)
            return current
        # A newer client is installed: *client* loses — closed on the owning
        # loop, never stored over the winner.
        logger.debug(
            "Hindsight: a newer client was installed concurrently; "
            "closing the duplicate"
        )
        self._release_loser_client(client)
        return current

    def _recreate_client(self, stale):
        """Replace the cached client with a freshly built one and return it.

        Used by _run_hindsight_operation after a stale embedded-daemon
        connection error. The client that failed must reach a tracked,
        confirmed terminal settlement through the generation's single-slot
        reconciliation protocol BEFORE any replacement is built or
        published — a failed, pending, canceled, shutdown-racing, or
        unconfirmable close leaves the provider fail-closed with the exact
        stale generation still recorded. Registration of the stale
        generation and clearing the published slot happen atomically under
        _client_lock then _publish_lock (register+clear in one critical
        section, never clear-then-later-track). If another thread installed
        a fresher client while the stale close was settling, that newer
        client wins and is returned without clobbering it.
        """
        if self._shutting_down.is_set():
            raise RuntimeError(
                "Hindsight is shutting down; stale client will not be "
                "replaced (provider fail-closed: shutdown owns cleanup)"
            )

        with self._client_lock:
            current = self._client
            if current is not stale:
                return current

        setup = _ClientSetup()
        setup.hand_back(stale)

        with self._client_lock:
            with self._publish_lock:
                if self._shutting_down.is_set():
                    raise RuntimeError(
                        "Hindsight is shutting down; stale client will not be "
                        "replaced (provider fail-closed: shutdown owns "
                        "cleanup)"
                    )
                current = self._client
                if current is not stale:
                    return current
                self._register_abandoned_setup(setup)
                self._client = None

        if not self._reconcile_close_attempt(setup):
            raise RuntimeError(
                "Hindsight stale client could not be confirmed released; "
                "provider left fail-closed (close failed or still in flight, "
                "client tracked, no replacement built)"
            )
        if setup.settled.is_set():
            self._clear_abandoned_setup(setup)

        with self._client_lock:
            current = self._client
            if current is not None:
                return current

        return self._install_client(self._get_client())

    def _apply_bank_defaults(self) -> None:
        """Apply bank missions and seed the default recall directives.

        Pushes ``bank_mission`` / ``bank_retain_mission`` (README: "Applied
        via Banks API") with one ``update_bank_config`` call, defaulting the
        retain mission to _DEFAULT_BANK_RETAIN_MISSION when the config key is
        unset/empty, then creates each _DEFAULT_BANK_DIRECTIVES entry that the
        bank doesn't already have (matched by ``name``), so existing banks are
        never duplicated or overwritten.

        Once per provider lifetime, best-effort: every failure is logged and
        skipped — bank tuning must never raise into chat or block memory I/O.
        """
        if self._bank_defaults_applied or self._mode == "disabled":
            return
        # Set before any I/O so a concurrent first-use caller (embedded daemon
        # start racing the first recall) can't double-apply.
        self._bank_defaults_applied = True
        try:
            self._get_client()
        except Exception as exc:
            logger.debug("Hindsight bank defaults skipped (no client): %s", exc)
            return

        retain_mission = self._bank_retain_mission or _DEFAULT_BANK_RETAIN_MISSION
        reflect_mission = self._bank_mission or None
        # Same PATCH payload the sync Hindsight.update_bank_config() wrapper
        # builds — {"updates": {non-None kwargs}} — sent via its async twin.
        # The sync wrapper must NOT be used here: it runs the request via
        # asyncio.get_event_loop().run_until_complete() on the CALLING thread,
        # binding the SDK's aiohttp session to that thread's loop instead of
        # the shared Hindsight loop every other call is scheduled on.
        updates: Dict[str, Any] = {"retain_mission": retain_mission}
        if reflect_mission is not None:
            updates["reflect_mission"] = reflect_mission
        request_timeout = float(self._timeout or _DEFAULT_TIMEOUT)
        try:
            self._run_hindsight_operation(
                lambda c: c._aupdate_bank_config(self._bank_id, updates)
            )
            logger.debug("Hindsight bank config applied: bank=%s, retain_mission=%s, reflect_mission=%s",
                         self._bank_id,
                         "default" if not self._bank_retain_mission else "custom",
                         "set" if reflect_mission else "unset")
        except Exception as exc:
            logger.warning("Hindsight update_bank_config failed for bank %s: %s",
                           self._bank_id, exc)

        try:
            # Async-only generated API (client.directives.*) — the high-level
            # Hindsight.list_directives()/create_directive() are sync
            # _run_async wrappers with the same wrong-loop problem as above.
            # The create body is sent as a plain dict because it serializes
            # identically to CreateDirectiveRequest and keeps this path free
            # of SDK model imports (hindsight-client is a lazy dependency).
            response = self._run_hindsight_operation(
                lambda c: c.directives.list_directives(
                    self._bank_id, _request_timeout=request_timeout
                )
            )
            existing = {
                str(getattr(item, "name", "") or "")
                for item in (getattr(response, "items", None) or [])
            }
        except Exception as exc:
            logger.warning("Hindsight list_directives failed for bank %s: %s",
                           self._bank_id, exc)
            return
        for directive in _DEFAULT_BANK_DIRECTIVES:
            if directive["name"] in existing:
                continue
            try:
                self._run_hindsight_operation(
                    lambda c, d=directive: c.directives.create_directive(
                        self._bank_id,
                        {
                            "name": d["name"],
                            "content": d["content"],
                            "priority": d["priority"],
                            "is_active": True,
                        },
                        _request_timeout=request_timeout,
                    )
                )
            except Exception as exc:
                logger.warning("Hindsight create_directive(%s) failed for bank %s: %s",
                               directive["name"], self._bank_id, exc)

    def _run_sync(self, coro):
        """Schedule *coro* on the shared loop using the configured timeout."""
        return _run_sync(coro, timeout=self._timeout)

    # -- retain writer thread + server-side visibility -------------------------

    def _ensure_writer(self) -> None:
        """Lazy-start the single retain-writer thread (tools-only providers never pay for it)."""
        if (thread := self._writer_thread) is not None and thread.is_alive():
            return
        if self._shutting_down.is_set():
            # Once shutdown begins the monotonic fence stays set — never
            # resurrect a writer that could accept new retain jobs.
            return
        # Per-provider background threads start with an EMPTY contextvars
        # Context. Under multiplex_profiles the spawning thread carries the
        # profile's secret scope + HERMES_HOME override (gateway/run.py wraps
        # the agent turn in copy_context().run), and get_secret fails closed
        # without it (#92608). Snapshot the spawner's context into the thread.
        # (The shared ``hindsight-loop`` thread needs no wrap: coroutines
        # scheduled via run_coroutine_threadsafe inherit the submitter's
        # context per call, so one loop can serve every profile.)
        thread = threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._writer_loop,),
            daemon=True,
            name="hindsight-writer",
        )
        self._writer_thread = thread
        # Keep the legacy _sync_thread alias pointing at the writer so any
        # external code that joins _sync_thread keeps working.
        self._sync_thread = thread
        thread.start()

    def _register_atexit(self) -> None:
        """Idempotent atexit drain: a CLI exit that skips MemoryManager.shutdown_all()
        must not race interpreter teardown."""
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self._atexit_shutdown)

    def _writer_loop(self) -> None:
        """Drain the retain queue serially until the sentinel. A failing job can't
        kill the writer; task_done() always fires so queue.join() works."""
        while True:
            try:
                job = self._retain_queue.get(timeout=1.0)
            except queue.Empty:
                if self._shutting_down.is_set():
                    return
                continue
            try:
                if job is _WRITER_SENTINEL:
                    return
                job()
            except Exception as exc:
                logger.warning("Hindsight retain failed: %s", exc, exc_info=True)
            finally:
                self._retain_queue.task_done()

    def _atexit_shutdown(self) -> None:
        try:
            if not self._shutting_down.is_set():
                self.shutdown()
        except Exception as exc:
            logger.debug("Hindsight atexit shutdown failed: %s", exc)

    def _run_hindsight_operation(self, operation):
        """Run an async Hindsight client operation, retrying once after idle shutdown.

        The operation callable is invoked INSIDE a coroutine on the shared
        Hindsight loop, never on the calling thread. hindsight-client ships
        several methods as sync ``_run_async`` wrappers that execute on
        ``asyncio.get_event_loop()`` of whichever thread calls them; when such
        a call runs on a foreground/writer/prefetch thread it binds the SDK's
        aiohttp session to that thread's loop, and every subsequent call
        scheduled here fails with aiohttp's "Timeout context manager should
        be used inside a task". Evaluating the callable on the owning loop
        keeps construct/use/retry/close on one loop.
        """
        async def _invoke(client):
            result = operation(client)
            if inspect.isawaitable(result):
                result = await result
            return result

        client = self._get_client()
        try:
            return self._run_sync(_invoke(client))
        except Exception as exc:
            # Stale embedded-daemon connection failure only (upstream's marker
            # set in .embedded is a superset of the fork's inline tuple).
            text = f"{type(exc).__name__}: {exc}".lower()
            if self._mode != "local_embedded" or not any(m in text for m in _RETRIABLE_CONNECTION_MARKERS):
                raise
            logger.info(
                "Hindsight embedded daemon appears unreachable; recreating client and retrying once: %s",
                exc,
            )
            client = self._recreate_client(client)
            return self._run_sync(_invoke(client))

    def _track_retain_ops(self, retain_response, bank_id: str) -> None:
        """Record the async ``operation_id``/``operation_ids`` of an aretain_batch reply
        (pending until recall-visible). No id (older API / sync completion) leaves
        only the local queue drain as a signal."""
        raw_ids = [getattr(retain_response, "operation_id", None), *(getattr(retain_response, "operation_ids", None) or [])]
        if ids := [str(op) for op in raw_ids if op]:
            self._retain_ops_bank_id = bank_id
            with self._pending_retain_ops_lock:
                self._pending_retain_ops.update(ids)

    def _is_retain_op_complete(self, bank_id: str, op_id: str) -> bool:
        """True when a server-side retain op is done or gone (completed ops are evicted,
        so 404 = no longer pending). Transient errors -> False, caller keeps waiting."""
        from hindsight_client_api.exceptions import NotFoundException

        try:
            resp = self._run_hindsight_operation(
                lambda client: client.operations.get_operation_status(bank_id=bank_id, operation_id=op_id)
            )
        except NotFoundException:
            return True
        except Exception as exc:
            logger.debug("Prefetch: operation status check failed for %s: %s", op_id, exc)
            return False
        return str(getattr(resp, "status", "") or "").lower() in {"completed", "failed"}

    def _wait_for_retains_drained(self, timeout: float) -> bool:
        """Block up to *timeout* s for the last retain to become recall-visible
        (prefetch thread only, never the reply path). Two barriers on one budget:
        (1) the writer queue drains — polls ``unfinished_tasks`` rather than
        ``queue.join()`` so a wedged write can't hang the prefetch; (2) the
        server-side async ops complete (async retain returns on acceptance, not
        durability). False on timeout/shutdown."""
        deadline = None if timeout <= 0 else time.monotonic() + timeout
        expired = lambda: deadline is not None and time.monotonic() >= deadline  # noqa: E731
        while self._retain_queue.unfinished_tasks > 0:
            if self._shutting_down.is_set():
                return False
            if expired():
                logger.debug("Prefetch: retain drain timed out after %.1fs (%d pending)",
                             timeout, self._retain_queue.unfinished_tasks)
                return False
            time.sleep(0.05)
        return self._wait_for_server_retain_ops(expired, timeout)

    def _wait_for_server_retain_ops(self, _expired: Callable[[], bool], timeout: float) -> bool:
        """Poll tracked async retain ops until complete or *_expired()* (deadline
        predicate). Ops still pending at the deadline are DROPPED: keeping them
        would let a permanently failing status endpoint burn the full timeout on
        EVERY later prefetch (a per-turn latency penalty via prefetch()'s bounded
        join). Trades a possibly-stale recall for liveness; WARNING once per prefetch."""
        while True:
            with self._pending_retain_ops_lock:
                bank_id = self._retain_ops_bank_id or self._bank_id
                pending = list(self._pending_retain_ops)
            if not pending:
                return True
            if self._shutting_down.is_set():
                return False
            done: set[str] = set()
            for op_id in pending:
                if self._shutting_down.is_set():
                    return False
                if _expired():
                    break
                if self._is_retain_op_complete(bank_id, op_id):
                    done.add(op_id)
            with self._pending_retain_ops_lock:
                self._pending_retain_ops.difference_update(done)
                if not self._pending_retain_ops:
                    return True
                dropped = len(self._pending_retain_ops) if _expired() else 0
                if dropped:
                    self._pending_retain_ops.clear()
            if dropped:
                logger.warning("Prefetch: server retain visibility timed out after %.1fs; "
                               "dropping %d unresolved op(s) so later prefetches stay "
                               "bounded (recall may miss the just-completed turn)", timeout, dropped)
                return False
            time.sleep(self._RETAIN_OP_POLL_INTERVAL_S)

    # -- retain target -----------------------------------------------------------

    def _resolve_retain_target(self, fallback_document_id: str) -> tuple[str, str | None]:
        """(document_id, update_mode) from live API capability: >= 0.5.0 reuses the
        stable session-scoped id with ``update_mode='append'``; older APIs get
        *fallback_document_id* (per-process unique) and no update_mode — the only
        way the resume-overwrite fix works there. The /version probe targets the
        embedded client's dynamic per-profile port when running, else api_url.

        On Hindsight ≥ 0.5.0 the API supports ``update_mode='append'``, which lets us reuse a stable
        session-scoped ``document_id`` across process lifecycles without overwriting prior turns. On older
        APIs we fall back to *fallback_document_id* (the per-process unique ``f"{session_id}-{start_ts}"``
        minted at initialize / switch time) and don't pass ``update_mode`` at all — that's the only way the
        resume-overwrite fix (#6654) keeps working on legacy servers.
        """
        url = getattr(self._client, "url", None) if self._mode == "local_embedded" else None
        probe_url = str(url) if url else (self._api_url or "")
        if self._session_id and _check_api_supports_update_mode_append(probe_url, self._api_key):
            return self._session_id, "append"
        return fallback_document_id, None

    # -- lifecycle ---------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "").strip()
        self._parent_session_id = str(kwargs.get("parent_session_id", "") or "").strip()
        # Status channel for the retain indicator (recall reports via recall_status()).
        if callable(kwargs.get("status_callback")):
            self._status_callback = kwargs["status_callback"]
        # session_id stays in tags so processes for one session remain filterable together.
        self._document_id = _mint_document_id(self._session_id)
        _maybe_upgrade_client()

        self._config = cfg = _load_config()
        for name in _SESSION_KWARGS:
            setattr(self, f"_{name}", str(kwargs.get(name) or "").strip())
        self._turn_index = self._last_retained_turn_count = 0
        self._session_turns = []
        self._mode = cfg.get("mode", "cloud")
        self._timeout = self._int_setting("timeout", "HINDSIGHT_TIMEOUT", _DEFAULT_TIMEOUT)
        self._idle_timeout = self._int_setting("idle_timeout", "HINDSIGHT_IDLE_TIMEOUT", _DEFAULT_IDLE_TIMEOUT)
        if self._mode == "local":  # legacy alias
            self._mode = "local_embedded"
        if self._mode == "local_embedded":
            # Must precede the daemon_embed_manager import, which reads it at import time.
            _export_port_health_grace_timeout(cfg)
            available, reason = _check_local_runtime()
            if not available:
                logger.warning("Hindsight local mode disabled because its runtime could not be imported: %s.%s",
                               reason, _local_runtime_hint(reason))
                self._mode = "disabled"
                return
        self._apply_connection_settings(cfg)
        self._apply_retain_settings(cfg)
        self._apply_recall_settings(cfg)

        client_version = "unknown"
        with contextlib.suppress(Exception):
            from importlib.metadata import version as pkg_version
            client_version = pkg_version("hindsight-client")
        logger.info("Hindsight initialized: mode=%s, api_url=%s, bank=%s, budget=%s, memory_mode=%s, prefetch_method=%s, client=%s",
                    self._mode, self._api_url, self._bank_id, self._budget, self._memory_mode, self._prefetch_method, client_version)
        if self._bank_id_template:
            logger.debug("Hindsight bank resolved from template %r: profile=%s workspace=%s platform=%s user=%s chat=%s -> bank=%s",
                         self._bank_id_template, self._agent_identity, self._agent_workspace,
                         self._platform, self._user_id, self._chat_id, self._bank_id)
        logger.debug("Hindsight config: auto_retain=%s, auto_recall=%s, retain_every_n=%d, "
                     "retain_async=%s, retain_context=%s, recall_max_tokens=%d, recall_max_input_chars=%d, "
                     "recall_min_scores=%s, recall_max_results=%d, tags=%s, recall_tags=%s",
                     self._auto_retain, self._auto_recall, self._retain_every_n_turns,
                     self._retain_async, self._retain_context, self._recall_max_tokens, self._recall_max_input_chars,
                     self._recall_min_scores, self._recall_max_results, self._tags, self._recall_tags)

        if self._mode == "local_embedded":
            self._start_embedded_daemon()

    def _apply_connection_settings(self, cfg: dict) -> None:
        """Endpoint, bank and mode selectors from *cfg* (env fallbacks where documented)."""
        self._api_key = _cloud_api_key(cfg)
        default_url = _DEFAULT_LOCAL_URL if self._mode in {"local_embedded", "local_external"} else _DEFAULT_API_URL
        self._api_url = cfg.get("api_url") or os.environ.get("HINDSIGHT_API_URL", default_url)
        self._llm_base_url = cfg.get("llm_base_url", "")

        banks = cfg_get(cfg, "banks", "hermes", default={})
        self._bank_id_template = cfg.get("bank_id_template", "") or ""
        self._bank_id = _resolve_bank_id_template(
            self._bank_id_template,
            fallback=cfg.get("bank_id") or banks.get("bankId", "hermes"),
            profile=self._agent_identity, workspace=self._agent_workspace,
            platform=self._platform, user=self._user_id, session=self._session_id,
            chat=self._chat_id,
        )
        budget = cfg.get("recall_budget") or cfg.get("budget") or banks.get("budget", "mid")
        self._budget = budget if budget in _VALID_BUDGETS else "mid"
        memory_mode = cfg.get("memory_mode", "hybrid")
        self._memory_mode = memory_mode if memory_mode in _SYSTEM_PROMPT_TAILS else "hybrid"
        prefetch_method = cfg.get("recall_prefetch_method") or cfg.get("prefetch_method", "recall")
        self._prefetch_method = prefetch_method if prefetch_method in {"recall", "reflect"} else "recall"
        self._bank_mission = cfg.get("bank_mission", "")
        self._bank_retain_mission = cfg.get("bank_retain_mission") or None

    def _apply_retain_settings(self, cfg: dict) -> None:
        def _cfg_or_env(key: str, env_var: str, default: str = "") -> Any:
            return cfg.get(key) or os.environ.get(env_var, default)

        self._retain_tags = _normalize_retain_tags(_cfg_or_env("retain_tags", "HINDSIGHT_RETAIN_TAGS"))
        self._tags = self._retain_tags or None
        self._observation_scopes = _normalize_observation_scopes(
            _cfg_or_env("observation_scopes", "HINDSIGHT_RETAIN_OBSERVATION_SCOPES"))
        self._retain_source = str(_cfg_or_env("retain_source", "HINDSIGHT_RETAIN_SOURCE", _DEFAULT_RETAIN_SOURCE)).strip()
        self._retain_user_prefix = str(_cfg_or_env("retain_user_prefix", "HINDSIGHT_RETAIN_USER_PREFIX", "User")).strip() or "User"
        self._retain_assistant_prefix = (
            str(_cfg_or_env("retain_assistant_prefix", "HINDSIGHT_RETAIN_ASSISTANT_PREFIX", "Assistant")).strip()
            or "Assistant"
        )
        self._apply_retain_policy(cfg)

    def _apply_retain_policy(self, cfg: dict) -> None:
        """Pure-config retain knobs (no env/secret reads; ``{}`` yields the defaults)."""
        self._auto_retain = cfg.get("auto_retain", True)
        self._retain_every_n_turns = max(1, int(cfg.get("retain_every_n_turns", 1)))
        self._retain_context = cfg.get("retain_context", _RETAIN_CONTEXT_DEFAULT)
        self._retain_async = cfg.get("retain_async", True)
        # On by default so the user SEES memory working whether or not the model
        # mentions it; off switch for customer-facing agents (recall_indicator too).
        self._retain_indicator = bool(cfg.get("retain_indicator", True))
        # The next turn's warm prefetch could read BEFORE an async retain is
        # recall-visible; when True it first waits (bounded, off the reply path)
        # for the queue to drain AND the server-side op(s) to complete.
        self._prefetch_waits_for_retain = cfg.get("prefetch_waits_for_retain", True)
        self._prefetch_retain_drain_timeout = float(cfg.get("prefetch_retain_drain_timeout", 10.0))

    def _apply_recall_settings(self, cfg: dict) -> None:
        """Recall knobs are pure config too (``{}`` yields the defaults)."""
        self._recall_tags = cfg.get("recall_tags") or None
        self._recall_tags_match = cfg.get("recall_tags_match", "any")
        self._auto_recall = cfg.get("auto_recall", True)
        self._recall_sync = bool(cfg.get("recall_sync", False))
        self._recall_max_tokens = int(cfg.get("recall_max_tokens", 4096))
        self._recall_max_input_chars = int(cfg.get("recall_max_input_chars", 800))
        # None -> observation-only (Hindsight's consolidated, deduplicated layer; raw
        # world/experience facts re-ship the evidence they summarize and burn the
        # recall_max_tokens budget); a comma-separated string is accepted for parity
        # with recall_tags; an explicit list broadens or disables the filter.
        configured_types = cfg.get("recall_types")
        if isinstance(configured_types, str):
            self._recall_types = [t.strip() for t in configured_types.split(",") if t.strip()]
        else:
            self._recall_types = list([] if configured_types is None else configured_types) or ["observation"]
        self._recall_prompt_preamble = cfg.get("recall_prompt_preamble", "")
        self._recall_indicator = bool(cfg.get("recall_indicator", True))
        # Strictness knobs (hindsight-client >= 0.9.2): server-side inclusive
        # AND-ed per-stage score floors, and a client-side top-N cap applied
        # after floor filtering. Unset/empty = exact pre-knob behavior.
        self._recall_min_scores = _normalize_min_scores(cfg.get("recall_min_scores"))
        max_results = cfg.get("recall_max_results")
        self._recall_max_results = max(0, int(max_results)) if max_results else 0

    def _start_embedded_daemon(self) -> None:
        """Start the embedded daemon on a background thread (Rich output -> log file)."""
        # PostgreSQL's initdb refuses root; without this guard the start thread
        # retries forever, reloading embedding models (~958MB RAM, ~33% CPU)
        # with no user-visible error.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            msg = ("Hindsight local_embedded mode cannot run as root "
                   "(PostgreSQL initdb refuses root). Skipping the embedded "
                   "memory daemon. Run Hermes as a non-root user, or switch "
                   "to cloud / local_external mode via 'hermes memory setup'.")
            logger.warning(msg)
            # Also print: otherwise the user would only see Hermes get sluggish.
            with contextlib.suppress(Exception):
                # Surface to the terminal too — a daemon that never starts would otherwise fail silently and
                # the user would only see Hermes get sluggish. (issue #13125)
                print(f"  ⚠ {msg}", file=sys.stderr, flush=True)
            self._mode = "disabled"
            return
        _context_thread(self._daemon_start_worker, "hindsight-daemon-start").start()

    def _daemon_start_worker(self) -> None:
        import traceback
        log_path = get_hermes_home() / "logs" / "hindsight-embed.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _log(text: str) -> None:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(text)

        try:
            # Rich console -> our log file (redirecting global fds would capture other threads).
            import hindsight_embed.daemon_embed_manager as dem
            from rich.console import Console
            dem.console = Console(file=open(log_path, "a", encoding="utf-8"), force_terminal=False)

            client = self._get_client()
            profile = self._config.get("profile", "hermes")
            # Profile .env out of sync with config -> rewrite and restart a running daemon.
            if _load_simple_env(_embedded_profile_env_path(self._config)) != _build_embedded_profile_env(self._config):
                _materialize_embedded_profile_env(self._config)
                if client._manager.is_running(profile):
                    _log("\n=== Config changed, restarting daemon ===\n")
                    client._manager.stop(profile)
            client._ensure_started()
            _log("\n=== Daemon started successfully ===\n")
        except Exception as e:
            _log(f"\n=== Daemon startup failed: {e} ===\n" + traceback.format_exc())

    def system_prompt_block(self) -> str:
        mode = self._memory_mode if self._memory_mode in _SYSTEM_PROMPT_TAILS else "hybrid"
        label = "" if mode == "hybrid" else f" ({mode} mode)"
        return f"# Hindsight Memory\nActive{label}. Bank: {self._bank_id}, budget: {self._budget}.\n{_SYSTEM_PROMPT_TAILS[mode]}"

    # -- recall ------------------------------------------------------------------

    def _recall_disabled(self) -> bool:
        """Guards shared by the async and synchronous recall paths."""
        why = ("tools-only mode" if self._memory_mode == "tools" else "auto_recall disabled" if not self._auto_recall
               else "shutting down" if self._shutting_down.is_set() else None)
        if why:
            logger.debug("Prefetch: skipped (%s)", why)
        return why is not None

    def _recall(self, query: str) -> list:
        kwargs: dict = {"bank_id": self._bank_id, "query": query, "budget": self._budget, "max_tokens": self._recall_max_tokens}
        if self._recall_tags:
            kwargs.update(tags=self._recall_tags, tags_match=self._recall_tags_match)
        if self._recall_types:
            kwargs["types"] = self._recall_types
        if self._recall_min_scores is not None:
            kwargs["min_scores"] = self._recall_min_scores
        resp = self._run_hindsight_operation(lambda client: client.arecall(**kwargs))
        return resp.results or []

    def _reflect(self, query: str) -> str | None:
        resp = self._run_hindsight_operation(
            lambda client: client.areflect(bank_id=self._bank_id, query=query, budget=self._budget)
        )
        return resp.text

    def _do_recall(self, query: str) -> tuple[str, int]:
        """One recall/reflect for *query* (background prefetch and ``recall_sync`` paths)
        -> (text, memory count); the count is 0 for reflect (synthesis) and on error."""
        if self._recall_max_input_chars:
            query = query[:self._recall_max_input_chars]
        try:
            if self._prefetch_method == "reflect":
                logger.debug("Recall: calling reflect (bank=%s, query_len=%d)", self._bank_id, len(query))
                return self._reflect(query) or "", 0
            logger.debug("Recall: calling recall (bank=%s, query_len=%d, budget=%s)",
                         self._bank_id, len(query), self._budget)
            results = self._recall(query)
            # The recall API has no server-side max-results param; the server
            # ranks by final score descending, so a client-side slice after
            # floor filtering keeps the highest-ranked N.
            if self._recall_max_results:
                results = results[:self._recall_max_results]
            logger.debug("Recall: returned %d results", len(results))
            return "\n".join(f"- {r.text}" for r in results if r.text), len(results)
        except Exception as e:
            logger.debug("Hindsight recall failed: %s", e, exc_info=True)
            return "", 0

    def _finish_prefetch(self, result: str, count: int) -> str:
        """Record indicator state (cleared on empty turns, never a stale count); format the block."""
        self._last_recall_returned, self._last_recall_count = bool(result), count if result else 0
        if not result:
            logger.debug("Prefetch: no results available")
            return ""
        logger.debug("Prefetch: returning %d chars of context", len(result))
        header = self._recall_prompt_preamble or (
            "# Hindsight Memory (persistent cross-session context)\n"
            "Use this to answer questions about the user and prior sessions. "
            "Do not call tools to look up information that is already present here."
        )
        return f"{header}\n\n{result}"

    def _join_prefetch(self, timeout: float, *, log: bool = False) -> None:
        if not (self._prefetch_thread and self._prefetch_thread.is_alive()):
            return
        if log:
            logger.debug("Prefetch: waiting for background thread to complete")
        self._prefetch_thread.join(timeout=timeout)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Opt-in: recall synchronously against the *current* message so the
        # injected memories match this turn's query, not the previous turn's.
        # See NousResearch/hermes-agent#5820.
        if self._recall_sync:
            return self._finish_prefetch(*(("", 0) if self._recall_disabled() else self._do_recall(query)))
        # Default: the background worker's result for the previous turn (capped join).
        self._join_prefetch(3.0, log=True)
        with self._prefetch_lock:
            result, count = self._prefetch_result, self._prefetch_count
            self._prefetch_result, self._prefetch_count = "", 0
        return self._finish_prefetch(result, count)

    def recall_status(self) -> Optional[RecallStatus]:
        """Count injected by the last prefetch; None if nothing injected or ``recall_indicator=false``."""
        if not self._recall_indicator or not self._last_recall_returned:
            return None
        return RecallStatus(provider_label="Hindsight", count=self._last_recall_count, glyph=_HINDSIGHT_GLYPH)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Sync mode recalls live each turn — nothing to prime in the background.
        if self._recall_sync or self._recall_disabled():
            return

        def _run():
            # Wait (bounded, off the reply path) for the just-completed turn's
            # retain to be recall-visible so the warmed context includes it.
            if self._prefetch_waits_for_retain:
                self._wait_for_retains_drained(self._prefetch_retain_drain_timeout)
            text, count = self._do_recall(query)
            if text:
                with self._prefetch_lock:
                    self._prefetch_result, self._prefetch_count = text, count

        self._prefetch_thread = _context_thread(_run, "hindsight-prefetch")
        self._prefetch_thread.start()

    # -- retain ------------------------------------------------------------------

    def _build_turn_messages(self, user_content: str, assistant_content: str) -> List[Dict[str, str]]:
        now = _event_timestamp()  # one turn -> both messages share the event timestamp
        return [{"role": role, "content": f"{prefix}: {content}", "timestamp": now} for role, prefix, content in
                (("user", self._retain_user_prefix, user_content), ("assistant", self._retain_assistant_prefix, assistant_content))]

    def _build_metadata(self, *, message_count: int, turn_index: int) -> Dict[str, str]:
        metadata: Dict[str, str] = {
            # UTC write/audit time (event time lives on the item timestamp).
            "retained_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "message_count": str(message_count),
            "turn_index": str(turn_index),
        }
        if self._retain_source:
            metadata["source"] = self._retain_source
        metadata.update({name: value for name in _METADATA_ATTRS if (value := getattr(self, f"_{name}"))})
        return metadata

    def _build_retain_kwargs(self, content: str, *, context: str | None = None,
                             metadata: Dict[str, str] | None = None, tags: List[str] | None = None,
                             occurred_at: str | None = None, update_mode: str | None = None) -> Dict[str, Any]:
        """Build one aretain_batch item. The server resolves occurred_start/end (incl.
        relative phrases in content) from the item timestamp: explicit occurred_at
        wins, else the configured event clock."""
        item: Dict[str, Any] = {
            # See #93568.
            "content": content,
            "metadata": metadata or self._build_metadata(message_count=1, turn_index=self._turn_index),
            "timestamp": (occurred_at or "").strip() or _event_timestamp(),
        }
        merged_tags = _normalize_retain_tags(list(self._retain_tags) + _normalize_retain_tags(tags))
        item.update({k: v for k, v in (("context", context), ("update_mode", update_mode)) if v is not None})
        item.update({k: v for k, v in (("tags", merged_tags), ("observation_scopes", self._observation_scopes)) if v})
        return item

    def _retain_batch(self, item: dict, *, bank_id: str, document_id: str | None = None,
                      retain_async: bool | None = None):
        """Dispatch one item via aretain_batch (bank_id/document_id/retain_async are
        call-level args, never item keys)."""
        kwargs: Dict[str, Any] = {"bank_id": bank_id, "items": [item], "document_id": document_id, "retain_async": retain_async}
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return self._run_hindsight_operation(lambda client: client.aretain_batch(**kwargs))

    def _make_turn_retain_job(self, turns: list[str], *, document_id: str, update_mode: str | None,
                              label: str, track_ops: bool = True) -> Callable[[], None]:
        """Writer job shipping *turns* as one document. Inputs are snapshotted NOW: the
        writer runs after later sync_turn() calls mutate _session_turns/_turn_index/_session_id."""
        content = "[" + ",".join(turns) + "]"
        metadata = self._build_metadata(message_count=len(turns) * 2, turn_index=self._turn_index)
        lineage = (("session", self._session_id), ("parent", self._parent_session_id))
        tags = [f"{kind}:{sid}" for kind, sid in lineage if sid] or None
        bank_id, retain_async, retain_context = self._bank_id, self._retain_async, self._retain_context

        def _job() -> None:
            item = self._build_retain_kwargs(content, context=retain_context, metadata=metadata,
                                             tags=tags, update_mode=update_mode)
            logger.debug("Hindsight %s: bank=%s, doc=%s, mode=%s, async=%s, content_len=%d, num_turns=%d",
                         label, bank_id, document_id, update_mode, retain_async, len(content), len(turns))
            resp = self._retain_batch(item, bank_id=bank_id, document_id=document_id, retain_async=retain_async)
            # Async retains are only *accepted* here; track the op id(s) so the
            # next-turn prefetch can wait for true server-side completion.
            if retain_async and track_ops:
                self._track_retain_ops(resp, bank_id)
            logger.debug("Hindsight %s succeeded", label)

        return _job

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Enqueue a retain for the current turn (non-blocking; writer thread). Dropped
        once shutdown() fired so post-exit retains never reach aiohttp during teardown."""
        why = "auto_retain disabled" if not self._auto_retain else "shutting down" if self._shutting_down.is_set() else None
        if why:
            logger.debug("sync_turn: skipped (%s)", why)
            return
        if session_id:
            self._session_id = str(session_id).strip()

        self._session_turns.append(json.dumps(self._build_turn_messages(user_content, assistant_content), ensure_ascii=False))
        self._turn_counter = self._turn_index = self._turn_counter + 1
        if remainder := self._turn_counter % self._retain_every_n_turns:
            logger.debug("sync_turn: buffered turn %d (will retain at turn %d)",
                         self._turn_counter, self._turn_counter + (self._retain_every_n_turns - remainder))
            return

        document_id, update_mode = self._resolve_retain_target(self._document_id)
        # Append-capable APIs get only the delta since the last retain; legacy /
        # overwrite APIs need the whole session because each retain replaces the document.
        start = self._last_retained_turn_count if update_mode == "append" else 0
        turns_to_retain = self._session_turns[start:]
        if not turns_to_retain:
            logger.debug("sync_turn: skipped append retain; no new turns since last retain")
            return
        logger.debug("sync_turn: retaining %d/%d turns, payload %d chars",
                     len(turns_to_retain), len(self._session_turns), sum(len(t) for t in turns_to_retain))

        job = self._make_turn_retain_job(turns_to_retain, document_id=document_id,
                                         update_mode=update_mode, label="retain")
        # Indicator fires only past every skip/buffer gate: solely on turns that persist.
        # Model-independent status line; no-op without retain_indicator/status channel.
        if self._retain_indicator and self._status_callback is not None:
            try:
                self._status_callback(f"{_HINDSIGHT_GLYPH} Hindsight — saving to memory…")
            except Exception:
                logger.debug("Retain indicator emit failed (non-fatal)", exc_info=True)
        self._enqueue_retain(job)
        # Advance the watermark only after the delta is queued so a later retain
        # doesn't re-ship turns already handed to the writer.
        if update_mode == "append":
            self._last_retained_turn_count = len(self._session_turns)

    def _enqueue_retain(self, job: Callable[[], None]) -> None:
        """Hand *job* to the (lazily started) writer and arm the atexit drain."""
        self._ensure_writer()
        self._register_atexit()
        self._retain_queue.put(job)

    # -- tools -------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [] if self._memory_mode == "context" else [RETAIN_SCHEMA, RECALL_SCHEMA, REFLECT_SCHEMA]

    def _tool_retain(self, args: dict) -> str:
        content, context = args["content"], args.get("context")
        item = self._build_retain_kwargs(content, context=context, tags=args.get("tags"),
                                         occurred_at=args.get("occurred_at"))
        logger.debug("Tool hindsight_retain: bank=%s, content_len=%d, context=%s",
                     self._bank_id, len(content), context)
        self._retain_batch(item, bank_id=self._bank_id)
        logger.debug("Tool hindsight_retain: success")
        return "Memory stored successfully."

    def _tool_recall(self, args: dict) -> str:
        query = args["query"]
        logger.debug("Tool hindsight_recall: bank=%s, query_len=%d, budget=%s",
                     self._bank_id, len(query), self._budget)
        results = self._recall(query)
        logger.debug("Tool hindsight_recall: %d results", len(results))
        return "\n".join(f"{i}. {r.text}" for i, r in enumerate(results, 1)) or "No relevant memories found."

    def _tool_reflect(self, args: dict) -> str:
        query = args["query"]
        logger.debug("Tool hindsight_reflect: bank=%s, query_len=%d, budget=%s",
                     self._bank_id, len(query), self._budget)
        text = self._reflect(query) or ""
        logger.debug("Tool hindsight_reflect: response_len=%d", len(text))
        return text or "No relevant memories found."

    # tool name -> (required arg, handler, user-facing failure prefix)
    _TOOL_HANDLERS = {
        "hindsight_retain": ("content", _tool_retain, "Failed to store memory"),
        "hindsight_recall": ("query", _tool_recall, "Failed to search memory"),
        "hindsight_reflect": ("query", _tool_reflect, "Failed to reflect"),
    }

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        required, handler, failure = self._TOOL_HANDLERS[tool_name]
        if not args.get(required, ""):
            return tool_error(f"Missing required parameter: {required}")
        try:
            return json.dumps({"result": handler(self, args)})
        except Exception as e:
            logger.warning("%s failed: %s", tool_name, e, exc_info=True)
            return tool_error(f"{failure}: {e}")

    # -- session lifecycle -------------------------------------------------------

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, **kwargs) -> None:
        """Rotate per-session state (/resume, /branch, /reset, /new, compression) so
        writes don't land in the previous session's document. Always: flush buffered
        turns under the OLD ids first (``retain_every_n_turns > 1`` would silently
        lose them), join the in-flight prefetch and drop its result (no stale recall
        for the new session), then set ``_session_id``, mint a fresh ``_document_id``
        and clear the batch buffers. ``reset`` is accepted but unneeded: buffer
        clearing is correct for every switch.

        Without this hook, initialize()-cached state (``_session_id``, ``_document_id``, ``_session_turns``,
        ``_turn_counter``) would keep pointing at the previous session and writes would land in the wrong
        document. See hermes-agent#6672.
        Always update ``_session_id`` so metadata and tags on subsequent retains reflect the active session.
        Always clear the accumulated batch buffers (``_session_turns``, ``_turn_counter``, ``_turn_index``)
        — even for /resume and /branch, the new session's batching must start from zero so an in-flight
        retain doesn't flush under the wrong ``_document_id``. See #1303.
        """
        new_id = str(new_session_id or "").strip()
        if not new_id:
            return

        # 1. Flush buffered turns under the OLD identifiers, resolved BEFORE the
        # rotation (legacy: per-process unique; >=0.5.0: session-scoped + append).
        if self._session_turns:
            old_document_id, old_update_mode = self._resolve_retain_target(self._document_id)
            job = self._make_turn_retain_job(list(self._session_turns), document_id=old_document_id,
                                             update_mode=old_update_mode, label="flush-on-switch",
                                             track_ops=False)

            def _flush():
                try:
                    job()
                except Exception as e:
                    logger.warning("Hindsight flush-on-switch failed: %s", e, exc_info=True)
            # Same writer queue as sync_turn: FIFO behind queued old-session retains,
            # no two threads racing aretain_batch on one document, shutdown drain intact.
            if not self._shutting_down.is_set():
                self._enqueue_retain(_flush)

        # 2. Drain the old session's in-flight prefetch and drop its result.
        self._join_prefetch(3.0)
        with self._prefetch_lock:
            self._prefetch_result = ""

        # 3. Rotate to the new session.
        if parent_session_id:
            self._parent_session_id = str(parent_session_id).strip()
        self._session_id, self._document_id = new_id, _mint_document_id(new_id)
        self._session_turns = []
        self._turn_counter = self._turn_index = self._last_retained_turn_count = 0
        logger.debug("Hindsight on_session_switch: new_session=%s parent=%s reset=%s doc=%s",
                     self._session_id, self._parent_session_id, reset, self._document_id)

    def _close_client(self) -> None:
        if self._mode != "local_embedded":
            self._run_sync(self._client.aclose())
            return
        # HindsightEmbedded.close() closes its sync client from this thread ("attached
        # to a different loop" before aiohttp releases the session): aclose the inner
        # client on the shared loop first, then let the wrapper clean up bookkeeping.
        inner_client = getattr(self._client, "_client", None)
        if inner_client is not None and hasattr(inner_client, "aclose"):
            _run_sync(inner_client.aclose())
            with contextlib.suppress(Exception):
                self._client._client = None
        with contextlib.suppress(RuntimeError):
            self._client.close()

    def shutdown(self) -> None:
        logger.debug("Hindsight shutdown: stopping writer + waiting for background threads")
        # Stop accepting retain jobs first so late sync_turn() calls are dropped.
        self._shutting_down.set()
        # The writer finishes in-flight work then exits on the sentinel; the
        # bounded join keeps shutdown predictable even if the daemon is wedged.
        if (writer := self._writer_thread) is not None and writer.is_alive():
            self._retain_queue.put(_WRITER_SENTINEL)
            writer.join(timeout=10.0)
            if writer.is_alive():
                logger.warning(
                    "Hindsight writer did not stop within 10s; "
                    "abandoning %d pending retain(s)",
                    self._retain_queue.qsize(),
                )
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)
        # Drop the reference first, then close: _close_client() releases the
        # session on the shared owning loop (never from this thread), the same
        # path a retry-displaced client already went through. The
        # read-and-drop is a fenced sweep: _publish_lock serializes it
        # against every client publish (caller-thread setup, owner-loop
        # build, _install_client), each of which re-checks _shutting_down
        # inside the same serialization — so a publish either completes
        # before this sweep (its client is closed below) or refuses and
        # disposes of the client itself through its generation. No client
        # can land in self._client after the sweep and outlive shutdown.
        with self._publish_lock:
            client = self._client
            self._client = None
        if client is not None and not self._is_client_shutdown_settled(client):
            setup = self._claim_client_for_close(client)
            settled = self._reconcile_close_attempt(setup)
            if settled:
                self._clear_abandoned_setup(setup)
            else:
                logger.warning(
                    "Hindsight shutdown: published client could not be "
                    "confirmed released; keeping the generation and its "
                    "tracked client recorded (fail-closed)"
                )
        # An abandoned setup may still track a late client its coroutine
        # FAILED to close. Nobody will join that generation anymore, so
        # shutdown makes a bounded owning-loop reconciliation attempt
        # itself — the same single-slot protocol a join uses, so a join
        # reconciler running concurrently is awaited rather than raced.
        # The generation is cleared ONLY on safe settlement: a close that
        # fails, times out, or stays in flight keeps the exact generation,
        # tracked client, and in-flight attempt recorded — letting the
        # reference die with the provider is precisely the leak this path
        # exists to prevent — and the unresolved fail-closed state is
        # logged. (An abandoned coroutine still mid-build is bounded-waited
        # on the same terms: it closes its own client when it completes.)
        for setup in list(self._iter_abandoned_setups()):
            state = setup.wait_reconcilable(
                timeout=float(self._timeout or _DEFAULT_TIMEOUT)
            )
            settled = state == "settled" or (
                state != "" and self._reconcile_close_attempt(setup)
            )
            if settled:
                self._clear_abandoned_setup(setup)
            else:
                logger.warning(
                    "Hindsight shutdown: an abandoned client setup remains "
                    "unresolved (%s); keeping the generation and its tracked "
                    "client recorded (fail-closed) — the client could not be "
                    "confirmed released",
                    "still building or cleaning up"
                    if state == ""
                    else "close failed or still in flight",
                )
        # An ACTIVE first-client setup (a caller parked mid-build inside
        # _await_client_setup, or in its future-ready-but-uninstalled
        # window) is deliberately NOT waited on here: shutdown must stay
        # bounded while that caller's wait may span the whole install
        # allowance. Both of its sides fence themselves against the sweep
        # above — the setup coroutine closes a client that completes after
        # the flag is set (ON the owning loop, exactly once), and the
        # caller's fenced publish under _publish_lock refuses to install
        # and instead drives the generation's bounded exactly-once release
        # before failing closed (see _reject_client_after_shutdown). The
        # owner-loop first-build path fences itself the same way (see
        # _get_client_on_owning_loop).
        # The module-global background event loop (_loop / _loop_thread)
        # is intentionally NOT stopped here. It is shared across every
        # HindsightMemoryProvider instance in the process — the plugin
        # loader creates a new provider per AIAgent, and the gateway
        # creates one AIAgent per concurrent chat session. Stopping the
        # loop from one provider's shutdown() strands the aiohttp
        # ClientSession + TCPConnector owned by every sibling provider
        # on a dead loop, which surfaces as the "Unclosed client session"
        # / "Unclosed connector" warnings reported in #11923. The loop
        # runs on a daemon thread and is reclaimed on process exit;
        # per-session cleanup happens via self._client.aclose() above.


# The module-global background event loop (_loop / _loop_thread) is intentionally NOT stopped here. It is
# shared across every HindsightMemoryProvider instance in the process — the plugin loader creates a new
# provider per AIAgent, and the gateway creates one AIAgent per concurrent chat session. Stopping the loop
# from one provider's shutdown() strands the aiohttp ClientSession + TCPConnector owned by every sibling
# provider on a dead loop, which surfaces as the "Unclosed client session" / "Unclosed connector" warnings
# reported in #11923. The loop runs on a daemon thread and is reclaimed on process exit; per-session cleanup
# happens via self._client.aclose() above.
def register(ctx) -> None:
    """Register Hindsight as a memory provider plugin."""
    ctx.register_memory_provider(HindsightMemoryProvider())


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from dataclasses import dataclass  # noqa: F401,E402
import importlib  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
