"""CLI for ``hermes claude_viewer {status,enable,disable,reconcile}``."""

from __future__ import annotations

import argparse

from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.claude_viewer.config import (
    load_claude_viewer_config,
    plugin_explicitly_disabled,
)
from plugins.claude_viewer.lifecycle import reconcile_viewer_on_load
from plugins.claude_viewer.port import FOREIGN
from plugins.claude_viewer.systemd import (
    ProbeOutcome,
    ReconcileResult,
    format_status,
    probe_port_state,
    probe_service_is_active,
    probe_service_is_enabled,
    reconcile_service,
    service_unit_path,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_claude_viewer_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended viewer state.

    A port already held by another process is deliberately NOT a failure: a
    healthy viewer is already serving the runs directory, and a foreign
    listener means a second copy could only crash-loop. Stand-down is the
    correct outcome, so only a genuine failure of *our* unit counts — that is
    what keeps a coexisting viewer from failing gateway start.
    """
    if not result.supported:
        return True
    if not result.enabled_known or not result.service_active_known:
        return True
    operational = [
        w
        for w in result.warnings
        if w.startswith("failed to") or w == "viewer unit enabled but not active"
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


def _live_result(cfg: dict) -> ReconcileResult:
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
            port=probe_port_state(cfg["port"], bind=cfg["bind"]),
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
        port=probe_port_state(cfg["port"], bind=cfg["bind"]),
        warnings=tuple(warnings),
        enabled_known=enabled_probe.known,
        service_active_known=active_probe.known,
    )


def cmd_status() -> int:
    cfg = load_claude_viewer_config()
    result = _live_result(cfg)
    print(format_status(result, cfg=cfg))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    return 0


def _save_enabled_flag(enabled: bool) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    delegation = dict(cfg.get("delegation") or {})
    section = dict(delegation.get("claude_viewer") or {})
    section["enabled"] = enabled
    delegation["claude_viewer"] = section
    cfg["delegation"] = delegation
    save_config(cfg)


def cmd_enable() -> int:
    if not platform_supported():
        print(
            "The bundled Claude run viewer requires Linux with systemd; "
            "nothing was installed."
        )
        return 1
    _save_enabled_flag(True)
    result = reconcile_viewer_on_load()
    if result is None:
        result = reconcile_service(load_claude_viewer_config(), enabled=True)
    print(format_status(result))
    if _management_failed(result, want_enabled=True):
        return 1
    print("Claude run viewer installed and started.")
    return 0


def cmd_disable() -> int:
    _save_enabled_flag(False)
    result = reconcile_viewer_on_load()
    if result is None:
        result = reconcile_service(load_claude_viewer_config(), enabled=False)
    if result is not None and _management_failed(result, want_enabled=False):
        print(format_status(result))
        return 1
    print("Claude run viewer disabled; service stopped.")
    return 0


def cmd_reconcile() -> int:
    result = reconcile_viewer_on_load()
    if result is None:
        result = reconcile_service(
            load_claude_viewer_config(), enabled=_effective_enabled()
        )
    print(format_status(result))
    if not result.supported and platform_supported():
        return 1
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="claude_viewer_command")
    subs.add_parser("status", help="Show viewer bind, public URL, and unit state")
    subs.add_parser("enable", help="Install and start the bundled viewer service")
    subs.add_parser("disable", help="Stop and disable the bundled viewer service")
    subs.add_parser(
        "reconcile",
        help="Rewrite the systemd unit idempotently (respects explicit disable)",
    )


def claude_viewer_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "claude_viewer_command", None)
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    print("usage: hermes claude_viewer {status,enable,disable,reconcile}")
    return 2
