"""``on_gateway_start`` install/reconcile hook for the bundled usage proxy.

Two phases, strictly ordered:

1. **Service reconcile** (systemd): idempotent install/start of this
   profile's unit, standing down when the port is held by anything that does
   not verify as this profile's proxy.
2. **Routing activation** (in-process): only after ``/health`` verifies the
   service id, protocol version, this profile's identity token AND the exact
   route table this reconcile rendered, the route table is registered with
   ``hermes_cli.llm_usage_routes`` and activated for the HTTP-client seams.

Anything short of that leaves routing off — traffic flows direct and stays
unmetered, with the reason surfaced in ``routing_state()`` and CLI status.
There is deliberately no fail-over and no replay: once a request has been
routed, proxy errors surface as errors (a silent direct retry could
double-bill the upstream), and ``Restart=always`` is the recovery path.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from plugins.auto_update.platform import platform_supported
from plugins.llm_usage_proxy.config import (
    load_llm_usage_proxy_config,
    plugin_explicitly_disabled,
)
from plugins.llm_usage_proxy.probe import wait_for_verified_health
from plugins.llm_usage_proxy.routes import proxy_origin
from plugins.llm_usage_proxy.server import DEFAULT_PORT
from plugins.llm_usage_proxy.systemd import (
    ReconcileResult,
    profile_identity,
    reconcile_service,
    resolve_reconcile_routes,
)

logger = logging.getLogger(__name__)

# How long on_gateway_start waits for a verified /health after enabling the
# unit before giving up and leaving traffic direct (visible, unmetered).
HEALTH_WAIT_TIMEOUT_SEC = 8.0


def reconcile_proxy_on_load(
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope=None,
    apply_routing: bool = True,
    environ=None,
) -> ReconcileResult | None:
    """Install/start this profile's proxy when enabled, then wire routing.

    Explicit disablement (``llm_usage_proxy.enabled: false`` or
    ``plugins.disabled``) stops this profile's unit, drops any registered
    route table, and never installs anything. Never raises.

    The route table is resolved once (``resolve_reconcile_routes``: adopted
    from the running proxy's verified ``/health`` under the multiplexed
    gateway, provider-config build otherwise) and that *same* table is what
    the service reconcile classifies the port with, what ``/health`` is
    verified against, and what gets registered — one source, so a proxy
    restarted with a new table generation is re-verified on the next tick
    instead of staying deverified.
    """
    if not platform_supported():
        return None

    cfg = load_llm_usage_proxy_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", False))
    port = int(cfg.get("port") or 0) or DEFAULT_PORT
    identity = profile_identity()
    # Never raises; (None, ...) means no table is resolvable in this process
    # and routing must stand down rather than verify against a guess.
    table, table_source = resolve_reconcile_routes(
        cfg, port=port, identity=identity, environ=environ
    )
    kwargs: dict = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    if environ is not None:
        kwargs["environ"] = environ
    kwargs["routes"] = table
    kwargs["routes_source"] = table_source
    try:
        result = reconcile_service(cfg, enabled=enabled, **kwargs)
    except Exception as exc:
        logger.warning(
            "llm_usage_proxy reconcile skipped: %s", exc, exc_info=True
        )
        result = None

    if not apply_routing:
        return result

    try:
        from hermes_cli import llm_usage_routes as routing

        if not enabled:
            if routing.routing_state()["routes"] or routing.routing_state()["active"]:
                logger.info(
                    "llm_usage_proxy disabled; routing stood down (clients built"
                    " while it was active keep their wrappers but stop"
                    " rerouting immediately)"
                )
            routing.clear_route_table("llm_usage_proxy disabled in config")
            return result

        if table is None:
            routing.deactivate_routing(
                "no route table is resolvable in this process (multiplexed:"
                " no verified proxy on the port to adopt, and provider"
                " endpoints not readable); traffic stays direct and unmetered"
            )
            logger.warning(
                "llm_usage_proxy has no route table on port %s; provider"
                " traffic stays direct and unmetered",
                port,
            )
            return result

        healthy, detail = wait_for_verified_health(
            port,
            timeout=HEALTH_WAIT_TIMEOUT_SEC,
            expect_identity=identity,
            expect_routes=table,
        )
        if not healthy:
            routing.deactivate_routing(
                f"proxy on port {port} did not verify ({detail});"
                " traffic stays direct and unmetered"
            )
            logger.warning(
                "llm_usage_proxy not verified on port %s (%s); provider"
                " traffic stays direct and unmetered",
                port,
                detail,
            )
            return result

        errors = routing.register_route_table(proxy_origin(port), table)
        if errors:
            routing.deactivate_routing(
                f"route table rejected: {errors[0]}; traffic stays direct"
            )
            logger.warning(
                "llm_usage_proxy route table rejected, traffic stays direct"
                " and unmetered: %s",
                "; ".join(errors),
            )
            return result

        routing.activate_routing()
        logger.info(
            "llm_usage_proxy routing active on %s for %d route(s) (%s): %s",
            proxy_origin(port),
            len(table),
            "adopted from proxy /health" if table_source == "health" else "from provider config",
            ", ".join(sorted(table)),
        )
    except Exception as exc:
        logger.warning(
            "llm_usage_proxy routing activation skipped: %s", exc, exc_info=True
        )

    return result
