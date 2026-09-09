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
)

logger = logging.getLogger(__name__)

# How long on_gateway_start waits for a verified /health after enabling the
# unit before giving up and leaving traffic direct (visible, unmetered).
HEALTH_WAIT_TIMEOUT_SEC = 8.0


def reconcile_proxy_on_plugin_load() -> None:
    """``register()``/plugin-load entry point for non-gateway processes.

    ``discover_plugins()`` (which calls this plugin's ``register()``) runs
    before ``main()`` constructs any provider client, but ``on_gateway_start``
    only fires when a gateway actually starts — a plain ``hermes chat`` never
    starts one, so without this call the CLI would build its SDK clients with
    routing off and its traffic would stay unmetered. Enabled profiles
    therefore reconcile here; the gateway hook re-runs the same idempotent
    lifecycle at gateway start.

    Disabled profiles are left entirely alone: no systemd calls and no
    routing changes (standing them down remains the gateway hook's and the
    disabled-management module's job). Never raises — a broken reconcile
    must not fail plugin load.
    """
    try:
        if plugin_explicitly_disabled():
            return
        if not load_llm_usage_proxy_config().get("enabled", False):
            return
        reconcile_proxy_on_load()
    except Exception:
        logger.warning(
            "llm_usage_proxy plugin-load reconcile failed; provider traffic"
            " stays direct and unmetered",
            exc_info=True,
        )


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
    """
    if not platform_supported():
        return None

    cfg = load_llm_usage_proxy_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", False))
    kwargs: dict = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    if environ is not None:
        kwargs["environ"] = environ
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

        port = int(cfg.get("port") or 0) or DEFAULT_PORT
        identity = profile_identity()
        table = dict(result.routes) if result is not None else {}
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
            "llm_usage_proxy routing active on %s for %d route(s): %s",
            proxy_origin(port),
            len(table),
            ", ".join(sorted(table)),
        )
    except Exception as exc:
        logger.warning(
            "llm_usage_proxy routing activation skipped: %s", exc, exc_info=True
        )

    return result
