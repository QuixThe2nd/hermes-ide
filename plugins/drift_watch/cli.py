"""CLI for ``hermes drift_watch {run,status,reconcile}``."""

from __future__ import annotations

import argparse

from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.drift_watch.config import load_drift_watch_config, plugin_explicitly_disabled
from plugins.drift_watch.core import (
    ERROR_PREFIX,
    last_capture_dir,
    last_drift_count,
    run_drift_watch,
)
from plugins.drift_watch.notify import emit_notification
from plugins.drift_watch.systemd import (
    ProbeOutcome,
    ReconcileResult,
    format_status,
    linger_warning,
    probe_timer_is_active,
    reconcile_scheduler_on_load,
    reconcile_units,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_drift_watch_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended scheduler state.

    An inert result (no tree configured) is a supported no-op, not a failure.
    """
    if not result.supported:
        return not result.inert
    if not result.enabled_known or not result.timer_active_known:
        return True
    operational_warnings = [
        w
        for w in result.warnings
        if w.startswith("failed to")
        or w == "timer enabled but not active"
    ]
    if want_enabled:
        if not result.enabled:
            return True
        return bool(operational_warnings)
    if result.enabled or result.timer_active:
        return True
    return bool(operational_warnings)


def cmd_run() -> int:
    if plugin_explicitly_disabled():
        return 0
    cfg = load_drift_watch_config()
    if not cfg["enabled"]:
        return 0
    text = run_drift_watch(
        cfg["tree"],
        cfg["state_dir"],
        retain_days=cfg["retain_days"],
        max_captures=cfg["max_captures"],
    )
    if not text:
        return 0
    print(text)
    emit_notification(text)
    return 1 if text.startswith(ERROR_PREFIX) else 0


def cmd_status() -> int:
    cfg = load_drift_watch_config()
    scope = detect_install_scope()
    warnings: list[str] = []
    linger = linger_warning(scope)
    if linger:
        warnings.append(linger)
    drift_count = last_drift_count(cfg["state_dir"])
    capture = last_capture_dir(cfg["state_dir"])
    result = ReconcileResult(
        supported=platform_supported() and scope is not None,
        scope=scope,
        changed=False,
        enabled=_effective_enabled(),
        timer_active=False,
        warnings=tuple(warnings),
    )
    if result.supported and scope is not None:
        active_probe = probe_timer_is_active(scope)
        if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
            warnings.append(
                f"failed to query timer active state: {active_probe.detail or 'unknown error'}"
            )
        result = ReconcileResult(
            supported=True,
            scope=scope,
            changed=False,
            enabled=_effective_enabled(),
            timer_active=active_probe.as_bool,
            warnings=tuple(warnings),
            timer_active_known=active_probe.known,
        )
    print(format_status(result))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    print(f"  Tree: {cfg['tree'] or '(not set — drift watch is inert)'}")
    print(f"  State dir: {cfg['state_dir']}")
    print(f"  Schedule: {cfg['schedule']}")
    print(f"  Retain days: {cfg['retain_days']}")
    print(f"  Max captures: {cfg['max_captures']}")
    print(
        f"  Last inventory drift: "
        f"{drift_count if drift_count is not None else '(no inventory yet)'}"
    )
    print(f"  Last capture: {capture if capture else '(none)'}")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    return 0


def cmd_reconcile() -> int:
    result = reconcile_scheduler_on_load()
    if result is None:
        enabled = _effective_enabled()
        result = reconcile_units(load_drift_watch_config(), enabled=enabled)
    print(format_status(result))
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="drift_watch_command")
    subs.add_parser(
        "run",
        help="Run one drift-watch pass (systemd oneshot entrypoint)",
    )
    subs.add_parser(
        "status",
        help="Show config, timer, and last-capture status",
    )
    subs.add_parser(
        "reconcile",
        help="Rewrite systemd units idempotently (respects explicit disable)",
    )
    return subs


def drift_watch_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "drift_watch_command", None)
    if sub == "run":
        return cmd_run()
    if sub == "status":
        return cmd_status()
    if sub == "reconcile":
        return cmd_reconcile()
    print("usage: hermes drift_watch {run,status,reconcile}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cmd_run())
