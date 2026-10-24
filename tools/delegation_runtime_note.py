"""Parent-facing delegated-worker runtime snapshot (topical sibling of
``tools.async_delegation``).

A dispatch receipt is ACCEPTANCE, not proof that anything started. This module
turns the registry's own runtime facts — owned by ``tools/async_delegation`` —
into a compact, owner-scoped note the parent's PER-REQUEST assembly appends to
the outgoing request's latest legal user/tool-result content: handle, backend,
observed lifecycle state, observed activity, freshness. No raw logs, secrets,
PIDs, full task prompts, or task/goal fragments at all; the total is bounded
and omissions are disclosed truthfully. Nothing here is a second registry or a
heartbeat — it READS the records the registry already owns and terminal
outcomes still travel the completion rail exclusively.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_RUNTIME_NOTE_MAX_ENTRIES = 6
_RUNTIME_NOTE_MAX_CHARS = 2000
# A single entry line's budget: keeps one hostile/oversized worker from eating
# the whole note before the omission math can react.
_RUNTIME_NOTE_MAX_LINE_CHARS = 220

_BACKEND_LABELS = (
    ("delegate_claude_agent", "claude-code cli"),
    ("delegate_cursor_agent", "cursor agent"),
)


def _backend_label(record: Dict[str, Any]) -> str:
    """Human backend label from the record's tool/result_kind provenance."""
    from tools.async_delegation import (
        RESULT_KIND_CLI_AGENT, RESULT_KIND_CLOUD_AGENT, RESULT_KIND_MISSION,
        RESULT_KIND_SUBAGENT,
    )

    tool = str(record.get("tool") or "")
    for name, label in _BACKEND_LABELS:
        if tool == name:
            return label
    kind = str(record.get("result_kind") or "")
    if kind == RESULT_KIND_CLI_AGENT:
        return "cli agent"
    if kind == RESULT_KIND_CLOUD_AGENT:
        return "cloud agent"
    if kind == RESULT_KIND_MISSION:
        return "external mission"
    if kind == RESULT_KIND_SUBAGENT:
        return "native subagent"
    return tool or "delegation"


def _clean_note_text(value: Any, limit: int) -> str:
    """Flatten, control-strip and truncate a fragment destined for the note.

    Bounded text is not secret removal and not an instruction boundary — the
    callers must never put private/untrusted text here at all; this only
    guarantees the note's own shape (single line, no control bytes)."""
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch == "\t")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _proc_observation(evidence: Dict[str, Any], now: float) -> Optional[tuple]:
    """``(alive, detail)`` from the REAL process handle when there is one.

    A Popen-creation timestamp proves startup happened, not that the process is
    still alive. ``startup_evidence["proc"]`` is the live handle the dispatch
    retained; ``poll()`` distinguishes alive from exited without adding any
    supervision (no kill, no timeout policy). Missing handle → None (honest
    unknown, never guessed)."""
    proc = evidence.get("proc")
    started_at = evidence.get("process_started_at")
    try:
        age = max(0, round(now - float(started_at))) if started_at else None
    except (TypeError, ValueError):
        age = None
    if proc is not None and callable(getattr(proc, "poll", None)):
        try:
            rc = proc.poll()
        except Exception:
            rc = None  # handle closed/zombie-reaped: unknown, don't guess
            return None
        if rc is None:
            return True, ("process alive" + (f", started {age}s ago" if age is not None else ""))
        return False, ("process exited" + (f" (code {rc})" if rc is not None else ""))
    if started_at:
        return None, ("process started" + (f" {age}s ago" if age is not None else "")
                      + "; liveness not directly observed")
    return None


