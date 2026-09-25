"""Transparent recovery for sessions forcibly interrupted mid-tool-call.

A session whose turn dies to a forced interruption — drain-timeout restart
or shutdown (``restart_timeout`` / ``shutdown_timeout``), a crash
(``restart_interrupted``), or any legacy/generic marker — used to resume
with an LLM-visible ``[System note: The previous turn was interrupted by
a gateway restart ...]`` wrapper.  The model then narrated the outage
("the advisory call died") instead of simply finishing the turn.  Only
sessions that accepted ``COOPERATIVE_RESTART_STEER`` should ever see
restart guidance; a genuinely interrupted victim resumes as if the
process had stayed alive.

This module owns the pieces of that transparent resume:

* :func:`is_forced_interruption_reason` — recovery classification.
  Cooperative restarts keep the existing safe-pause guidance; every other
  reason recovers below the LLM boundary.
* :func:`build_victim_replay_plan` — locate the FINAL interrupted
  assistant tool batch in the raw transcript and split its calls into
  completed (leave untouched), replayable (re-run through the normal
  dispatcher), and fail-closed (the lifecycle request that caused the
  bounce, plus anything that cannot pair unambiguously).  There is NO
  tool-name safety whitelist: a forced victim's unresolved calls are
  re-run literally, whatever tool they target — at-least-once execution
  is the accepted cost of the replay contract, and only the result
  persistence is required to be exactly-once.
* :func:`execute_victim_replay` — re-run the replayable calls through
  ``agent._execute_tool_calls`` (the normal Hermes tool dispatcher, with
  its ordinary authorization/hooks/schema/approval/budget middleware),
  persist each replacement result exactly once through the SYNCHRONOUS
  CHECKED append (commit AND read back — a persistence failure FAILS the
  recovery: no repaired history, no model continuation), close
  fail-closed calls with the existing UNKNOWN effect-disposition rows so
  the batch pairs for strict providers, and return the repaired
  transcript rows.  Executions are reserved ahead through
  :class:`ReplayExecutionLedger` — ONE atomic durable state transition
  per call in the session SQLite database — so a recovery whose result
  never became durable is never executed twice, across workers.
* :func:`plan_forced_resume_turn` — decide how the recovered turn enters
  the model: a REAL user message runs as a normal new turn after the
  completed batch, and the synthesized auto-resume event (no user text)
  continues the interrupted turn through the ordinary
  ``continue_interrupted_turn`` seam — no note, no synthetic user row,
  nothing for the model to notice.
* :func:`trim_incomplete_assistant_text_tail` — the text-only policy: a
  turn interrupted mid-text resumes from the original legal boundary by
  excluding the incomplete assistant tail, never by appending a second
  assistant row (or a fake user/system row) after it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from agent.message_sanitization import (
    tool_call_id_variants,
    tool_result_id_variants,
)
from agent.replay_cleanup import is_interrupted_tool_result
from agent.tool_dispatch_helpers import make_tool_result_message
from agent.tool_result_classification import tool_may_have_side_effect
from gateway.restart_wind_down import is_cooperative_restart_reason

logger = logging.getLogger(__name__)

# The agent-callable gateway lifecycle request (plugins/gateway_restart).
# Re-running it from inside the recovery it caused would bounce the gateway
# again — the self-restart loop this module must never create.
GATEWAY_LIFECYCLE_TOOL_NAMES = frozenset({"restart"})


def is_forced_interruption_reason(reason: Optional[str]) -> bool:
    """True for every recovery reason that is NOT a cooperative park.

    ``cooperative_restart`` sessions asked to wind down and expect the
    safe-pause guidance; ``restart_timeout`` / ``shutdown_timeout`` /
    ``restart_interrupted`` / anything else (including None and legacy
    markers) are victims of a bounce they did not accept.
    """
    return not is_cooperative_restart_reason(reason)


# ──────────────────────────────────────────────────────────────────────
# Lifecycle-request classification (the only calls never replayed)
# ──────────────────────────────────────────────────────────────────────


def _arguments_dict(arguments: Any) -> Optional[dict]:
    """Best-effort parse of a tool call's arguments into a dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def resolve_replay_call_target(name: str, arguments: Any) -> Tuple[str, Any]:
    """Resolve a deferred ``tool_call`` bridge wrapper to its underlying call.

    Returns ``(name, arguments)`` unchanged for every non-bridge call and
    for a wrapper that does not parse — classification then judges the
    wrapper as-is instead of guessing.  Deliberately NOT
    ``tools.tool_search.resolve_underlying_call``: that helper enforces the
    config-curated deferrable set, and lifecycle classification must not
    depend on which tools defer-eligibility currently allows — a wrapper
    naming ``restart`` is a lifecycle request whatever the defer config
    says.  This is classification only; the dispatcher still executes the
    original wrapper call as-is.
    """
    if name != "tool_call":
        return name, arguments
    raw = _arguments_dict(arguments) or {}
    underlying = raw.get("name")
    if not isinstance(underlying, str) or not underlying.strip():
        return name, arguments
    underlying_args: Any = raw.get("arguments")
    if isinstance(underlying_args, str):
        underlying_args = _arguments_dict(underlying_args)
    return underlying.strip(), underlying_args if underlying_args is not None else {}


