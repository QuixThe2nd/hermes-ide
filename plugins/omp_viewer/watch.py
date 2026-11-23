"""Watch-link producer for running OMP lane delegations.

Mirrors the Claude Code Agent viewer notice (``tools/claude_agent_tool``):
the lane launcher announces a freshly started run's live page through the
mid-tool status callback as ``"OMP Lane: http://<host>:<port>/#<dir>"``;
platform adapters render the line as a branded card only when the URL
passes the same safety gate the Claude link passes
(``is_allowed_watch_url``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from plugins.omp_viewer.config import plugin_explicitly_disabled
from plugins.omp_viewer.url import is_allowed_watch_url, watch_url
from tools.tool_status import OMP_LANE_VIEWER_STATUS_PREFIX, emit_tool_status

logger = logging.getLogger(__name__)


def lane_watch_status_line(session_dir, *, cfg=None, host: Optional[str] = None) -> Optional[str]:
    """The ``OMP Lane: <url>`` status line, or None when it should stay quiet.

    Quiet means: the plugin is disabled, or the produced URL would fail the
    adapters' own safety gate — announcing a link no adapter may render only
    trains users to ignore the card.
    """
    if plugin_explicitly_disabled():
        return None
    url = watch_url(str(session_dir), cfg=cfg, host=host)
    if not is_allowed_watch_url(url):
        logger.debug("omp lane watch URL failed the safety gate; not announcing")
        return None
    return f"{OMP_LANE_VIEWER_STATUS_PREFIX}{url}"


def emit_lane_watch_notice(session_dir) -> bool:
    """Best-effort emit; unbound dispatch (CLI, cron) is a quiet no-op."""
    try:
        line = lane_watch_status_line(Path(session_dir))
        if line is None:
            return False
        return emit_tool_status(line)
    except Exception:
        logger.debug("omp lane watch notice failed", exc_info=True)
        return False
