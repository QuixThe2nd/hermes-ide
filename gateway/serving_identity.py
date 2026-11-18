"""Serving-process identity: which gateway process is actually serving turns.

After an overlap restart the draining predecessor keeps owning every
externally observable identity surface — the pid file, ``gateway_state.json``,
systemd's MainPID, and (until it exits) the control socket — while the
successor already serves new turns. Agents and operators consulting those
surfaces mid-handoff repeatedly concluded the restart was incomplete and
waited on the dying process.

This module is the single owner of the serving-identity contract:

- :func:`serving_identity_snapshot` classifies THIS process's role from
  runner state only (``serving`` / ``overlap-successor`` /
  ``draining-predecessor`` / ``unknown``) and FAILS UNKNOWN — never a
  default ``serving`` claim — when classification raises;
- :func:`serving_runtime_note` renders the concise per-turn runtime note
  that rides the existing api_content sidecar channel (never the system
  prompt). A draining predecessor may claim its successor "already took
  over new turns" ONLY on the hub-attested readiness ack that gates real
  event handoff — a spawned PID alone proves nothing (spawn != ready, and
  PIDs get reused);
- :func:`overlap_fields` merges the role into the control socket's
  ``identify`` answer (with the successor's start fingerprint and readiness)
  so out-of-process consumers can tell a draining predecessor from the
  serving successor;
- :func:`serving_status_lines` renders the status-surface block that
  separates the authoritative serving gateway from supervisor/predecessor
  evidence, and says "unknown" — never a green serving claim — whenever
  identity or readiness cannot be proven (a draining predecessor without a
  confirmed successor, a fresh ``draining`` state record, a successor PID
  that fails its start-fingerprint check).

Go twin: ``internal/servingidentity`` — same classification and rendering
contract, executed by the shared-table contract test and the Go module's
own tests. The gateway hot path renders with this Python module: repo
deployments (pip, docker) carry no Go toolchain and there is no runtime Go
bridge — the same arrangement as ``internal/cronyield``,
``internal/citationstrip`` and friends.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

ROLE_SERVING = "serving"
ROLE_OVERLAP_SUCCESSOR = "overlap-successor"
ROLE_DRAINING_PREDECESSOR = "draining-predecessor"
ROLE_UNKNOWN = "unknown"

_identity_cache: Optional[dict[str, Any]] = None


def _static_identity_fields() -> dict[str, Any]:
    """pid / start fingerprint / code sha of this process, computed once.

    These never change for the life of the process, and
    ``_get_code_identity_fields`` shells out to git — far too slow to repeat
    per turn. Re-derived if the process pid changes (tests monkeypatch
    ``os.getpid``), so a stale cache can never name the wrong process.
    """
    global _identity_cache
    cached_pid = (_identity_cache or {}).get("pid")
    if _identity_cache is None or cached_pid != os.getpid():
        pid_record: dict[str, Any] = {}
        code_fields: dict[str, Any] = {}
        try:
            from gateway.status import _build_pid_record, _get_code_identity_fields
            pid_record = _build_pid_record()
            code_fields = _get_code_identity_fields()
        except Exception:
            logger.debug("serving identity: pid/code identity read failed", exc_info=True)
        _identity_cache = {
            "pid": pid_record.get("pid") or os.getpid(),
            "start_time": pid_record.get("start_time"),
            "code_sha": code_fields.get("code_sha"),
        }
    return _identity_cache


def _pid_attr(runner: Any, name: str) -> Optional[int]:
    try:
        value = int(getattr(runner, name, 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def serving_identity_snapshot(runner: Any) -> dict[str, Any]:
    """Live serving-identity snapshot of THIS gateway process. Never raises.

    Role rules (shared contract with ``internal/servingidentity``):

    - ``draining-predecessor`` — a restart was requested on this process
      (``_restart_requested``); it is finishing in-flight turns while its
      spawned successor (``_overlap_restart_successor_pid``) takes new ones.
      A pure shutdown/pause drain (``_draining`` without
      ``_restart_requested``) is NOT a restart handoff and stays ``serving``.
    - ``overlap-successor`` — this process booted behind a still-live
      predecessor (``_overlap_successor_pending``) and is serving the new
      turns now.
    - ``unknown`` — classification raised: no role claim may be made.
    - ``serving`` — otherwise.

    ``successor_ready`` is the hub-attested readiness ack from
    ``gateway.overlap_restart.overlap_successor_readiness`` — the same
    signal that gates real event handoff; a bare successor PID never
    implies it.
    """
    fields = dict(_static_identity_fields())
    fields.update(
        role=ROLE_SERVING,
        predecessor_pid=None,
        successor_pid=None,
        successor_start_time=None,
        successor_ready=False,
    )
    try:
        if bool(getattr(runner, "_restart_requested", False)):
            fields["role"] = ROLE_DRAINING_PREDECESSOR
            fields["successor_pid"] = _pid_attr(runner, "_overlap_restart_successor_pid")
            fields["successor_start_time"] = _pid_attr(
                runner, "_overlap_restart_successor_start_time")
            from gateway.overlap_restart import overlap_successor_readiness

            fields["successor_ready"] = overlap_successor_readiness(runner) == "ready"
        elif bool(getattr(runner, "_overlap_successor_pending", False)):
            fields["role"] = ROLE_OVERLAP_SUCCESSOR
            fields["predecessor_pid"] = _pid_attr(runner, "_overlap_predecessor_pid")
    except Exception:
        logger.debug("serving identity: role classification failed", exc_info=True)
        fields["role"] = ROLE_UNKNOWN
        fields["predecessor_pid"] = None
        fields["successor_pid"] = None
        fields["successor_start_time"] = None
        fields["successor_ready"] = False
    return fields


def _build_clause(fields: Mapping[str, Any]) -> str:
    start = fields.get("start_time")
    sha = fields.get("code_sha")
    rendered = f"pid {fields.get('pid')}"
    if start:
        rendered += f", start {start}"
    if sha:
        rendered += f", build {str(sha)[:8]}"
    return rendered


def serving_runtime_note(runner: Any) -> str:
    """Concise per-turn runtime note naming the process that serves THIS turn.

    Wording contract (shared with ``internal/servingidentity``): the note
    always names the serving pid and states that pids reported by systemctl,
    the pid file, or status files are not the conversation's process. A
    draining predecessor may name its successor pid, but claims the
    successor already serves new turns ONLY on the hub-attested readiness
    ack — never a bare spawn PID.
    """
    fields = serving_identity_snapshot(runner)
    identity = _build_clause(fields)
    role = fields["role"]
    if role == ROLE_OVERLAP_SUCCESSOR and fields.get("predecessor_pid"):
        return (
            "[System note: This turn is served by Hermes gateway "
            f"{identity}. It is the restart successor: predecessor pid "
            f"{fields['predecessor_pid']} is draining its final in-flight "
            "turns, so systemctl, the pid file and gateway_state.json may "
            f"still report pid {fields['predecessor_pid']} until it exits. "
            "That drain is the expected handoff — the restart already "
            "succeeded; do not wait for the predecessor or treat its drain "
            "as an incomplete restart.]"
        )
    if role == ROLE_DRAINING_PREDECESSOR:
        successor = fields.get("successor_pid")
        if fields.get("successor_ready"):
            successor_clause = (
                f"its restart successor (pid {successor}) has already taken over new turns"
            )
        elif successor:
            successor_clause = (
                f"a successor process (pid {successor}) is starting and will take "
                "over new turns once its hub attach is confirmed"
            )
        else:
            successor_clause = (
                "new turns will be served by the replacement process once this one exits"
            )

        return (
            "[System note: This turn is served by Hermes gateway "
            f"{identity}. This process is draining for a queued restart; "
            f"{successor_clause}. Finish this turn's work and do not call "
            "restart again for the same restart.]"
        )
    return (
        "[System note: This turn is served by Hermes gateway "
        f"{identity}. That pid — not any pid reported by systemctl, the "
        "pid file, or gateway status files — is the process running this "
        "conversation.]"
    )


def transition_state(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> str:
    """Compare a persisted last-served identity against the current process.

    Shared contract with ``internal/servingidentity``:

    - ``changed`` — the record provably names a different process: a
      different pid, or the same pid with a different start fingerprint
      (PIDs get reused; only the fingerprint disambiguates the recycle).
    - ``same`` — identical pid AND start fingerprint.
    - ``unknown`` — no usable record, or the fingerprints needed to compare
      are missing: claim nothing rather than guess.
    """
    prev = previous if isinstance(previous, Mapping) else {}
    prev_pid = prev.get("pid")
    cur_pid = current.get("pid")
    if not _positive_int(prev_pid) or not _positive_int(cur_pid):
        return "unknown"
    if prev_pid != cur_pid:
        return "changed"
    prev_start = prev.get("start_time")
    cur_start = current.get("start_time")
    if not _positive_int(prev_start) or not _positive_int(cur_start):
        return "unknown"
    return "same" if prev_start == cur_start else "changed"


def serving_transition_note(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> Optional[str]:
    """First-turn-after-handoff note separating carried-over from process-local state.

    Wording contract (shared with ``internal/servingidentity``): rendered
    ONLY when the persisted record provably names a different process
    (:func:`transition_state` == ``"changed"``) — never on a session's
    first-ever turn, ordinary same-process turns, or unprovable comparisons.
    The note names both identities, states the transition is not a new task
    (persisted conversation/task context stays valid), and demands
    revalidation of process-local state (terminals, background processes,
    kernels, sockets, caches, tool handles, recorded pids).
    """
    if transition_state(previous, current) != "changed":
        return None
    old_clause = _start_clause(previous.get("pid"), previous.get("start_time"))
    return (
        "[System note: This conversation moved to a new Hermes gateway "
        f"process: {_build_clause(current)} replaced {old_clause}. That was "
        "a process transition, not a new task: the persisted conversation "
        "history and task context carried over and stay valid. "
        "Process-local state did NOT carry over — terminal sessions, "
        "background processes, kernels, sockets, in-memory caches, tool "
        "handles, and any pid you recorded belonged to the old process: "
        "revalidate them (re-open, re-list, re-check) before relying on "
        "them again.]"
    )


def serving_turn_notes(
    runner: Any,
    previous: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    """``(identity note, transition note | None)`` for this turn.

    The transition note renders only when the caller's persisted
    last-served identity (``gateway.serving_transition``) provably names a
    different process; both notes ride the existing one-shot channels
    (turn notes + continuation sidecar).
    """
    return (
        serving_runtime_note(runner),
        serving_transition_note(previous, serving_identity_snapshot(runner)),
    )


def serving_identity_clause(runner: Any) -> str:
    """Short identity clause (``pid N, build abcd1234``) for recovery notes."""
    fields = serving_identity_snapshot(runner)
    clause = f"pid {fields.get('pid')}"
    sha = fields.get("code_sha")
    if sha:
        clause += f", build {str(sha)[:8]}"
    return clause


def overlap_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Identify-payload merge of the serving-identity role; unknowns omitted.

    ``successor_start_time`` lets consumers apply the PID-reuse fingerprint
    check; ``successor_ready`` relays the predecessor's hub-attested
    readiness ack. Both only appear alongside a reported successor.
    """
    role = snapshot.get("role") or ROLE_UNKNOWN
    fields: dict[str, Any] = {"role": role}
    for key in ("predecessor_pid", "successor_pid", "successor_start_time"):
        value = snapshot.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            fields[key] = value
    if isinstance(fields.get("successor_pid"), int):
        fields["successor_ready"] = snapshot.get("successor_ready") is True
    return fields