def is_lifecycle_replay_request(name: str, arguments: Any) -> bool:
    """True when re-running this exact call could re-trigger the bounce.

    Narrow by construction — this is the ONLY reason a forced victim's
    call is not replayed:

    * the gateway ``restart`` tool (the agent-side lifecycle request), or
      a deferred bridge wrapping it;
    * a shell command targeting the gateway's own lifecycle
      (``hermes gateway restart``, ``systemctl restart hermes-gateway``,
      ``launchctl``/``pkill`` variants) — detected with the same
      canonical classifier the terminal tool's hard block uses
      (``cron.lifecycle_guard``), so the two can never drift apart.

    Everything else — terminal, file writers, unknown/MCP tools — is an
    ordinary victim call and replays: the contract chooses at-least-once
    execution over UNKNOWN, and only the restart requester's own command
    is excluded, never side-effecting calls in general.

    FAIL CLOSED: a command-bearing call whose classifier is unavailable or
    errors is treated as a lifecycle request.  A mis-classified ordinary
    command costs one fail-closed UNKNOWN row; a mis-classified lifecycle
    command costs another gateway bounce inside the recovery that is
    trying to fix the last one.  Calls without a command have nothing to
    classify and stay replayable.
    """
    resolved_name, resolved_args = resolve_replay_call_target(name, arguments)
    if resolved_name in GATEWAY_LIFECYCLE_TOOL_NAMES:
        return True
    command = None
    args = _arguments_dict(resolved_args)
    if args is not None and isinstance(args.get("command"), str):
        command = args["command"]
    if not command:
        return False
    try:
        from cron.lifecycle_guard import contains_gateway_lifecycle_command

        return bool(contains_gateway_lifecycle_command(command))
    except Exception:
        logger.debug(
            "lifecycle guard unavailable during replay classification; "
            "failing closed for command-bearing call %r",
            command[:120],
        )
        return True


# ──────────────────────────────────────────────────────────────────────
# Replay plan
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ReplayCall:
    """One unresolved or explicitly interrupted call from the final batch."""

    call_id: str
    name: str
    arguments: Any
    kind: str  # "unresolved" (no tool row) | "interrupted" (interrupt marker)

    def as_message_tool_call(self) -> Any:
        """Adapter shaped like the provider tool-call objects the dispatcher
        iterates (``.id`` / ``.function.name`` / ``.function.arguments``)."""
        return SimpleNamespace(
            id=self.call_id,
            function=SimpleNamespace(name=self.name, arguments=self.arguments),
        )


@dataclass
class VictimReplayPlan:
    """Classification of the final interrupted assistant tool batch."""

    batch_present: bool = False
    replay_calls: List[ReplayCall] = field(default_factory=list)
    # Calls that must NOT re-run: the lifecycle request that caused the
    # bounce, plus calls that cannot pair unambiguously.  These keep the
    # existing fail-closed treatment — UNKNOWN orphan results from
    # agent.replay_cleanup / this module — instead of fabricated success.
    fail_closed_calls: List[ReplayCall] = field(default_factory=list)
    # Malformed batch IDENTITY: duplicate call ids, or a call with a
    # missing/whitespace-only id.  No unambiguous pairing key exists for
    # the batch, so nothing about it may be replayed, closed, or continued
    # — block below the provider, add no fabricated/duplicate rows, and
    # keep recovery pending.  (Distinct from fail_closed_calls, which
    # still pairs and closes with UNKNOWN rows.)
    identity_malformed: bool = False
    lifecycle_call_ids: set = field(default_factory=set)
    interrupted_call_ids: set = field(default_factory=set)
    completed_call_ids: set = field(default_factory=set)

    @property
    def has_replay_work(self) -> bool:
        return bool(self.replay_calls)


def _call_id_of_tool_call(call: Any) -> str:
    """Exact pairing id of an assistant tool_calls entry (dict or object).

    Byte-for-byte VERBATIM: provider-native ids — including composite
    bridge ids like ``call_alpha|item_beta`` and any non-empty
    leading/trailing bytes — must survive classification, dispatch,
    pairing, persistence, and provider history unchanged.  Trimming here
    would rewrite ``" call_alpha|item_beta "`` into an id the provider
    never sent, and normalizing to the call-id half would dispatch the
    wrong id and break strict providers' assistant→tool pairing.
    Malformed ids (missing or whitespace-only) are left exactly as they
    are: the plan fails those calls closed rather than silently
    rewriting them into something replayable.
    """
    if isinstance(call, dict):
        raw = call.get("call_id") or call.get("id")
    else:
        raw = getattr(call, "call_id", None) or getattr(call, "id", None)
    if not isinstance(raw, str):
        return ""
    return raw


def _tool_call_name(call: Any) -> str:
    function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(getattr(function, "name", "") or "")


def _tool_call_arguments(call: Any) -> Any:
    function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
    if isinstance(function, dict):
        return function.get("arguments")
    return getattr(function, "arguments", None)


