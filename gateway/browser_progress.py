"""Live-turn delivery for sanitized browser screenshot events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
from pathlib import Path
from typing import Any, Mapping

from gateway.platforms.base import BasePlatformAdapter
from hermes_constants import get_hermes_home
from tools.browser_progress import (
    BROWSER_PROGRESS_CAPTION,
    discard_browser_progress_event,
    owned_browser_progress_path,
)

logger = logging.getLogger(__name__)
_SUPPORTED_PLATFORMS = frozenset({"discord", "matrix"})
_SEND_TIMEOUT_SECONDS = 20


def browser_progress_supported(source: Any, adapter: Any) -> bool:
    """True only for native Discord/Matrix image upload, not base text fallback."""
    platform = getattr(source, "platform", None)
    platform_name = str(getattr(platform, "value", platform) or "").lower()
    if platform_name not in _SUPPORTED_PLATFORMS or adapter is None:
        return False
    sender = getattr(adapter, "send_image_file", None)
    if not callable(sender):
        return False
    return not (
        isinstance(adapter, BasePlatformAdapter)
        and type(adapter).send_image_file is BasePlatformAdapter.send_image_file
    )


async def deliver_browser_progress(turn_runner: Any, event: Mapping[str, Any]) -> None:
    """Deliver one current-turn image; all transport/validation failures are fail-soft."""
    ctx = turn_runner._ctx
    owned = False
    home: Path | None = None
    try:
        if event.get("session_id") != ctx.session_id:
            return
        home = Path(get_hermes_home())
        image_path = owned_browser_progress_path(event, home)
        if image_path is None:
            return
        owned = True
        gateway = turn_runner._runner
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != event.get("sha256"):
            return
        if not ctx._run_still_current():
            return
        if not browser_progress_supported(ctx.source, gateway._delivery_adapter_for(ctx.source)):
            return

        async def _send() -> Any:
            if not ctx._run_still_current():
                return None
            # Adapter reattachment and relay routing remain source-owned. Never
            # retain a process-global platform adapter across this await boundary.
            adapter = gateway._delivery_adapter_for(ctx.source)
            if not browser_progress_supported(ctx.source, adapter):
                return None
            metadata = dict(ctx._progress_metadata) if ctx._progress_metadata else None
            return await adapter.send_image_file(
                chat_id=ctx.source.chat_id,
                image_path=str(image_path),
                caption=BROWSER_PROGRESS_CAPTION,
                reply_to=ctx._progress_reply_to,
                metadata=metadata,
            )

        await asyncio.wait_for(ctx.deliver_visible(_send), timeout=_SEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("Browser progress image delivery suppressed after %s", type(exc).__name__)
    finally:
        if owned and home is not None:
            discard_browser_progress_event(event, home)


def discard_pending_browser_progress(event_queue: Any) -> None:
    """Delete queued artifacts abandoned by a cancelled or stale turn."""
    if event_queue is None:
        return
    home = Path(get_hermes_home())
    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            return
        if isinstance(event, Mapping) and event.get("type") == "browser.screenshot":
            discard_browser_progress_event(event, home)
