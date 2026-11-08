"""Home Server plugin — Discord Home Server provisioning and sync.

Deliberately registers no model tool and no plugin slash command: provisioning
is driven by the gateway's ``/sethomeserver`` (see
``gateway/slash_commands.py``) and re-sync by ``sync_if_due`` below, reachable
from gateway connect or cron via ``run.py``. Adding a core tool for this would
put a rarely-used schema on every API call for no benefit.
"""

from __future__ import annotations

from plugins.fallback_watch import register
from plugins.home_server.core import (  # noqa: F401  (re-exported entry points)
    HomeServerError,
    is_configured,
    reconcile,
    should_sync,
    sync_if_due,
)