def _start_clause(pid: Any, start_time: Any) -> str:
    rendered = f"pid {pid}"
    if start_time:
        rendered += f", start {start_time}"
    return rendered


def _successor_identity_state(
    successor_pid: int,
    expected_start: Any,
    resolver: Optional[Callable[[int, Any], str]],
) -> str:
    """``live`` / ``dead`` / ``unknown`` for a reported successor pid."""
    if resolver is None:
        return "unknown" if expected_start is None else "live"
    try:
        state = resolver(successor_pid, expected_start)
    except Exception:
        logger.debug("serving identity: successor identity probe failed", exc_info=True)
        return "unknown"
    return state if state in ("live", "dead", "unknown") else "unknown"

def _pid_candidates(*pid_sources: Any) -> list[int]:
    """Deduped positive-int pids across sources, order-preserving.

    Sources may be a single pid, an iterable of pids (the process scan), or
    ``None``. Non-int and non-positive values are dropped — the scan and the
    state file are evidence surfaces that can carry junk.
    """
    seen: list[int] = []
    for source in pid_sources:
        if source is None:
            continue
        values = source if isinstance(source, (list, tuple, set, frozenset)) else [source]
        for value in values:
            if _positive_int(value) and value not in seen:
                seen.append(value)
    return seen


def _pid_list(pids: list[int]) -> str:
    """``"pid A, pid B"`` for candidate-name rendering."""
    return ", ".join(f"pid {pid}" for pid in pids)

