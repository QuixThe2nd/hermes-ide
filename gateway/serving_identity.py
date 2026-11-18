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
  ``draining-predecessor``);
- :func:`serving_runtime_note` renders the concise per-turn runtime note
  that rides the existing api_content sidecar channel (never the system
  prompt);
- :func:`overlap_fields` merges the role into the control socket's
  ``identify`` answer so out-of-process consumers can tell a draining
  predecessor from the serving successor;
- :func:`serving_status_lines` renders the status-surface block that
  separates the authoritative serving gateway from supervisor/predecessor
  evidence, and says "unknown" — never a false serving claim — when no
  identity source is provable.

Go twin: ``internal/servingidentity`` (same classification and wording
contract, independently tested; production Python never imports it).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

ROLE_SERVING = "serving"
ROLE_OVERLAP_SUCCESSOR = "overlap-successor"
ROLE_DRAINING_PREDECESSOR = "draining-predecessor"

_identity_cache: Optional[dict[str, Any]] = None


def _static_identity_fields() -> dict[str, Any]:
    """pid / start fingerprint / code sha of this process, computed once.

    These never change for the life of the process, and ``_get_code_identity_fields``
    shells out to git — far too slow to repeat per turn.
    """
    global _identity_cache
    if _identity_cache is None:
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
    - ``serving`` — otherwise.
    """
    fields = dict(_static_identity_fields())
    fields["role"] = ROLE_SERVING
    fields["predecessor_pid"] = None
    fields["successor_pid"] = None

    def _pid_attr(name: str) -> Optional[int]:
        try:
            value = int(getattr(runner, name, 0) or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    try:
        if bool(getattr(runner, "_restart_requested", False)):
            fields["role"] = ROLE_DRAINING_PREDECESSOR
            fields["successor_pid"] = _pid_attr("_overlap_restart_successor_pid")
        elif bool(getattr(runner, "_overlap_successor_pending", False)):
            fields["role"] = ROLE_OVERLAP_SUCCESSOR
            fields["predecessor_pid"] = _pid_attr("_overlap_predecessor_pid")
    except Exception:
        logger.debug("serving identity: role classification failed", exc_info=True)
        fields["role"] = ROLE_SERVING
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
    the pid file, or status files are not the conversation's process; a
    knowable transition appends the predecessor/successor explanation.
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
        successor_clause = (
            f"successor pid {successor} already takes new turns"
            if successor
            else "a successor is being started and will take new turns"
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


def overlap_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Identify-payload merge of the serving-identity role; unknowns omitted."""
    fields: dict[str, Any] = {"role": snapshot.get("role") or ROLE_SERVING}
    for key in ("predecessor_pid", "successor_pid"):
        value = snapshot.get(key)
        if isinstance(value, int) and value > 0:
            fields[key] = value
    return fields


def _start_clause(pid: Any, start_time: Any) -> str:
    rendered = f"pid {pid}"
    if start_time:
        rendered += f", start {start_time}"
    return rendered


def serving_status_lines(
    *,
    identify: Optional[Mapping[str, Any]] = None,
    state_pid: Any = None,
    state_gateway_state: Any = None,
    state_pid_is_live: bool = False,
    state_is_stale: bool = False,
    chained: Optional[Mapping[str, Any]] = None,
    pid_live: Optional[Callable[[int], bool]] = None,
) -> list[str]:
    """Status-surface lines separating the serving gateway from predecessor evidence.

    Resolution ladder (shared contract with ``internal/servingidentity``):

    1. A live control-socket ``identify`` answer is authoritative. A
       draining-predecessor answer must point at its reported successor as
       the serving process — with a liveness warning when the successor is
       not running.
    2. Without a socket answer, a live, fresh ``gateway_state.json`` record
       names the serving pid (labelled by its source).
    3. Otherwise "unknown" with the reason — never a serving claim that
       cannot be proven.

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
        if role == ROLE_DRAINING_PREDECESSOR and isinstance(successor_pid, int) and successor_pid > 0:
            if pid_live is None or pid_live(successor_pid):
                lines.append(
                    f"✓ Serving gateway: pid {successor_pid} — restart successor; new turns are served here")
                lines.append(
                    f"  Draining predecessor: pid {identify.get('pid')} — finishing its in-flight turns; "
                    "systemctl, the pid file and gateway_state.json still show pid "
                    f"{identify.get('pid')} until it exits (expected handoff, not an incomplete restart)")
            else:
                lines.append(
                    f"⚠ Serving gateway: unknown — predecessor pid {identify.get('pid')} is draining, but its "
                    f"reported successor pid {successor_pid} is not running; the restart may have failed, "
                    "check the gateway log before restarting anything")
        elif role == ROLE_OVERLAP_SUCCESSOR:
            lines.append(
                f"✓ Serving gateway: {answering} — restart successor, now authoritative")
        else:
            lines.append(f"✓ Serving gateway: {answering} — answering on the gateway control socket")
    elif isinstance(state_pid, int) and state_pid > 0:
        if not state_is_stale and state_pid_is_live:
            lines.append(
                f"✓ Serving gateway: pid {state_pid} (from gateway_state.json, state '{state_gateway_state}')")
        elif state_is_stale:
            lines.append(
                "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "
                f"is stale (state '{state_gateway_state}')")
        else:
            lines.append(
                f"⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "
                f"pid {state_pid} is not running")
    else:
        lines.append(
            "⚠ Serving gateway: unknown — no control socket answer and no gateway_state.json record")
    if isinstance(chained, Mapping):
        source = chained.get("source") or "an earlier restart request"
        lines.append(
            "⏳ Chained restart queued: another restart will run once the current handoff "
            f"completes (requested by {source})")
    return lines
