"""Process-stable, profile-scoped registry of live browser-sidebar adapters.

WHY THIS MODULE EXISTS
----------------------
The plugin loader imports this plugin under synthetic module names
(``hermes_plugins.browser_sidebar_platform``, plus ``__home_<digest>``
variants for additional Hermes homes) and re-executes the package on every
load, evicting the previous instance from ``sys.modules``. Module-global
state in ``adapter.py`` therefore exists once PER module instance: a
``run_tab_js`` handler registered by one load reads a different (empty)
adapter list than the one a later-constructed adapter appended to. That is
the bug that made ``run_tab_js`` answer ``browser sidebar is not connected``
while chat kept flowing through the same live adapter.

This module is imported only via its canonical absolute path
(``from plugins.platforms.browser_sidebar.runtime import ...``), which the
import system caches in ``sys.modules`` exactly once per process. The
loader's eviction touches only ``hermes_plugins.<slug>*`` names, so every
module instance of this plugin — canonical, loader, per-profile — converges
on ONE registry object.

REGISTRY SEMANTICS
------------------
- Keyed by ``hermes_constants.hermes_home_key()`` — the same identity the
  tool registry itself scopes by. The key is captured inside ``connect()``,
  which the gateway always runs within the owning profile's runtime scope
  (``gateway/run_adapters.py`` wraps construct+connect in
  ``_profile_runtime_scope``), so a tool call dispatched under profile B's
  turn can never be routed to profile A's browser.
- Registration happens on successful connect and is idempotent: a
  reconnect re-registers, and a same-profile replacement adapter overwrites
  the previous entry.
- Unregistration is identity-safe: disconnecting an old adapter never
  evicts a newer adapter that already re-registered under the same key.
- Adapters are held by weak reference, so a discarded adapter cannot leak;
  dead entries are pruned on every access.
- No secrets live here: diagnostics expose module identities, profile
  keys, and counts only.
"""

from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from hermes_constants import hermes_home_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from plugins.platforms.browser_sidebar.adapter import BrowserSidebarAdapter

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: Dict[str, "_Lease"] = {}


@dataclass
class _Lease:
    """One profile's registered adapter plus the loop that owns its socket."""

    home_key: str
    adapter: "weakref.ref[BrowserSidebarAdapter]"
    loop: asyncio.AbstractEventLoop
    generation_id: str = ""
    gateway_pid: int = 0
    hub_pid: int = 0


def _pid_of(adapter: Any) -> int:
    """The gateway process that owns *adapter* (0 when unknown)."""
    try:
        return int(getattr(adapter, "_gateway_pid", 0) or 0) or os.getpid()
    except (TypeError, ValueError):
        return 0


def _prune_locked() -> None:
    """Drop entries whose adapter died without disconnecting (lock held)."""
    for key in [k for k, lease in _REGISTRY.items() if lease.adapter() is None]:
        del _REGISTRY[key]


def register_connected_adapter(
    adapter: "BrowserSidebarAdapter", *, loop: asyncio.AbstractEventLoop,
    home_key: Optional[str] = None,
) -> str:
    """Idempotently record *adapter* as the sidebar adapter for its profile.

    Returns the registry key used (the owning profile's home key).
    """
    key = home_key or hermes_home_key()
    with _REGISTRY_LOCK:
        _prune_locked()
        _REGISTRY[key] = _Lease(
            home_key=key, adapter=weakref.ref(adapter), loop=loop,
            generation_id=str(getattr(adapter, "_generation_id", "") or ""),
            gateway_pid=_pid_of(adapter), hub_pid=int(getattr(adapter, "hub_pid", 0) or 0),
        )
    return key


def unregister_adapter(adapter: "BrowserSidebarAdapter") -> None:
    """Remove *adapter*'s entry — only where the registered entry IS this adapter.

    Identity-safe: a stale adapter disconnecting after a same-profile
    replacement already re-registered must not evict the replacement.
    """
    with _REGISTRY_LOCK:
        for key, lease in list(_REGISTRY.items()):
            if lease.adapter() is adapter:
                del _REGISTRY[key]


def reset() -> None:
    """Drop every entry (test isolation between module-scoped rigs)."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def connected_adapter(
    home_key: Optional[str] = None,
) -> Optional[Tuple["BrowserSidebarAdapter", asyncio.AbstractEventLoop]]:
    """The browser-connected adapter for a profile, with its owning loop.

    ``home_key`` defaults to the CURRENT profile's home key, so the caller
    never silently resolves another profile's adapter. Returns ``None`` when
    no adapter is registered for the profile or its browser is offline.
    """
    key = home_key or hermes_home_key()
    with _REGISTRY_LOCK:
        _prune_locked()
        lease = _REGISTRY.get(key)
    if lease is None:
        return None
    adapter = lease.adapter()
    if adapter is None or not getattr(adapter, "_browser_connected", False):
        return None
    return adapter, lease.loop


def diagnostics(home_key: Optional[str] = None) -> Dict[str, Any]:
    """Actionable, secret-free state for a ``not connected`` error: which
    profiles have registered adapters, whether their browsers are attached,
    and which module instances produced them."""
    key = home_key or hermes_home_key()
    with _REGISTRY_LOCK:
        _prune_locked()
        leases: List[_Lease] = list(_REGISTRY.values())
    entries = []
    for lease in leases:
        adapter = lease.adapter()
        entries.append({
            "profile": lease.home_key,
            "browser_connected": bool(getattr(adapter, "_browser_connected", False)),
            "adapter_module": type(adapter).__module__ if adapter is not None else None,
            "loop_alive": not lease.loop.is_closed(),
            "generation_id": lease.generation_id,
            "gateway_pid": lease.gateway_pid,
            "hub_pid": lease.hub_pid,
        })
    return {
        "requested_profile": key,
        "registered_profiles": [lease.home_key for lease in leases],
        "registered_count": len(entries),
        "entries": entries,
        "runtime_module": __name__,
    }