def serving_status_lines(
    *,
    identify: Optional[Mapping[str, Any]] = None,
    state_pid: Any = None,
    state_gateway_state: Any = None,
    state_pid_is_live: bool = False,
    state_is_stale: bool = False,
    state_restart_requested: Any = None,
    chained: Optional[Mapping[str, Any]] = None,
    successor_identity: Optional[Callable[[int, Any], str]] = None,
    gateway_pids: Optional[Iterable[int]] = None,
) -> list[str]:
    """Status-surface lines separating the serving gateway from predecessor evidence.

    Resolution ladder (shared contract with ``internal/servingidentity``):

    1. A live control-socket ``identify`` answer is authoritative — EXCEPT
       a ``draining-predecessor`` answer, which names the serving process
       only when its reported successor carries the hub readiness ack AND
       passes the start-fingerprint check (PID reuse). A
       spawned-but-unattested successor is "starting", never a green
       serving claim; a fingerprint mismatch or dead pid is a warning. A
       LEGACY answer (no overlap block — a gateway whose build predates
       overlap reporting) is never green: its process's serving role cannot
       be proven, and during an overlap window that answerer is exactly the
       draining predecessor.
    2. Without a socket answer, a live, fresh ``gateway_state.json`` record
       names the serving pid — unless the record itself says the process is
       draining (``gateway_state == "draining"`` or ``restart_requested``),
       or more than one gateway process is live (``gateway_pids``): a
       status file names ONE process, never THE server among many.
    3. Otherwise "unknown"/"ambiguous" with the reason and the candidate
       pids — never a serving claim that cannot be proven.

    ``gateway_pids`` (a process scan like ``find_gateway_pids``) is
    evidence, never a serving claim: it only demotes unproven rungs to
    ambiguous, and corroborates proven ones. Optional/absent by default.
    ``chained`` (a peeked ``.restart_chain.json`` payload) adds the
    pending-restart line.
    """
    lines: list[str] = []
    if isinstance(identify, Mapping) and isinstance(identify.get("pid"), int):
        raw_overlap = identify.get("overlap")
        overlap: Mapping[str, Any] = raw_overlap if isinstance(raw_overlap, Mapping) else {}
        answering = _start_clause(identify.get("pid"), identify.get("start_time"))
        role = overlap.get("role")
        successor_pid = overlap.get("successor_pid")
        if role == ROLE_DRAINING_PREDECESSOR:
            predecessor_pid = identify.get("pid")
            if _positive_int(successor_pid):
                expected_start = overlap.get("successor_start_time")
                identity_state = _successor_identity_state(
                    successor_pid, expected_start, successor_identity)
                if identity_state == "dead":
                    lines.append(
                        f"⚠ Serving gateway: unknown — predecessor pid {predecessor_pid} is "
                        f"draining, but its reported successor pid {successor_pid} is not "
                        "running or its PID was reused; the restart may have failed, check "
                        "the gateway log before restarting anything")
                elif overlap.get("successor_ready") is True and identity_state == "live":
                    lines.append(
                        f"✓ Serving gateway: pid {successor_pid} — restart successor "
                        "(hub-confirmed serving); new turns are served here")
                    lines.append(
                        f"  Draining predecessor: pid {predecessor_pid} — finishing its "
                        "in-flight turns; systemctl, the pid file and gateway_state.json "
                        f"still show pid {predecessor_pid} until it exits (expected "
                        "handoff, not an incomplete restart)")
                else:
                    lines.append(
                        f"⏳ Serving gateway: successor pid {successor_pid} is starting — "
                        f"draining predecessor pid {predecessor_pid} spawned it, but its "
                        "readiness is not yet confirmed; re-check in a moment")
            else:
                lines.append(
                    f"⚠ Serving gateway: unknown — gateway pid {predecessor_pid} is draining "
                    "for a restart with no successor recorded; its supervisor will revive or "
                    "respawn it — check the gateway log before restarting anything")
        elif role == ROLE_OVERLAP_SUCCESSOR:
            predecessor_pid = overlap.get("predecessor_pid")
            predecessor_clause = (
                f"; predecessor pid {predecessor_pid} is draining its in-flight turns"
                if _positive_int(predecessor_pid) else ""
            )
            lines.append(
                f"✓ Serving gateway: {answering} — restart successor, serving new turns"
                f"{predecessor_clause}")
        elif role == ROLE_SERVING:
            lines.append(f"✓ Serving gateway: {answering} — answering on the gateway control socket")
        elif not role:
            # LEGACY identify: no overlap block, so the answerer's serving
            # role is unprovable. During an overlap window this is exactly
            # the draining predecessor (a pre-overlap build) — green-serving
            # it named the dying process in the field.
            candidates = _pid_candidates(identify.get("pid"), state_pid, gateway_pids)
            rendered = _pid_list(candidates)
            if len(candidates) > 1:
                lines.append(
                    "⚠ Serving gateway: ambiguous — the control socket is owned by "
                    f"pid {identify.get('pid')}, which reports no restart-handoff state "
                    "(its build predates overlap reporting), while gateway processes "
                    f"{rendered} are live; which one serves turns cannot be proven from "
                    "here — re-check once the handoff completes")
            elif str(state_gateway_state) == "draining" or bool(state_restart_requested):
                lines.append(
                    "⚠ Serving gateway: unknown — the control socket is owned by "
                    f"pid {identify.get('pid')}, which reports no restart-handoff state "
                    "(its build predates overlap reporting) while its own state record "
                    "says it is draining; the serving process cannot be proven from here")
            else:
                lines.append(
                    f"⚠ Serving gateway: unknown — gateway {answering} answered without "
                    "restart-handoff state (its build predates overlap reporting); it is "
                    "the only gateway process found, but its serving role cannot be proven")
        else:
            lines.append(
                f"⚠ Serving gateway: unknown — gateway {answering} answered, but its "
                "restart-handoff state could not be classified")
    elif isinstance(state_pid, int) and state_pid > 0:
        draining = str(state_gateway_state) == "draining" or bool(state_restart_requested)
        candidates = _pid_candidates(state_pid, gateway_pids)
        if not state_is_stale and state_pid_is_live:
            if draining:
                lines.append(
                    "⚠ Serving gateway: unknown — gateway_state.json names pid "
                    f"{state_pid} draining for a restart (state '{state_gateway_state}'); "
                    "status files keep naming the predecessor until the handoff completes"
                    + (f"; live gateway processes: {_pid_list(candidates)}" if len(candidates) > 1 else ""))
            elif len(candidates) > 1:
                lines.append(
                    f"⚠ Serving gateway: ambiguous — gateway_state.json names pid {state_pid} "
                    f"but {_pid_list(candidates)} gateway processes are live; which one "
                    "serves turns cannot be proven from status files alone")
            else:
                lines.append(
                    f"✓ Serving gateway: pid {state_pid} (from gateway_state.json, state '{state_gateway_state}')")
        elif state_is_stale:
            lines.append(
                "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "
                f"is stale (state '{state_gateway_state}')")
        else:
            lines.append(
                "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "
                f"pid {state_pid} is not running")
    else:
        candidates = _pid_candidates(gateway_pids)
        if len(candidates) > 1:
            lines.append(
                "⚠ Serving gateway: ambiguous — no control socket answer and no "
                f"gateway_state.json record; gateway processes {_pid_list(candidates)} "
                "are live and which one serves turns cannot be proven from here")
        else:
            lines.append(
                "⚠ Serving gateway: unknown — no control socket answer and no gateway_state.json record")
    if isinstance(chained, Mapping):
        source = chained.get("source") or "an earlier restart request"
        lines.append(
            "⏳ Chained restart queued: another restart will run once the current handoff "
            f"completes (requested by {source})")
    return lines