def build_victim_replay_plan(raw_history: Any) -> VictimReplayPlan:
    """Classify the FINAL interrupted assistant tool batch of a transcript.

    Operates on the RAW transcript rows (pre-sanitizer): the replay-tail
    strippers in :mod:`agent.replay_cleanup` erase dangling read-only tails
    and rewrite interrupted results, so the plan must be captured before
    they run.

    Only the trailing batch is considered — an interrupted batch buried
    under a later user message is not "the final interrupted batch" and
    keeps the existing stripping treatment (mid-turn steering/queue
    semantics unchanged).
    """
    plan = VictimReplayPlan()
    if not isinstance(raw_history, list) or not raw_history:
        return plan

    # Walk back over the contiguous trailing tool rows to their issuing
    # assistant message.
    idx = len(raw_history) - 1
    while idx >= 0 and isinstance(raw_history[idx], dict) and raw_history[idx].get("role") == "tool":
        idx -= 1
    if idx < 0 or not isinstance(raw_history[idx], dict):
        return plan
    batch_msg = raw_history[idx]
    if batch_msg.get("role") != "assistant" or not batch_msg.get("tool_calls"):
        # Interruption landed before/after any tool batch — nothing to replay;
        # the original turn simply continues from its own last row.
        return plan
    plan.batch_present = True

    tool_calls = list(batch_msg.get("tool_calls") or [])
    call_ids = [_call_id_of_tool_call(call) for call in tool_calls]
    if len(call_ids) != len(set(call_ids)):
        # Providers occasionally reuse one id across a batch (#58327) — a
        # replayed result cannot pair unambiguously, and persisting one
        # fabricated row per duplicate id would itself violate exact-ID
        # cardinality and strict provider pairing.  The whole batch is
        # malformed identity: block below the provider with no rows added.
        logger.debug("Victim replay: duplicate call ids in final batch; failing closed")
        plan.identity_malformed = True
        plan.fail_closed_calls = [
            ReplayCall(cid, _tool_call_name(call), _tool_call_arguments(call), "unresolved")
            for call, cid in zip(tool_calls, call_ids)
        ]
        return plan

    # Variant-aware answer matching: a durable result row may be stored
    # under ANY alias of a composite id (the live dispatcher normalizes
    # ``call_alpha|item_beta`` → ``call_alpha`` when it builds result
    # rows), and a normalized row DOES answer the composite call —
    # replaying it would re-run a completed side effect.  Conversely, a
    # row aliasing MORE than one call of this batch cannot pair
    # unambiguously, so the whole batch fails closed instead of guessing
    # which call the row answers.
    call_variant_sets = [tool_call_id_variants(call) for call in tool_calls]
    answered: Dict[str, Dict[str, Any]] = {}
    interrupted: Dict[str, Dict[str, Any]] = {}
    for row in raw_history[idx + 1:]:
        if not isinstance(row, dict):
            continue
        row_variants = tool_result_id_variants(row.get("tool_call_id"))
        if not row_variants:
            continue
        matches = [
            i
            for i, variants in enumerate(call_variant_sets)
            if variants & row_variants
        ]
        if not matches:
            continue
        if len(matches) > 1:
            logger.debug(
                "Victim replay: result row %r aliases %d calls of the final "
                "batch; failing the batch closed",
                row.get("tool_call_id"),
                len(matches),
            )
            plan.fail_closed_calls = [
                ReplayCall(cid, _tool_call_name(call), _tool_call_arguments(call), "unresolved")
                for call, cid in zip(tool_calls, call_ids)
            ]
            return plan
        cid = call_ids[matches[0]]
        if is_interrupted_tool_result(row.get("content", "")):
            interrupted.setdefault(cid, row)
        else:
            # A later non-interrupted row (e.g. a previous replay's result)
            # means the call already completed — never replay it.
            answered.setdefault(cid, row)

    for call, cid in zip(tool_calls, call_ids):
        name = _tool_call_name(call)
        arguments = _tool_call_arguments(call)
        if not cid or not cid.strip():
            # Missing or whitespace-only id: no unambiguous pairing key
            # exists, so the batch's identity is malformed — block the
            # whole batch below the provider (no fabricated rows) instead
            # of silently rewriting the id.
            plan.identity_malformed = True
            plan.fail_closed_calls.append(ReplayCall(cid, name, arguments, "unresolved"))
            continue
        if cid in answered:
            plan.completed_call_ids.add(cid)
            continue
        kind = "interrupted" if cid in interrupted else "unresolved"
        replay_call = ReplayCall(cid, name, arguments, kind)
        if is_lifecycle_replay_request(name, arguments):
            # The restart requester's own command is not an interrupted
            # victim; replaying it would bounce the gateway again.
            plan.fail_closed_calls.append(replay_call)
            plan.lifecycle_call_ids.add(cid)
            continue
        plan.replay_calls.append(replay_call)
        if kind == "interrupted":
            plan.interrupted_call_ids.add(cid)
    return plan


# ──────────────────────────────────────────────────────────────────────
# Durable replay-execution reservation
# ──────────────────────────────────────────────────────────────────────
#
# A reservation exists ONLY in the window between "the call executed" and
# "its replacement result is durably persisted": on verified persistence
# the reservation is released, so a provider reusing an id in a later
# turn/batch is never fenced by a stale record.  What remains is exactly
# the ambiguous window — execution happened but the transcript never got
# the row (append failure, crash mid-recovery) — where a second recovery
# MUST NOT re-run the side effect.
#
# The substrate is the session's own SQLite database: ONE BEGIN IMMEDIATE
# transaction per reservation (``SessionDB.reserve_replay_execution``), so
# two independent workers — separate gateway processes, or two ledger
# instances in one process — cannot both reserve the same call.  There is
# deliberately no JSON ledger file to corrupt: either the reservation
# reads back from SQLite or it FAILS CLOSED.  Non-SessionStore test
# doubles fall back to a process-global in-memory map (same fencing
# within one process; never used in production, where the store is always
# a real gateway SessionStore).


def _reservation_key(session_id: str, call: "ReplayCall") -> str:
    """Stable reservation key: session + exact call identity + argument hash.

    Keyed by more than the bare call id so a later legitimate provider id
    reuse (#58327) is a DISTINCT reservation, while a retry of the SAME
    call — same exact id, same arguments — collides and is fenced.
    """
    args = call.arguments
    if isinstance(args, str):
        canonical_args = args
    else:
        try:
            canonical_args = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            canonical_args = repr(args)
    payload = "\x00".join((session_id, call.call_id, call.name, canonical_args))
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
    return f"freplay:{digest}"


_FALLBACK_RESERVATION_LOCK = threading.Lock()
_FALLBACK_RESERVATIONS: Dict[str, str] = {}
_FALLBACK_RESERVATION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


