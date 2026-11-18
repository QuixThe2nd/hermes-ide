"""One-shot restart requests handed from a draining gateway to its successor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from utils import atomic_json_write, is_truthy_value

logger = logging.getLogger(__name__)

_RESTART_CHAIN_FILENAME = ".restart_chain.json"
# Match GatewayRunner.start's existing post-connect settle before lifecycle work.
POST_CLAIM_STARTUP_SETTLE_SECONDS = 1.0


def _restart_chain_path() -> Path:
    # The same launch-profile home used by .restart_notify.json. Imported late:
    # run.py owns the gateway facade and its profile-aware home binding.
    from gateway.run import _hermes_home

    return _hermes_home / _RESTART_CHAIN_FILENAME


def has_pending_overlap_successor(runner: Any) -> bool:
    """A draining gateway can chain only while its successor is still pending."""
    return bool(
        getattr(runner, "_draining", False)
        and getattr(runner, "_overlap_restart_successor_started", False)
    )


def _requesting_source_summary(
    source: Any, message_id: Optional[str], *, kind: str,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"kind": kind}
    if source is None:
        return summary
    platform = getattr(source, "platform", None)
    summary.update({
        "platform": getattr(platform, "value", platform),
        "chat_id": getattr(source, "chat_id", None),
        "chat_type": getattr(source, "chat_type", None),
    })
    for field in ("user_id", "thread_id"):
        value = getattr(source, field, None)
        if value:
            summary[field] = value
    if message_id is not None:
        summary["message_id"] = message_id
    return summary


def peek_chained_restart() -> Optional[dict[str, Any]]:
    """Read the chained-restart marker WITHOUT consuming it (status display only).

    ``consume_chained_restart`` unlinks the marker because the successor acts
    on it exactly once; status surfaces must only report that a chained
    restart is pending.
    """
    try:
        payload = json.loads(_restart_chain_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def record_chained_restart(
    runner: Any,
    *,
    source: Any = None,
    message_id: Optional[str] = None,
    request_kind: str = "user",
    via_service: bool = False,
    restart_hub: bool = False,
) -> bool:
    """Synchronously record one successor restart; repeated requests coalesce.

    There is deliberately no await or worker hop in this write. A cancelled or
    failed hand-off therefore cannot publish a marker after its caller has
    already been told that no chain was accepted.

    ``restart_hub`` rides the chain payload instead of a direct hub marker: a
    ``--hub`` request joining an existing chain is merged into it, so the
    already-up successor re-hands the bounce to the NEXT one.
    """
    if not has_pending_overlap_successor(runner):
        return False
    path = _restart_chain_path()
    from gateway.status import get_process_start_time

    payload = {
        "requested_at": time.time(),
        "source": _requesting_source_summary(source, message_id, kind=request_kind),
        "via_service": bool(via_service),
        # The generation this chain belongs to. Without it neither the
        # direct-claim sweep nor the status surface can tell a live chain
        # from a marker orphaned by a dead generation (the incident where a
        # marker requested against one gateway stayed "pending" through two
        # successor restarts).
        "predecessor": {
            "pid": os.getpid(),
            "start_time": get_process_start_time(os.getpid()),
        },
    }
    successor_pid = getattr(runner, "_overlap_restart_successor_pid", None)
    if isinstance(successor_pid, int) and not isinstance(successor_pid, bool) and successor_pid > 0:
        payload["successor_pid"] = successor_pid
        successor_start = getattr(runner, "_overlap_restart_successor_start_time", None)
        if isinstance(successor_start, int) and not isinstance(successor_start, bool) and successor_start > 0:
            payload["successor_start_time"] = successor_start
    if restart_hub:
        payload["restart_hub"] = True
    try:
        if path.exists():
            if restart_hub:
                _merge_hub_flag_into_marker(path)
            return True
        atomic_json_write(path, payload, indent=None)
    except Exception as exc:
        logger.warning("Could not record chained gateway restart: %s", exc)
        return False
    logger.info(
        "Chained gateway restart recorded for the pending successor (source=%s)",
        payload["source"],
    )
    return True


def _merge_hub_flag_into_marker(path: Path) -> None:
    """A ``--hub`` request joining an existing chain must not be lost to coalescing."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("restart_hub") is True:
        return
    payload["restart_hub"] = True
    try:
        atomic_json_write(path, payload, indent=None)
    except Exception as exc:
        logger.warning("Could not record hub flag on chained gateway restart: %s", exc)


def chained_restart_status(
    runner: Any,
    *,
    source: Any = None,
    message_id: Optional[str] = None,
    via_service: bool = False,
    restart_hub: bool = False,
) -> Optional[dict[str, Any]]:
    """Return the shared user-restart ACK when this request joined the chain."""
    if not record_chained_restart(
        runner,
        source=source,
        message_id=message_id,
        via_service=via_service,
        restart_hub=restart_hub,
    ):
        return None
    return {
        "status": "chained",
        "active_agents": runner._running_agent_count(),
        "via_service": bool(via_service),
    }


