"""One-shot restart requests handed from a draining gateway to its successor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

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
    payload = {
        "requested_at": time.time(),
        "source": _requesting_source_summary(source, message_id, kind=request_kind),
        "via_service": bool(via_service),
    }
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
        raw = path.read_text(encoding="utf-8")
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
