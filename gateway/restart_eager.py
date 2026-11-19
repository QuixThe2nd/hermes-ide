"""Eagerly resolve the imports the late-life restart paths use lazily.

Incident (gateway.log 2026-11-19 13:13): a gateway generation booted on
build c83f98b6 lived on while the checkout fast-forwarded underneath it to
#844, which deleted ``gateway/restart_chain.py``; hours later its overlap
claim ran ``from gateway.restart_chain import ...`` from the old in-memory
code and the generation died with ModuleNotFoundError. The generation
array (#844) keeps old-code generations alive for hours beside an updated
tree, so ANY function-level import executed late in a process's life can
hit a module the update renamed, deleted, or swapped for an incompatible
version.

Importing those modules once here — during gateway startup, before the
overlap wait — pins each one in ``sys.modules``, so every later
function-level ``from X import y`` on the restart / overlap-claim /
handover / drain / successor-spawn / shutdown paths resolves against the
in-memory (process-start) tree instead of touching disk. The imports stay
lazy at their call sites: most exist to break cycles with ``gateway.run``
(hoisting them would restructure the facade/sibling graph); this sibling
is the single owner of the eager resolution.

Best-effort by design: preloading only ADDS ``sys.modules`` entries. A
module that cannot import at startup could not have imported late either,
and each late path's own error handling still applies.
"""

from __future__ import annotations

import importlib
import logging
import sys

logger = logging.getLogger(__name__)

# Every module the restart / overlap-claim / handover / drain /
# successor-spawn / shutdown paths import inside functions:
# gateway/overlap_restart.py, gateway/restart.py, gateway/serving_identity.py,
# gateway/control_socket.py, gateway/lifecycle_ledger.py, the claim and stop
# paths of gateway/run.py (+ its run_shutdown.py mixin), the
# gateway_restart tool, the discord/matrix hub-client overlap mixins, and
# the modules those reach into lazily on the same paths. Keep in sync with
# those call sites.
_LATE_LIFE_MODULES: tuple[str, ...] = (
    # Overlap claim and generation arbitration.
    "gateway.control_socket",
    "gateway.generations",
    "gateway.overlap_registry",
    "gateway.overlap_restart",
    "gateway.restart_hub",
    "gateway.serving_identity",
    "gateway.systemd_notify",
    # Drain, shutdown and successor handover.
    "gateway.drain_control",
    "gateway.lifecycle_ledger",
    "gateway.memory_status",
    "gateway.restart",
    "gateway.restart_channel_rename",
    "gateway.restart_drain_status",
    "gateway.restart_loop_guard",
    "gateway.scale_to_zero",
    "gateway.session",
    "gateway.session_context",
    "gateway.shutdown_flush",
    "gateway.shutdown_forensics",
    "gateway.shutdown_watchdog",
    "gateway.status",
    "gateway.warning_notifications",
    "gateway.relay",
    "gateway.platforms.base",
    # Modules those paths reach into lazily.
    "agent.auxiliary_client",
    "agent.monitoring.gateway_health",
    "agent.secret_scope",
    "cron.jobs",
    "cron.scheduler",
    "hermes_cli._subprocess_compat",
    "hermes_cli.active_sessions",
    "hermes_cli.config_effective",
    "hermes_cli.lifecycle",
    "hermes_cli.process_identity",
    "hermes_cli.profiles",
    "hermes_cli.version_info",
    "hermes_logging",
    "hermes_state",
    "hermes_state_common",
    "hermes_state_registry",
    "plugins.gateway_restart.tool",
    "plugins.platforms.discord.hub",
    "plugins.platforms.discord.hub_vault_prompts",
    "plugins.platforms.discord.overlap_spawn",
    "tools.async_delegation",
    "tools.browser_tool_lifecycle",
    "tools.environments.local",
    "tools.process_registry",
    "tools.terminal_tool",
)


def preload_late_life_imports() -> None:
    """Pin the late-life restart-path modules in ``sys.modules``.

    Called once during gateway startup, before the overlap wait. Modules
    ``gateway.run``'s own import graph already loaded are skipped; a module
    that fails to import is logged and skipped — it would have failed at
    its late call site anyway, where the existing error handling applies.
    Never raises.
    """
    failed: list[str] = []
    for name in _LATE_LIFE_MODULES:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — preload must never kill boot
            failed.append(f"{name}: {exc!r}")
    if failed:
        logger.warning(
            "Late-life restart-path modules not resolvable at startup "
            "(late paths stay lazy for them): %s",
            "; ".join(failed),
        )
