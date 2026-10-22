"""Fork-owned helpers for the ``gateway/run.py`` boot zone.

The fork converged its boot zone (end of ``class GatewayRunner`` through ``main()``)
onto upstream's helper-function structure. Upstream defines ``_best_effort`` earlier
in its own ``run.py`` (outside the boot zone); this tree never grew it, so the
upstream body lives here and is imported at the zone head.

This module must stay free of ``gateway.run`` imports.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _best_effort(fn: Callable[[], Any], debug_msg: Optional[str] = None) -> Any:
    """Call ``fn``; return None on any Exception (debug-logged via ``debug_msg`` ``%s`` if given)."""
    try:
        return fn()
    except Exception as exc:
        if debug_msg:
            logger.debug(debug_msg, exc)
        return None