def _runtime_lifecycle(record: Dict[str, Any], activity: Optional[Dict[str, Any]], now: float) -> tuple:
    """``(state, detail)`` from RUNTIME EVIDENCE only — never invented from silence.

    - accepted: the dispatch is admitted but the runner has not begun (executor
      scheduling alone never reads as ``running``);
    - started: the runner thread runs but no process/model activity has been
      observed yet (honest unknown, not a semantic phase);
    - running: observed evidence — a live child process or sampled model
      activity (api calls / current tool);
    - exited: the real process handle reports the child has terminated while
      the record is still live (result formatting/delivery in flight);
    - stalling / finishing: the monitor's or finalizer's own states.

    Externally-driven units (assistant missions) are reported as exactly that —
    never as an OS process queued/running by this process. Remote/cursor
    workers never inherit local-executor startup as remote startup: without an
    observed remote fact the state stays explicitly unconfirmed.
    """
    status = record.get("status")
    if status == "stalling":
        quiet = record.get("_stall_quiet_seconds")
        return "stalling", f"no progress for {round(quiet)}s" if quiet else "no progress observed"
    if status == "finalizing":
        return "finishing", "delivering result"
    evidence = record.get("startup_evidence") or {}
    if status == "running":
        if record.get("external"):
            # BEFORE any OS-process vocabulary: an externally-driven unit
            # (assistant mission) has no worker of ours — "queued for the
            # background pool" would misreport it as an OS process.
            return "underway", "externally driven; not an OS worker of this process"
        proc_obs = _proc_observation(evidence, now)
        # Only a CONFIRMED observation names a state: (True, …) → running,
        # (False, …) → exited. (None, …) means startup was observed but the
        # current liveness was not — falling through keeps it honestly
        # "started/unknown" instead of collapsing unknown into "exited".
        if proc_obs is not None and proc_obs[0] is not None:
            return ("running" if proc_obs[0] else "exited"), proc_obs[1]
        if not record.get("_started"):
            return "accepted", "queued for the background pool; not started yet"
        if activity and (activity.get("api_calls") or activity.get("current_tool")):
            bits = []
            if activity.get("api_calls"):
                bits.append(f"{activity['api_calls']} api call(s)"
                            + (f" across {activity['children']} child(ren)"
                               if (activity.get("children") or 0) > 1 else ""))
            if activity.get("current_tool"):
                bits.append(f"in tool {_clean_note_text(activity['current_tool'], 32)}")
            return "running", "; ".join(bits)
        if evidence.get("process_started_at"):
            # Startup proven, no live handle retained: honest partial evidence —
            # "started", never a guessed running/exited.
            started_at = evidence.get("process_started_at")
            try:
                age = max(0, round(now - float(started_at)))
            except (TypeError, ValueError):
                age = None
            return "started", ("process started" + (f" {age}s ago" if age is not None else "")
                               + "; liveness not directly observed")
        if str(record.get("tool") or "") == "delegate_cursor_agent":
            remote = evidence.get("remote_observed")
            if isinstance(remote, str) and remote:
                return "running", _clean_note_text(remote, 64)
            return "started", "remote run state unconfirmed (local executor started only)"
        return "started", "worker started; no model activity observed yet"
    return str(status or "unknown"), ""