class ReplayExecutionLedger:
    """Write-ahead reservation of replay executions.

    ``reserve_execution`` is taken BEFORE dispatch — one atomic durable
    state transition — and ``release_execution`` AFTER the replacement
    result is verified durable.  Cross-worker atomic by construction:
    SQLite's write lock serializes the reserve, so of two workers racing
    the same call exactly one receives ``"claimed"``.

    Fail-closed semantics:

    * no session store / no resolvable database / any store error → the
      reservation FAILS and the call does not execute;
    * a held reservation (prior execution whose result never became
      durable) → the call does not execute again;
    * a release failure after durable persistence is BLOCKING — reported
      as a failed repair rather than ignored, because a stale reservation
      would fence a future legitimate reuse of the same call identity.

    A crash between the external effect and any durable completion record
    remains at-least-once by contract; this ledger fences
    concurrent/repeated recovery without a crash, which CAN be exact.
    """

    def __init__(self, session_store: Any = None, session_id: Optional[str] = None):
        self._session_store = session_store
        self._session_id = session_id

    def _session_db(self) -> Any:
        """The reservation substrate: a SessionDB, ``"fallback"``, or None.

        Only a REAL gateway SessionStore participates in durable
        reservations — its sessions live in SQLite with cross-process
        write locking.  Anything else (test doubles) gets the
        process-global in-memory fence.  A real store whose database
        cannot be resolved returns None: fail closed, never guess.
        """
        if self._session_store is None:
            return None
        try:
            from gateway.session import SessionStore
        except Exception:
            return None
        if not isinstance(self._session_store, SessionStore):
            return "fallback"
        try:
            db = self._session_store._db_for_session_id(self._session_id)
        except Exception:
            logger.warning(
                "Victim replay: session DB lookup failed for %s",
                self._session_id,
                exc_info=True,
            )
            return None
        return db

    def reserve_execution(self, call: "ReplayCall") -> Tuple[bool, Optional[str]]:
        """Atomically reserve one execution; ``(True, None)`` when claimed."""
        if not self._session_id:
            return False, "no session id for replay reservation"
        key = _reservation_key(self._session_id, call)
        value = json.dumps(
            {
                "ts": time.time(),
                "session_id": self._session_id,
                "call_id": call.call_id,
                "name": call.name,
            }
        )
        substrate = self._session_db()
        if substrate == "fallback":
            now = time.time()
            with _FALLBACK_RESERVATION_LOCK:
                for stale_key, stale_value in list(_FALLBACK_RESERVATIONS.items()):
                    try:
                        stale_ts = float(json.loads(stale_value).get("ts", 0.0))
                    except (TypeError, ValueError, AttributeError):
                        stale_ts = 0.0
                    if now - stale_ts > _FALLBACK_RESERVATION_MAX_AGE_SECONDS:
                        _FALLBACK_RESERVATIONS.pop(stale_key, None)
                if key in _FALLBACK_RESERVATIONS:
                    return False, "already executed (in-process fence)"
                _FALLBACK_RESERVATIONS[key] = value
            return True, None
        if substrate is None:
            return False, "no durable reservation store"
        try:
            state, existing = substrate.reserve_replay_execution(key, value)
        except Exception:
            logger.warning(
                "Victim replay: reservation store failed for %s",
                call.call_id,
                exc_info=True,
            )
            return False, "reservation store error"
        if state == "claimed":
            return True, None
        return False, f"already executed (reservation held: {existing!r})"

    def release_execution(self, call: "ReplayCall") -> bool:
        """Release the reservation once the result row is verified durable.

        False is BLOCKING: the caller must fail the repair instead of
        leaving a fence that will outlive this recovery.
        """
        if not self._session_id:
            return False
        key = _reservation_key(self._session_id, call)
        substrate = self._session_db()
        if substrate == "fallback":
            with _FALLBACK_RESERVATION_LOCK:
                _FALLBACK_RESERVATIONS.pop(key, None)
            return True
        if substrate is None:
            return False
        try:
            substrate.release_replay_reservation(key)
        except Exception:
            logger.warning(
                "Victim replay: reservation release failed for %s",
                call.call_id,
                exc_info=True,
            )
            return False
        return True


def claim_forced_recovery_ownership(
    session_store: Any,
    session_id: Optional[str],
    raw_history: Any,
) -> str:
    """Durably claim ownership of ONE forced recovery, keyed to the exact
    transcript tail.

    Returns ``"claimed"`` | ``"already_claimed"`` | ``"superseded"`` |
    ``"unavailable"``.  This is the cross-worker fence for the recovery
    itself (tail trim + replay + continuation): the claim is one SessionDB
    transaction that compare-and-swaps the session's tail digest, so of
    two workers that loaded the same tail only one proceeds — the loser
    sees ``"already_claimed"`` (a fresh claim for this same tail exists)
    or ``"superseded"`` (the durable tail no longer matches what it
    loaded) and must stand down / reload BEFORE any provider execution,
    never racing a duplicate trim-and-continue onto the transcript.

    Non-SessionStore test doubles have no shared substrate: they get
    ``"claimed"`` (single-process semantics).
    """
    if not session_id:
        return "claimed"
    try:
        from gateway.session import SessionStore
    except Exception:
        return "claimed"
    if not isinstance(session_store, SessionStore):
        return "claimed"
    try:
        db = session_store._db_for_session_id(session_id)
        if db is None:
            # No durable substrate for this session: nothing to fence on.
            # Recovery cannot prove durability here either, so persistence
            # will fail closed at its own seam.
            return "claimed"
        from hermes_state import forced_recovery_tail_digest

        tail = None
        if isinstance(raw_history, list) and raw_history:
            candidate = raw_history[-1]
            if isinstance(candidate, dict):
                tail = candidate
        outcome = db.claim_forced_recovery_tail(
            session_id, forced_recovery_tail_digest(tail)
        )
        return outcome if isinstance(outcome, str) else "unavailable"
    except Exception:
        logger.warning(
            "Forced recovery ownership claim failed for %s",
            session_id,
            exc_info=True,
        )
        return "unavailable"


# ──────────────────────────────────────────────────────────────────────
# Replay execution
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VictimReplayOutcome:
    """Result of a transparent replay attempt.

    ``repaired_history`` is None when nothing was replayed: the caller
    keeps the already-built history and the existing fail-closed strippers
    own the tail.  ``failure`` names the reason on a failed attempt
    (partial dispatcher output, persistence failure, reservation
    conflict) — a failure is BLOCKING: the caller must not call the
    model, must not clear ``resume_pending``, and must not emit a
    synthetic answer on top of it.  ``ready_for_continuation`` is the
    typed gate for exactly that decision.
    """

    repaired_history: Optional[List[Dict[str, Any]]] = None
    replayed_call_ids: List[str] = field(default_factory=list)
    failure: Optional[str] = None

    @property
    def ready_for_continuation(self) -> bool:
        """True only when a durable repaired history exists for the batch.

        False both for a no-op (no replay work — the normal turn path
        owns the tail) and for a failure (blocked: retry/recover later).
        ``repaired_history is not None`` remains the "something was
        repaired" signal on its own.
        """
        return self.repaired_history is not None and self.failure is None


