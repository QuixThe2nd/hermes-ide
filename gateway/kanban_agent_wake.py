"""Agent-wake turn for a blocked dev-pipeline job.

The kanban notifier already delivers the *human* half of a block: a short
message ("⏸ Kanban t_… blocked: …") to every subscription destination. For a
delegated dev-pipeline job that is not enough, because the submitter is an
agent that can diagnose and recover the job itself — relaying the Discord
ping by hand cost hours on the 2026-08-25 incident (job t_135a3014: run 6
OOM at the then-hardcoded ``MemoryMax=6G``, run 8 SIGTERM at
``RuntimeMaxSec``, then ``block_loop_detected`` routed it to triage with no
agent-facing signal at all).

This module owns the agent-facing half:

* :func:`actionable_dev_block` — is there a ``dev_blocked`` event in this
  claim the agent should act on? Deliberate human/safety stops
  (``cancelled_by_user``, ``secret_in_diff``) answer no, so a task the human
  parked stays parked.
* :func:`build_dev_block_brief` — the self-contained turn text: board, task,
  block kind + reason, recent runs with durations and failure lines,
  workspace and logs paths, and the standing
  investigate-then-recover-else-escalate instruction.
* the wake ledger — at most ONE agent wake per
  ``(board, task, block signature, destination)``, persisted under the
  shared kanban root so it survives gateway restarts. A recovery attempt
  that re-blocks with the *same* signature must not wake that destination
  again; the human message is then the only signal, which is what breaks
  the agent-self loop. The destination is part of the key so a task with
  several subscriptions behaves the same whether they are claimed in one
  tick or several: each destination gets exactly one turn.

Delivery is deliberately NOT implemented here. The notifier injects the
brief through :func:`gateway.wake.deliver_wake` — the same synthetic-turn
path the existing kanban wake uses — so session resolution (chat_type,
thread, scope, profile) stays in exactly one place.

Every entry point is fail-soft: an exception here must degrade into "no
wake this tick" (logged), never into a dead notifier loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("gateway.run")

# Block kinds that must never wake the agent. ``cancelled_by_user`` is the
# human deliberately parking the job — the agent must leave it alone (the
# executor records it via ``_handle_external_block``). ``secret_in_diff`` is
# a deliberate safety stop: a secret already reached the diff, so the right
# next step is a human security decision, not an agent iterating on the
# same working tree. Everything else in ``executor.DEV_BLOCK_KINDS`` is a
# mechanical failure the submitter can at least diagnose.
AGENT_WAKE_SUPPRESSED_KINDS = frozenset({"cancelled_by_user", "secret_in_diff"})

# How many recent attempts the brief carries. Three is enough to show a
# repeated-failure pattern (the OOM-then-SIGTERM shape from the incident)
# without turning the wake turn into a log dump.
BRIEF_RUN_LIMIT = 3

# Ledger bounds. The ledger is a small JSON map of "already woke the agent
# for this (board, task, signature)" → unix time. Bounded on both axes so a
# long-lived install cannot grow it without limit: entries older than the
# TTL are dropped on write, and the newest MAX entries are kept when the
# cap is exceeded.
_WAKE_LEDGER_TTL_SECONDS = 14 * 24 * 3600
_WAKE_LEDGER_MAX_ENTRIES = 512


def agent_wake_enabled() -> bool:
    """Read ``dev_pipeline.agent_wake_on_block`` live from config.yaml.

    Read fresh on every call (same posture as ``progress_notifications``
    and ``kanban.auto_decompose``) so flipping the gate to ``false`` stops
    agent wakes on the next notifier tick instead of requiring a gateway
    restart. Fails to the shipped default (enabled): a transient config
    read error must not silently disable the behaviour, and a wake is
    bounded by the ledger anyway.
    """
    try:
        from plugins.dev_pipeline.pipeline import get_dev_pipeline_config

        return bool(get_dev_pipeline_config().get("agent_wake_on_block", True))
    except Exception:
        return True


def actionable_dev_block(events: Sequence[Any]) -> Optional[dict]:
    """Return the ``dev_blocked`` payload the agent should act on, if any.

    Scans a notifier claim for ``dev_blocked`` events (only the dev
    executor writes that kind, so its presence also scopes the wake to
    dev-pipeline tasks — a plain kanban task routed to triage never gets an
    agent turn). The *last* one wins: it is the state the task is in now.
    ``None`` when the claim has none, or when the newest one is a deliberate
    human/safety stop.
    """
    payload: Optional[dict] = None
    for ev in events:
        if getattr(ev, "kind", None) != "dev_blocked":
            continue
        body = getattr(ev, "payload", None)
        payload = body if isinstance(body, dict) else {}
    if not payload:
        return None
    if str(payload.get("block_kind") or "") in AGENT_WAKE_SUPPRESSED_KINDS:
        return None
    return payload


def block_signature(payload: Mapping[str, Any]) -> str:
    """Stable signature of a block cause, for ledger dedupe.

    Kind + whitespace-collapsed, case-folded reason. Case folding merges
    ``OOM at 6G`` with ``oom at 6g``; a genuinely different cause (different
    kind, or a materially different reason string) produces a different
    signature and wakes again.
    """
    kind = str(payload.get("block_kind") or "")
    reason = " ".join(str(payload.get("reason") or "").split()).casefold()
    digest = hashlib.sha1(
        f"{kind}\x00{reason}".encode("utf-8", "replace")
    ).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Wake ledger
# ---------------------------------------------------------------------------

def wake_ledger_path() -> Path:
    """``<kanban root>/kanban/agent_wake_ledger.json``.

    Sits beside ``current`` and ``.dispatcher.lock`` in the shared kanban
    root (cross-profile by design — the board is shared, so the wake record
    must be too), and survives gateway restarts.
    """
    from hermes_cli import kanban_db as _kb

    return _kb.kanban_home() / "kanban" / "agent_wake_ledger.json"


def _load_ledger(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("agent-wake ledger unreadable at %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _write_ledger(path: Path, ledger: dict[str, float]) -> None:
    """Prune to TTL + size cap, then atomically replace the file."""
    now = time.time()
    pruned = {
        key: at for key, at in ledger.items()
        if now - at < _WAKE_LEDGER_TTL_SECONDS
    }
    if len(pruned) > _WAKE_LEDGER_MAX_ENTRIES:
        keep = sorted(pruned.items(), key=lambda kv: kv[1], reverse=True)
        pruned = dict(keep[:_WAKE_LEDGER_MAX_ENTRIES])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".agent_wake_ledger.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pruned, handle, sort_keys=True)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ledger_key(board: str, task_id: str, signature: str, destination: str) -> str:
    return f"{board}/{task_id}/{signature}/{destination}"


def already_woke(
    path: Path, board: str, task_id: str, signature: str, destination: str,
) -> bool:
    """True when *destination* was already woken for this (task, signature)."""
    try:
        return ledger_key(board, task_id, signature, destination) in _load_ledger(path)
    except Exception as exc:
        logger.debug("agent-wake ledger check failed: %s", exc)
        return False


def record_wake(
    path: Path, board: str, task_id: str, signature: str, destination: str,
) -> None:
    """Mark a (task, signature, destination) as woken. Called after delivery.

    Recording after (not before) delivery means a failed wake leaves the
    ledger untouched and the next tick retries it — a lost wake is the
    original incident, a rare duplicate wake from a cross-process race is
    merely a redundant turn.
    """
    try:
        ledger = _load_ledger(path)
        ledger[ledger_key(board, task_id, signature, destination)] = time.time()
        _write_ledger(path, ledger)
    except Exception as exc:
        logger.warning(
            "agent-wake ledger write failed for %s/%s: %s", board, task_id, exc,
        )


# ---------------------------------------------------------------------------
# Brief rendering
# ---------------------------------------------------------------------------

def _scrub(text: str, limit: int) -> str:
    """One-line, length-capped, secret-scrubbed copy of *text*."""
    line = " ".join(str(text or "").split())
    if not line:
        return ""
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(line, force=True)
    except Exception:
        return line


def _recent_attempts(conn: Any, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, started_at, ended_at, outcome, error
          FROM task_runs
         WHERE task_id = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (task_id, BRIEF_RUN_LIMIT),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        started = row["started_at"]
        ended = row["ended_at"]
        duration = ""
        if started and ended:
            seconds = max(0, int(ended) - int(started))
            if seconds >= 3600:
                duration = f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
            else:
                duration = f"{seconds // 60}m{seconds % 60:02d}s"
        out.append({
            "run_id": int(row["id"]),
            "duration": duration,
            "outcome": str(row["outcome"] or "unknown"),
            "error": _scrub(row["error"] or "", 200),
        })
    return out


def _workspace_and_logs(conn: Any, board: str, task_id: str) -> tuple[str, str]:
    """Resolve the job's workspace and logs dirs for the brief.

    Prefers the paths the executor actually persisted on the latest run's
    pipeline state (authoritative for what ran), then falls back to the
    deterministic per-task layout the executor itself derives — mirrors
    ``executor.workspace_paths`` without importing the plugin module into
    the gateway.
    """
    from hermes_cli import kanban_db as _kb

    workspace = str(_kb.workspaces_root(board=board) / task_id / "repo")
    logs = str(_kb.worker_logs_dir(board=board) / task_id)
    try:
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row and row["metadata"]:
            meta = json.loads(row["metadata"])
            state = meta.get("dev_pipeline") if isinstance(meta, dict) else None
            if isinstance(state, dict):
                if state.get("repo_path"):
                    workspace = str(state["repo_path"])
                if state.get("logs_root"):
                    logs = str(state["logs_root"])
    except Exception as exc:
        logger.debug("agent-wake workspace resolution failed for %s: %s", task_id, exc)
    return workspace, logs


def build_dev_block_brief(
    conn: Any,
    *,
    board: str,
    task_id: str,
    task: Any,
    payload: Mapping[str, Any],
    triage: bool,
) -> str:
    """Render the self-contained agent turn for a blocked dev-pipeline job.

    Everything the submitting agent needs to act without asking the human
    "which job / where are the logs": identity (board, task id, title),
    cause (block kind + reason), evidence (recent runs with durations and
    failure lines), locations (workspace, logs dir), and the standing
    instruction that separates autonomous recovery from escalation.
    """
    title = str(getattr(task, "title", None) or task_id)[:120]
    kind = str(payload.get("block_kind") or "unknown")
    reason = _scrub(payload.get("reason"), 300)
    workspace, logs = _workspace_and_logs(conn, board, task_id)
    attempts = _recent_attempts(conn, task_id)

    lines = [
        f'[Dev-pipeline job "{task_id}" blocked — automated pipeline '
        "notification, not the user. Investigate and recover it yourself; "
        "escalate only what genuinely needs a human.]",
        "",
        f"Task:      {title}",
        f"Task id:   {task_id}",
        f"Board:     {board}",
        f"Block:     {kind}" + (f" — {reason}" if reason else ""),
    ]
    if triage:
        lines.append(
            "Routed:    triage (block loop detected — re-blocking for the same "
            "cause past the recurrence limit)",
        )
    lines.append(f"Workspace: {workspace}")
    lines.append(f"Logs:      {logs}")

    if attempts:
        lines.append("")
        lines.append("Recent attempts (newest first):")
        for run in attempts:
            entry = (
                f"  run {run['run_id']}"
                + (f" · {run['duration']}" if run["duration"] else "")
                + f" · {run['outcome']}"
            )
            if run["error"]:
                entry += f" · {run['error']}"
            lines.append(entry)

    lines += [
        "",
        "Standing instruction:",
        "  1. Investigate first — read the attempt logs above before changing "
        "anything.",
        "  2. Recover autonomously when the cause is mechanical (resource "
        "limits, stale executor state, a known transient): fix the cause, then "
        "requeue the job.",
        "  3. Escalate to the human only when the decision is genuinely "
        "theirs — credentials/auth, business trade-offs, anything destructive "
        "— and bring a diagnosis plus one concrete ask, not just the error.",
    ]
    return "\n".join(lines)