def build_delegation_runtime_note(
    parent_session_ids: Any = (), *, now: Optional[float] = None,
    owner_home_key: str = "",
) -> str:
    """Compact current snapshot of the live delegations owned by *parent_session_ids*.

    Owner scoping is by DURABLE session identity only (the spawner's
    ``parent_session_id`` plus its compression lineage — the caller resolves
    that set). NOT by routing ``session_key``: that key survives ``/new`` and
    is shared across profiles, so an OR-selector on it would leak a different
    durable session's workers. The home fence is an EXACT match on the
    canonical ``hermes_home_key`` identity — never the ``get_active_profile_name()``
    display label, which collapses distinct custom homes to "custom". A
    missing/failed consumer identity yields NO snapshot (unknown ownership is
    exclusion, not an unfiltered view), and a record whose producer identity is
    missing/failed is likewise excluded.

    One coherent snapshot is taken under a single lock hold (a scalar
    projection per record — callbacks/evidence dicts are referenced, never
    serialized); progress samplers and process ``poll()``s run OUTSIDE the
    lock. Any sampler/poll failure degrades to an honest unknown. Bounded:
    at most ``_RUNTIME_NOTE_MAX_ENTRIES`` entry lines and
    ``_RUNTIME_NOTE_MAX_CHARS`` total INCLUDING the framing and the omission
    disclosure; the disclosed omitted count is the true count. Terminal
    outcomes are NOT duplicated here — they arrive exclusively through the
    completion rail."""
    from tools.async_delegation import (
        _ACTIVE_STATES, _LIVE_STATES, _children_activity_from_token, _records, _records_lock,
    )

    ids = {str(s) for s in (parent_session_ids or ()) if str(s or "")}
    if not ids or not str(owner_home_key or ""):
        # Unknown ownership → no snapshot at all, never an unfiltered view.
        return ""
    home_key = str(owner_home_key)
    now = time.time() if now is None else now
    # Single lock hold: scalar projection + references for post-lock sampling.
    projections: List[Dict[str, Any]] = []
    with _records_lock:
        owned = [r for r in _records.values()
                 if r.get("status") in _LIVE_STATES
                 and str(r.get("parent_session_id") or "") in ids
                 # Home fence: EXACT canonical-home match. The registry is
                 # process-global while each conversation belongs to one home;
                 # records with a missing/failed producer identity ("") are
                 # excluded, not admitted.
                 and str(r.get("owner_home_key") or "") == home_key]
        owned.sort(key=lambda r: r.get("dispatched_at") or 0)
        for r in owned:
            projections.append({
                "delegation_id": r.get("delegation_id"),
                "tool": r.get("tool"), "result_kind": r.get("result_kind"),
                "status": r.get("status"), "_started": r.get("_started"),
                "_stall_quiet_seconds": r.get("_stall_quiet_seconds"),
                "dispatched_at": r.get("dispatched_at"), "external": r.get("external"),
                "startup_evidence": r.get("startup_evidence"),
                "progress_fn": (r.get("progress_fn")
                                if r.get("status") in _ACTIVE_STATES and callable(r.get("progress_fn"))
                                else None),
            })
    if not projections:
        return ""
    shown, omitted = projections[:_RUNTIME_NOTE_MAX_ENTRIES], max(
        0, len(projections) - _RUNTIME_NOTE_MAX_ENTRIES)

    def _line(entry: Dict[str, Any]) -> str:
        activity = None
        fn = entry.get("progress_fn")
        if fn is not None:
            try:  # sampler runs outside the lock; failure = unknown, never fatal
                token, _in_tool = fn()
                parsed = [a for a in (_children_activity_from_token(token, now) or [])
                          if isinstance(a, dict)]
                if parsed:
                    # Batch fan-out: AGGREGATE the per-child samples — summed
                    # api calls plus any observed current tool — so one line
                    # reports the whole unit, not just its first child.
                    calls = sum(a.get("api_calls") for a in parsed
                                if isinstance(a.get("api_calls"), (int, float)))
                    activity = {
                        "api_calls": calls,
                        "current_tool": next(
                            (a.get("current_tool") for a in parsed if a.get("current_tool")), None),
                        "children": len(parsed),
                    }
            except Exception:
                activity = None
        state, detail = _runtime_lifecycle(entry, activity, now)
        age = max(0, round(now - (entry.get("dispatched_at") or now)))
        line = f"- {entry.get('delegation_id')} [{_backend_label(entry)}] {state}"
        if detail:
            line += f" ({detail})"
        line += f" — dispatched {age}s ago"
        return _clean_note_text(line, _RUNTIME_NOTE_MAX_LINE_CHARS)

    def _compose(entries: List[Dict[str, Any]], om: int) -> str:
        header = time.strftime("%H:%M:%S", time.localtime(now))
        body = [f"[delegated workers: current runtime state as of {header} "
                "— observed by the harness, not a heartbeat]"]
        body.extend(_line(e) for e in entries)
        if om:
            body.append(f"({om} more active worker(s) omitted)")
        return "\n".join(body)

    # Cap applied AFTER framing+disclosure, on the whole note: drop whole
    # entries (folding each into the TRUE omitted count) until it fits.
    note = _compose(list(shown), omitted)
    while shown and len(note) > _RUNTIME_NOTE_MAX_CHARS:
        shown.pop()
        omitted += 1
        note = _compose(list(shown), omitted)
    if len(note) > _RUNTIME_NOTE_MAX_CHARS:  # pathological: header alone too big
        note = _compose([], omitted)
    return note