def build_replay_assistant_message(plan: VictimReplayPlan) -> Any:
    """Synthetic assistant message carrying ONLY the replayable calls.

    Shaped for ``agent._execute_tool_calls`` — the normal Hermes tool
    dispatcher — so the re-run goes through the identical middleware,
    narration, budgeting, and incremental-persistence path as a live call.
    """
    return SimpleNamespace(
        tool_calls=[call.as_message_tool_call() for call in plan.replay_calls]
    )


def orphan_recovery_row(call: ReplayCall) -> Dict[str, Any]:
    """Existing fail-closed treatment for a call that must not re-run.

    Mirrors ``agent.replay_cleanup.strip_dangling_tool_call_tail``: a
    side-effecting call whose outcome cannot be proven reports UNKNOWN, a
    provably effect-free one reports no effect.  Never a fabricated
    success.  The row carries the EXACT original call id (the constructor
    normalizes composite bridge ids; re-stamp so strict pairing holds).
    """
    disposition = "unknown" if tool_may_have_side_effect(call.name) else "none"
    content = (
        "[Orphan recovery: this tool may have executed before Hermes stopped; "
        "its effect is UNKNOWN. Inspect current state before retrying.]"
        if disposition == "unknown"
        else "[Orphan recovery: this read-only tool did not complete and had no effect.]"
    )
    row = make_tool_result_message(
        call.name, content, call.call_id, effect_disposition=disposition
    )
    row["tool_call_id"] = call.call_id
    row["_orphan_recovery"] = True
    return row


