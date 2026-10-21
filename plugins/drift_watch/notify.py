"""Best-effort user-visible notifications for drift-watch alerts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def notifications_log_path() -> Path:
    return get_hermes_home() / "drift-watch" / "notifications.log"


def emit_notification(
    message: str,
    *,
    write_text: Callable[[Path, str], None] | None = None,
) -> None:
    if not message:
        return
    path = notifications_log_path()
    writer = write_text or _append_line
    try:
        writer(path, message)
    except Exception as exc:
        logger.debug("drift-watch notification failed (non-fatal): %s", exc)


def _append_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")
