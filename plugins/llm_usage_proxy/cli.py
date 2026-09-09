"""CLI for ``hermes llm_usage_proxy {status,enable,disable,reconcile,serve}``."""

from __future__ import annotations

import argparse

from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.llm_usage_proxy.config import (
    CONFIG_SECTION,
    load_llm_usage_proxy_config,
    plugin_explicitly_disabled,
)
from plugins.llm_usage_proxy.lifecycle import reconcile_proxy_on_load
from plugins.llm_usage_proxy.probe import probe_port_state
from plugins.llm_usage_proxy.systemd import (
    ProbeOutcome,
    ReconcileResult,
    format_status,
    profile_identity,
    probe_service_is_active,
    probe_service_is_enabled,
    reconcile_service,
    service_unit_path,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_llm_usage_proxy_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended proxy state.

    A port already held by a non-verifying listener is deliberately NOT a
    failure: this profile's proxy is not the one serving it, and adopting or
    fighting it would break the other owner. Stand-down is the correct
    outcome, so only a genuine failure of *this profile's* unit counts — that
    is what keeps a coexisting proxy from failing gateway start.
    """
    if not result.supported:
        return True
    if not result.enabled_known or not result.service_active_known:
        return True
    operational = [
        w
        for w in result.warnings
        if w.startswith("failed to") or w == "usage proxy unit enabled but not active"
    ]
    if want_enabled and result.port.occupied:
        return bool(operational)
    if want_enabled:
        if not result.enabled or not result.service_active:
            return True
        return bool(operational)
    if result.enabled or result.service_active:
        return True
    return bool(operational)


def _routing_state() -> dict:
    try:
        from hermes_cli.llm_usage_routes import routing_state

        return routing_state()
    except Exception:
        return {"active": False, "reason": "routing seam unavailable"}


def _live_result(cfg: dict, *, environ=None) -> ReconcileResult:
    """Assemble a status-only result without touching unit files."""
    scope = detect_install_scope()
    warnings: list[str] = []
    supported = bool(platform_supported() and scope is not None)
    if not supported:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=probe_port_state(
                cfg["port"],
                expect_identity=profile_identity(),
            ),
        )
    enabled_probe = probe_service_is_enabled(scope)
    active_probe = probe_service_is_active(scope)
    if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(
            f"failed to query enabled state: {enabled_probe.detail or 'unknown error'}"
        )
    if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(
            f"failed to query active state: {active_probe.detail or 'unknown error'}"
        )
    return ReconcileResult(
        supported=True,
        scope=scope,
        changed=False,
        enabled=enabled_probe.as_bool,
        service_active=active_probe.as_bool,
        unit_installed=service_unit_path(scope).is_file(),
        port=probe_port_state(
            cfg["port"],
            expect_identity=profile_identity(),
        ),
        warnings=tuple(warnings),
        enabled_known=enabled_probe.known,
        service_active_known=active_probe.known,
        identity=profile_identity(),
    )


def cmd_status() -> int:
    cfg = load_llm_usage_proxy_config()
    result = _live_result(cfg)
    print(format_status(result, cfg=cfg, routing=_routing_state()))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    if not cfg["enabled"]:
        print(
            "  Routing is opt-in per profile: `hermes llm_usage_proxy enable`"
            " writes llm_usage_proxy.enabled: true into this profile's config."
        )
    return 0


def _save_enabled_flag(enabled: bool) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get(CONFIG_SECTION) or {})
    section["enabled"] = enabled
    cfg[CONFIG_SECTION] = section
    save_config(cfg)


def cmd_enable() -> int:
    if not platform_supported():
        print(
            "The bundled LLM usage proxy requires Linux with systemd; "
            "nothing was installed."
        )
        return 1
    _save_enabled_flag(True)
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(load_llm_usage_proxy_config(), enabled=True)
    print(format_status(result, routing=_routing_state()))
    if _management_failed(result, want_enabled=True):
        return 1
    print(
        "LLM usage proxy installed and started. Newly built model clients"
        " route through it; clients built before this change keep their"
        " direct transports until they are rebuilt (a gateway restart"
        " guarantees it)."
    )
    return 0


def cmd_disable() -> int:
    _save_enabled_flag(False)
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(load_llm_usage_proxy_config(), enabled=False)
    if result is not None and _management_failed(result, want_enabled=False):
        print(format_status(result, routing=_routing_state()))
        return 1
    print(
        "LLM usage proxy disabled; service stopped and in-process routing"
        " stood down. Routed requests stop immediately; clients that were"
        " talking to the proxy will reconnect directly on their next"
        " request."
    )
    return 0


def cmd_reconcile() -> int:
    result = reconcile_proxy_on_load()
    if result is None:
        result = reconcile_service(
            load_llm_usage_proxy_config(), enabled=_effective_enabled()
        )
    print(format_status(result, routing=_routing_state()))
    if not result.supported and platform_supported():
        return 1
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the proxy in the foreground (no systemd required)."""
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.server import main as server_main

    cfg = load_llm_usage_proxy_config()
    port = int(getattr(args, "port", None) or 0) or int(cfg["port"])
    argv = ["--port", str(port), "--identity", profile_identity()]
    for name, base in sorted(build_route_table(cfg).items()):
        argv.extend(("--upstream", f"{name}={base}"))
    return server_main(argv)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="llm_usage_proxy_command")
    subs.add_parser("status", help="Show proxy bind, SQLite path, and unit state")
    subs.add_parser("enable", help="Install and start the bundled proxy service")
    subs.add_parser("disable", help="Stop and disable the bundled proxy service")
    subs.add_parser(
        "reconcile",
        help="Rewrite the systemd unit idempotently (respects explicit disable)",
    )
    serve = subs.add_parser(
        "serve", help="Run the proxy in the foreground (no systemd)"
    )
    serve.add_argument("--port", type=int, default=None)


def llm_usage_proxy_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "llm_usage_proxy_command", None)
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    if sub == "serve":
        return cmd_serve(args)
    print(
        "usage: hermes llm_usage_proxy {status,enable,disable,reconcile,serve}"
    )
    return 2