def splice_replayed_results(
    raw_history: List[Dict[str, Any]],
    plan: VictimReplayPlan,
    fresh_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return repaired transcript rows with fresh results in place.

    Reconciles EVERY row for each exact call id — a batch can carry
    duplicate stale interrupted markers (interrupt, replay, another
    interrupt), and replacing only the first leaves fresh + stale behind,
    letting the stale marker win downstream.  Rules:

    * completed (non-interrupted) rows are preserved verbatim — never
      touched, never replayed;
    * every stale interrupted marker for an id that receives a
      replacement (fresh result or fail-closed orphan row) is REMOVED, so
      exactly one final result row remains per call id;
    * replacements append after the preserved rows in the model's original
      call order, keeping the batch pairable for strict providers.
    """
    repaired = list(raw_history)
    fresh_by_id = {
        str(row.get("tool_call_id") or ""): row
        for row in fresh_rows
        if isinstance(row, dict) and row.get("tool_call_id")
    }

    def _is_stale_marker(row: Any) -> bool:
        return (
            isinstance(row, dict)
            and row.get("role") == "tool"
            and is_interrupted_tool_result(row.get("content", ""))
        )

    # Ids that already carry a real, completed result — never superseded.
    answered_ids = {
        str(row.get("tool_call_id") or "")
        for row in repaired
        if isinstance(row, dict)
        and row.get("role") == "tool"
        and not is_interrupted_tool_result(row.get("content", ""))
    }
    # Fail-closed calls of a batch being repaired get their UNKNOWN
    # orphan-recovery row so every call pairs exactly once — a
    # half-answered batch is a protocol violation strict providers reject.
    orphan_by_id: Dict[str, Dict[str, Any]] = {}
    for call in plan.fail_closed_calls:
        if (
            call.call_id
            and call.call_id not in answered_ids
            and call.call_id not in fresh_by_id
        ):
            orphan_by_id[call.call_id] = orphan_recovery_row(call)

    superseded_ids = set(fresh_by_id) | set(orphan_by_id)
    if superseded_ids:
        # Drop EVERY stale interrupted marker for superseded ids — not
        # just the first occurrence.
        repaired = [
            row
            for row in repaired
            if not (
                _is_stale_marker(row)
                and str(row.get("tool_call_id") or "") in superseded_ids
            )
        ]
    # Append the replacements in the model's original call order: fresh
    # results first, then any fail-closed orphan rows.
    for call in plan.replay_calls:
        fresh = fresh_by_id.get(call.call_id)
        if fresh is not None:
            repaired.append(fresh)
    for call in plan.fail_closed_calls:
        orphan = orphan_by_id.get(call.call_id)
        if orphan is not None:
            repaired.append(orphan)
    return repaired


def _persist_replayed_rows(
    fresh_rows: List[Dict[str, Any]],
    *,
    session_store: Any,
    session_id: Optional[str],
    superseded_call_ids: List[str],
) -> List[Dict[str, Any]]:
    """Durably land the replacement results; return rows that failed.

    Preferred path — ONE durable transaction per batch
    (``SessionStore.replace_transcript_tool_results`` →
    ``SessionDB.supersede_tool_results``): soft-archive every active tool
    row for ``superseded_call_ids`` (the stale interrupted markers this
    repair replaces) and insert the not-yet-durable replacement rows in
    the SAME commit, then read back and verify the canonical outcome —
    exactly ONE active tool row per candidate id, carrying the candidate's
    content.  Archiving and inserting separately would leave a window
    where the durable transcript holds BOTH the stale marker and the
    fresh result for one exact call id; one transaction closes it.

    Stores without the transactional primitive (test doubles) fall back
    to the SYNCHRONOUS CHECKED append
    (``SessionStore.append_to_transcript_checked``): commit AND read back
    the exact canonical row before believing success.  A queue
    acknowledgement or a silent return is not durability — a missing
    store, an unavailable database, or a read-back mismatch all count as
    FAILED, so the recovery can never report a repair the transcript does
    not hold.

    Rows the dispatcher already flushed durably carry the intrinsic
    ``_db_persisted`` marker and are not inserted again — but they still
    count as candidates the read-back must verify, and their ids still
    get their stale markers archived.
    """
    failed: List[Dict[str, Any]] = []
    pending = [row for row in fresh_rows if not row.get("_db_persisted")]
    ids = [str(i) for i in superseded_call_ids if i]
    if not pending and not ids:
        return failed
    # Sentinel for an archive-only failure (every row already durable but
    # the stale markers could not be superseded): the caller only reads
    # ids from failed rows, so a minimal row carrying the first id is
    # enough to make the repair blocking.
    archive_failed = (
        [{"tool_call_id": ids[0], "_archive_failed": True}] if ids else []
    )
    if session_store is None or not session_id:
        # No durable store reachable: nothing can be committed or
        # archived, so every non-durable row counts as failed rather than
        # silently missing.
        return list(pending) or archive_failed
    replace_results = getattr(session_store, "replace_transcript_tool_results", None)
    if callable(replace_results):
        try:
            durable = replace_results(
                session_id, [dict(row) for row in pending], list(ids)
            )
        except Exception:
            durable = False
        if durable is True:
            return failed
        logger.warning(
            "Victim replay: transactional supersede of replacement tool "
            "results did not verify durable (ids: %s)",
            ", ".join(sorted(set(ids))) or "<none>",
        )
        return list(pending) or archive_failed
    append_checked = getattr(session_store, "append_to_transcript_checked", None)
    if not callable(append_checked):
        # No checked primitive on this store: success cannot be PROVEN,
        # which is failure for a recovery whose contract is exactly-once
        # result persistence.
        logger.warning(
            "Victim replay: session store has no checked append; cannot "
            "prove durability of replacement tool results"
        )
        return list(pending) or archive_failed
    for row in pending:
        try:
            durable = append_checked(session_id, dict(row))
        except Exception:
            durable = False
        if durable is not True:
            logger.warning(
                "Victim replay: replacement tool result %s did not verify "
                "durable",
                row.get("tool_call_id"),
            )
            failed.append(row)
    return failed


def _restamp_fresh_rows(
    dispatch_calls: List[ReplayCall],
    tool_rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Pair dispatcher output rows back to their calls by EXACT call id.

    The dispatcher builds result rows through ``make_tool_result_message``
    fed with ``coalesce_tool_call_id``'s canonical id — composite bridge
    ids are normalized to the call-id half (``call_alpha|item_beta`` →
    ``call_alpha``) and surrounding whitespace is stripped — while the
    replay contract carries the EXACT original bytes.  Matching therefore
    runs in two passes:

    * PASS 1 claims byte-for-byte exact ids first, so an aliased call can
      never steal the row of the call whose id it carries (two rows for
      one exact id is duplicate output — a failed recovery);
    * PASS 2 serves the still-unmatched calls from the canonical ALIAS
      variants of their verbatim id (``tool_result_id_variants``): a
      candidate row may serve a call only when it is the exact id of no
      other call of the batch and no other unmatched call also claims it
      — an alias contested by two calls is ambiguous and fails closed.

    Re-stamped rows lose the ``_db_persisted`` marker when the id changed:
    any durable write happened under the other id, so the exact-id row
    still needs one canonical append.

    Returns the ordered fresh rows (one per call) or a failure reason.
    """
    unclaimed = [row for row in tool_rows]
    chosen_by_index: List[Optional[Dict[str, Any]]] = [None] * len(dispatch_calls)

    # PASS 1 — exact byte-for-byte ids.
    for index, call in enumerate(dispatch_calls):
        exact = [
            row
            for row in unclaimed
            if str(row.get("tool_call_id") or "") == call.call_id
        ]
        if len(exact) > 1:
            return [], (
                f"dispatcher produced duplicate results for call {call.call_id!r}"
            )
        if len(exact) == 1:
            chosen_by_index[index] = exact[0]
            unclaimed.remove(exact[0])

    # PASS 2 — canonical alias variants for the still-unmatched calls.
    exact_ids = {call.call_id for call in dispatch_calls}
    for index, call in enumerate(dispatch_calls):
        if chosen_by_index[index] is not None:
            continue
        variants = tool_result_id_variants(call.call_id)
        other_variant_sets = [
            tool_result_id_variants(other.call_id)
            for other_index, other in enumerate(dispatch_calls)
            if other_index != index and chosen_by_index[other_index] is None
        ]
        candidates = [
            row
            for row in unclaimed
            if str(row.get("tool_call_id") or "") in variants
            # A row that is another call's EXACT id belongs to that call.
            and str(row.get("tool_call_id") or "") not in (
                exact_ids - {call.call_id}
            )
            # …and a row claimed by another still-unmatched call's
            # variants is contested — never guess which call it answers.
            and not any(
                str(row.get("tool_call_id") or "") in others
                for others in other_variant_sets
            )
        ]
        if len(candidates) != 1:
            return [], (
                f"dispatcher produced no unambiguous result for call {call.call_id!r}"
            )
        row = candidates[0]
        if str(row.get("tool_call_id") or "") == call.call_id:
            chosen_by_index[index] = row
        else:
            restamped = dict(row)
            restamped["tool_call_id"] = call.call_id
            restamped.pop("_db_persisted", None)
            chosen_by_index[index] = restamped
        unclaimed.remove(row)

    if unclaimed:
        return [], (
            "dispatcher produced unexpected tool rows: "
            + ", ".join(sorted(str(r.get("tool_call_id") or "") for r in unclaimed))
        )
    ordered = [row for row in chosen_by_index if row is not None]
    if len(ordered) != len(dispatch_calls):
        return [], "dispatcher output could not be paired to the replay calls"
    return ordered, None


def execute_victim_replay(
    agent: Any,
    plan: VictimReplayPlan,
    *,
    raw_history: List[Dict[str, Any]],
    session_store: Any = None,
    session_id: Optional[str] = None,
    effective_task_id: str = "",
    ledger: Optional[ReplayExecutionLedger] = None,
) -> VictimReplayOutcome:
    """Re-run the plan's replayable calls through the normal dispatcher.

    Fail-closed by construction: an empty plan, any dispatcher failure,
    partial dispatcher output, or any persistence failure returns an
    outcome with ``failure`` set and no repaired history — a BLOCKING
    outcome: the caller must not call the model, clear
    ``resume_pending``, or emit a synthetic answer on top of it.  The
    existing effect-disposition semantics (UNKNOWN orphan results /
    stripped read-only tails) then own the tail instead of fabricated
    success.

    Two conditions block BELOW the provider with ZERO rows added, because
    nothing about the batch can be proven safe to continue:

    * malformed batch identity (``plan.identity_malformed``) — duplicate
      or missing/whitespace-only call ids leave no unambiguous pairing
      key, so no fabricated or duplicate row may land; and
    * a reservation conflict — another worker owns an unresolved
      execution of this exact call identity.  The loser stands down
      without provider invocation, without UNKNOWN rows, and without
      clearing recovery; it may continue only after the winner's durable
      closure is proven (the caller's reload check).

    Every execution is reserved ahead through ONE atomic durable state
    transition (:class:`ReplayExecutionLedger`) keyed by session + exact
    call identity + argument hash, so concurrent workers cannot both
    execute the same side effect.

    A fail-closed-only batch with well-formed identity (e.g. the
    lifecycle request that caused the bounce) still CLOSES: one ordinary
    UNKNOWN/error tool result per call, durably persisted — never
    executed — so the reconstructed transcript pairs instead of leaking
    an unanswered batch that strict providers turn into ``user,
    assistant, assistant``.
    """
    if ledger is None:
        ledger = ReplayExecutionLedger(session_store, session_id)

    if plan.identity_malformed:
        # No unambiguous pairing key exists for this batch: block below
        # the provider, persist nothing, keep recovery pending.
        logger.warning(
            "Victim replay: malformed batch identity (duplicate/empty call "
            "ids) for session %s; blocking below the provider without "
            "fabricated rows",
            session_id,
        )
        return VictimReplayOutcome(
            failure="malformed batch identity: duplicate/empty call ids; "
            "blocked below the provider",
        )

    if not plan.has_replay_work:
        if plan.batch_present and plan.fail_closed_calls:
            return _finish_repair(
                raw_history,
                VictimReplayPlan(
                    batch_present=True,
                    fail_closed_calls=list(plan.fail_closed_calls),
                ),
                [],
                session_store=session_store,
                session_id=session_id,
                ledger=ledger,
                reserved_calls=[],
            )
        return VictimReplayOutcome()

    # Working copy: the caller's plan is not mutated by the reservation
    # pass.
    dispatch_calls: List[ReplayCall] = []
    for call in plan.replay_calls:
        reserved, reason = ledger.reserve_execution(call)
        if reserved:
            dispatch_calls.append(call)
            continue
        # Held (an earlier recovery or ANOTHER WORKER owns an unresolved
        # execution of this exact call identity) or unprovable (store
        # error): a conflict means this worker does not own the batch.
        # Stand down WITHOUT provider invocation, without UNKNOWN rows,
        # and without clearing recovery: release only the reservations
        # THIS pass took (so this stand-down fences nothing), and return a
        # typed BLOCKED outcome.  Continuation is legal only after the
        # winner's durable closure of the exact batch is proven.
        logger.warning(
            "Victim replay: call %r not reserved (%s); standing down "
            "without execution",
            call.call_id,
            reason,
        )
        for taken in dispatch_calls:
            ledger.release_execution(taken)
        return VictimReplayOutcome(
            failure=f"reservation conflict for {call.call_id!r}: {reason}",
        )

    dispatch = getattr(agent, "_execute_tool_calls", None)
    if not callable(dispatch):
        return VictimReplayOutcome(failure="agent has no tool dispatcher")

    work_messages: List[Dict[str, Any]] = []
    logger.info(
        "Transparent resume: re-running %d interrupted tool call(s)%s",
        len(dispatch_calls),
        f" for session {session_id}" if session_id else "",
    )
    # The dispatcher's incremental flush resolves the agent-level
    # user-message persistence override BY INDEX into the messages list.
    # That index belongs to the previous turn's conversation — applied to
    # these synthetic tool-only rows it would corrupt the persisted result
    # — so clear it for the dispatch window and restore after.
    _saved_override = (
        getattr(agent, "_persist_user_message_idx", None),
        getattr(agent, "_persist_user_message_override", None),
        getattr(agent, "_persist_user_message_timestamp", None),
    )
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    # Exact-ID seam: the dispatcher's incremental DB flush would persist
    # its result rows under the NORMALIZED id (make_tool_result_message
    # coalesces composite bridge ids), leaving a second, alias-id row in
    # the transcript while the wire carries the exact one.  Suppress the
    # dispatcher's session-DB writes for the replay window — every
    # ordinary middleware (authorization, hooks, schema, approval,
    # budget) still runs; ONLY persistence is held back — so the single
    # durable write is the exact-id row this recovery makes below, through
    # the checked append.
    _saved_persist_disabled = getattr(agent, "_persist_disabled", False)
    agent._persist_disabled = True
    try:
        dispatch(
            build_replay_assistant_message(
                VictimReplayPlan(replay_calls=dispatch_calls)
            ),
            work_messages,
            effective_task_id,
            0,
        )
    except Exception:
        logger.warning(
            "Victim replay dispatch failed; keeping fail-closed recovery",
            exc_info=True,
        )
        return VictimReplayOutcome(failure="dispatcher raised")
    finally:
        agent._persist_disabled = _saved_persist_disabled
        (
            agent._persist_user_message_idx,
            agent._persist_user_message_override,
            agent._persist_user_message_timestamp,
        ) = _saved_override

    fresh_rows, restamp_error = _restamp_fresh_rows(
        dispatch_calls,
        [
            row
            for row in work_messages
            if isinstance(row, dict) and row.get("role") == "tool"
        ],
    )
    if restamp_error is not None:
        # Partial/ambiguous dispatcher output is a FAILED recovery — never
        # a repaired history or a model continuation.  Any row the
        # dispatcher already flushed durably stays durable, so a later
        # bounded recovery can reconcile it without re-execution.
        logger.warning("Victim replay aborted: %s", restamp_error)
        return VictimReplayOutcome(failure=restamp_error)

    working = VictimReplayPlan(
        batch_present=plan.batch_present,
        replay_calls=dispatch_calls,
        fail_closed_calls=list(plan.fail_closed_calls),
        interrupted_call_ids=plan.interrupted_call_ids,
        completed_call_ids=plan.completed_call_ids,
    )
    return _finish_repair(
        raw_history,
        working,
        fresh_rows,
        session_store=session_store,
        session_id=session_id,
        ledger=ledger,
        reserved_calls=dispatch_calls,
    )


def _finish_repair(
    raw_history: List[Dict[str, Any]],
    plan: VictimReplayPlan,
    fresh_rows: List[Dict[str, Any]],
    *,
    session_store: Any,
    session_id: Optional[str],
    ledger: ReplayExecutionLedger,
    reserved_calls: List[ReplayCall],
) -> VictimReplayOutcome:
    """Splice, persist, release reservations, and report.

    A persistence failure OR a reservation-release failure FAILS the
    repair: the outcome carries ``failure`` (blocking) and no repaired
    history, so no model continuation or turn-clear can land on top of a
    half-persisted batch or a stale execution fence.
    """
    repaired = splice_replayed_results(raw_history, plan, fresh_rows)
    # The UNKNOWN orphan-recovery rows synthesized for fail-closed calls of
    # this batch are part of the repair: persist them once so the on-disk
    # transcript pairs exactly like the model-visible one.  The marker key
    # itself never reaches the transcript or the model-facing history.
    orphan_rows = [
        row for row in repaired if isinstance(row, dict) and row.get("_orphan_recovery")
    ]
    for row in orphan_rows:
        row.pop("_orphan_recovery", None)
    persist_rows = list(fresh_rows) + orphan_rows
    # Every replacement row's id has its stale interrupted markers
    # soft-archived in the SAME durable transaction that lands the fresh
    # rows — the on-disk transcript must end up holding exactly ONE active
    # result row per exact call id, matching the spliced history.
    superseded_ids = sorted(
        {
            str(row.get("tool_call_id") or "")
            for row in persist_rows
            if isinstance(row, dict) and row.get("tool_call_id")
        }
    )
    failed = _persist_replayed_rows(
        persist_rows,
        session_store=session_store,
        session_id=session_id,
        superseded_call_ids=superseded_ids,
    )
    if failed:
        failed_ids = ", ".join(
            sorted(str(row.get("tool_call_id") or "") for row in failed)
        )
        logger.error(
            "Victim replay: %d replacement result(s) not durable (%s); "
            "failing the recovery so no continuation happens on top of a "
            "half-persisted batch",
            len(failed),
            failed_ids,
        )
        return VictimReplayOutcome(
            failure=f"persistence failed for: {failed_ids}",
        )
    # Durable now — release the write-ahead reservations so a provider
    # reusing the id in a later turn is never fenced by a stale record.
    # A release failure is BLOCKING, never ignored: the rows are durable,
    # but a fence that survives this recovery would close a future
    # legitimate reuse of the same call identity.
    for call in reserved_calls:
        if not ledger.release_execution(call):
            logger.error(
                "Victim replay: reservation release failed for %r after "
                "durable persistence; failing the recovery so the stale "
                "fence is surfaced instead of ignored",
                call.call_id,
            )
            return VictimReplayOutcome(
                failure=f"reservation release failed for {call.call_id!r}",
            )
    return VictimReplayOutcome(
        repaired_history=repaired,
        replayed_call_ids=[
            str(row.get("tool_call_id") or "") for row in fresh_rows
        ],
    )


# ──────────────────────────────────────────────────────────────────────
# Turn-entry decision
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ForcedResumeTurn:
    """How a forced victim's recovered turn enters the model.

    ``continue_interrupted_turn`` — True when there is no real user text:
    the turn continues through the ordinary in-loop seam
    (``run_conversation(continue_interrupted_turn=True)``) with history
    ending in the completed tool batch, and ``message`` is None so no
    synthetic user row can exist anywhere.

    False when a real user message arrived while recovery was closing the
    batch: ``message`` is that text VERBATIM and the normal turn path
    appends it after the completed assistant→tool batch — a legal
    boundary, never interleaved inside the batch.
    """

    message: Optional[str] = None
    continue_interrupted_turn: bool = False
    persist_user_message: Optional[str] = None


def plan_forced_resume_turn(message: Optional[str]) -> ForcedResumeTurn:
    """Route a forced victim's turn entry with zero synthetic prose."""
    if isinstance(message, str) and message.strip():
        # Real user input: verbatim to the model AND to the transcript.
        return ForcedResumeTurn(
            message=message,
            continue_interrupted_turn=False,
            persist_user_message=message,
        )
    # Synthesized auto-resume event with no user text: continue the
    # interrupted turn itself.  No note, no blank user row — the model
    # request is the ordinary "continue after tool results" call.
    return ForcedResumeTurn(
        message=None,
        continue_interrupted_turn=True,
        persist_user_message=None,
    )


def trim_incomplete_assistant_text_tail(
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Weakest correct policy for a text-only forced interruption.

    A turn interrupted while the model was mid-text leaves a trailing
    ``assistant`` text row in durable history.  Transparent continuation
    cannot extend that row (no provider continues a previous assistant
    message), and appending a NEW assistant row after it persists the
    invalid ``user, assistant, assistant`` sequence strict providers
    reject.  Resume instead from the ORIGINAL LEGAL BOUNDARY — the last
    user/tool row — by excluding the incomplete assistant tail.  The model
    then regenerates its answer to the user's message as an ordinary
    first response; no fake user/system row is invented anywhere.

    Returns ``(trimmed_rows, dropped_rows)``.  Only a contiguous trailing
    run of ``assistant`` rows WITHOUT tool calls is dropped: an
    ``assistant(tool_calls)`` tail belongs to the replay path, and any
    completed tool results stay paired with their issuing row.
    """
    trimmed = list(rows)
    dropped: List[Dict[str, Any]] = []
    while trimmed:
        last = trimmed[-1]
        if not (
            isinstance(last, dict)
            and last.get("role") == "assistant"
            and not last.get("tool_calls")
        ):
            break
        dropped.insert(0, trimmed.pop())
    return trimmed, dropped