def consume_chained_restart() -> Optional[dict[str, Any]]:
    """Read and unlink the marker before any restart action can occur."""
    path = _restart_chain_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read chained gateway restart marker: %s", exc)
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        # Another authoritative claimant consumed it first.
        return None
    except OSError as exc:
        logger.warning("Could not consume chained gateway restart marker: %s", exc)
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("Discarding invalid chained gateway restart marker: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


async def run_chained_restart(runner: Any) -> bool:
    """Consume one marker after claim, settle startup, then restart this runner."""
    payload = consume_chained_restart()
    if payload is None:
        return False
    await asyncio.sleep(POST_CLAIM_STARTUP_SETTLE_SECONDS)
    if is_truthy_value(payload.get("restart_hub")):
        # This already-up successor stays alive to hand off to the NEXT one:
        # persist the hub bounce marker BEFORE request_restart() so the next
        # successor bounces the hub. Never bounce from a draining process.
        try:
            from gateway.restart_hub import write_hub_pending

            write_hub_pending(_restart_chain_path().parent)
        except Exception as exc:
            logger.warning(
                "restart-hub: could not persist hub bounce marker for chained restart: %s",
                exc,
            )
    try:
        started = runner.request_restart(
            overlap=True,
            via_service=bool(payload.get("via_service", False)),
        )
    except Exception:
        # Consumption already happened: a failed act must never replay on boot.
        logger.exception("Chained gateway restart could not be requested after successor claim")
        return False
    if not started:
        logger.warning("Chained gateway restart was already superseded on the authoritative successor")
        return False
    logger.info("Chained gateway restart requested on the authoritative successor")
    return True


def _marker_names_pid(marker: Mapping[str, Any]) -> bool:
    """True when the marker names the serving pid as its predecessor."""
    predecessor = marker.get("predecessor")
    return isinstance(predecessor, Mapping) and isinstance(
        predecessor.get("pid"), int) and not isinstance(predecessor.get("pid"), bool) \
        and predecessor.get("pid") > 0


def _marker_is_legacy(marker: Mapping[str, Any]) -> bool:
    """A marker recorded before generation stamping: it names no process."""
    successor_pid = marker.get("successor_pid")
    successor_named = isinstance(successor_pid, int) and not isinstance(
        successor_pid, bool) and successor_pid > 0
    return not _marker_names_pid(marker) and not successor_named


def chained_marker_state(
    marker: Any,
    serving_pid: Any,
    serving_start: Any,
) -> str:
    """Classify a peeked chain marker against the process provably serving turns.

    ``"pending"`` — the marker belongs to the serving process's own handoff:
    it names the serving process as the draining predecessor, or as the
    successor that predecessor is draining for.
    ``"stale"`` — the marker carries its generation's identity (this code
    stamps it at record time) and the serving process is provably NEITHER
    member: that generation is dead and nothing will ever act on the marker.
    ``"unproven"`` — legacy marker, or a comparison missing the fingerprints
    to prove either way: claim nothing and keep the pending rendering.

    Shared contract with ``internal/servingidentity`` (Go twin); pinned by
    the shared honesty table.
    """
    if not isinstance(marker, Mapping) or _marker_is_legacy(marker):
        return "unproven"
    if not isinstance(serving_pid, int) or isinstance(serving_pid, bool) or serving_pid <= 0:
        return "unproven"
    predecessor = marker.get("predecessor") if isinstance(marker.get("predecessor"), Mapping) else {}
    for record_pid, record_start in (
        (predecessor.get("pid"), predecessor.get("start_time")),
        (marker.get("successor_pid"), marker.get("successor_start_time")),
    ):
        if record_pid == serving_pid:
            if not record_start or not serving_start:
                return "unproven"
            return "pending" if record_start == serving_start else "stale"
    # The serving process is provably not the marker's predecessor (pid
    # differs) nor its successor: a different live pid is a different
    # process regardless of fingerprints.
    return "stale"


async def settle_chained_restart_after_direct_claim(
    runner: Any,
    *,
    launched_as_overlap_successor: bool,
) -> bool:
    """Settle a leftover chain marker after a DIRECT authority claim.

    ``run_chained_restart`` runs only on the pending-successor claim path
    (``claim_when_predecessor_exits``). When this process claimed authority
    directly — a normal start, ``--replace``, or an overlap successor whose
    predecessor died before the deferred claim — no process will ever consume
    a leftover marker: the successor it was recorded for is gone. Without
    this sweep the marker outlives its generation: status keeps showing a
    chained restart that can never run, and a FUTURE successor would act on
    it spuriously.

    The chain is honored only when the marker provably names THIS process as
    its successor, or — for markers recorded before generation stamping —
    when the overlap-successor launch env proves the drainer spawned this
    process. Every other marker is consumed and discarded as orphaned.
    Never raises: marker trouble must not break the startup.
    """
    from gateway.status import get_process_start_time

    try:
        marker = peek_chained_restart()
        if marker is None:
            return False
        ours = (
            marker.get("successor_pid") == os.getpid()
            and marker.get("successor_start_time") == get_process_start_time(os.getpid())
        )
        if ours or (_marker_is_legacy(marker) and launched_as_overlap_successor):
            logger.info(
                "Chained restart marker honored after a direct authority claim; "
                "requesting the chained restart")
            return await run_chained_restart(runner)
        consumed = consume_chained_restart()
        logger.warning(
            "Cleared an orphaned chained-restart marker left by a dead process "
            "generation (source=%s): this gateway claimed authority directly and is "
            "not the successor that marker was recorded for",
            (consumed or marker).get("source"),
        )
        return False
    except Exception:
        logger.exception(
            "Chained-restart marker sweep failed; leaving the marker in place")
        return False
