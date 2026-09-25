"""CLI for ``hermes auto_update {status,enable,disable,reconcile,run,activate}``.

``activate`` is the fail-closed half of the two-phase flow: it restarts the
fleet onto a prepared update only when Hermes is idle *and* the prepared
generation can be strictly proven (marker, bound receipt, and checkout SHA
all agreeing) — re-validated under the stock updater lock, with the fleet
re-inspected after any restart before the obligation is cleared. Anything it
cannot prove is a nonzero exit that keeps the obligation, never a success.
"""

from __future__ import annotations

import argparse
import sys

from hermes_constants import display_hermes_home

from plugins.auto_update.config import load_auto_update_config, plugin_explicitly_disabled
from plugins.auto_update.platform import detect_install_scope, platform_supported
from plugins.auto_update.lifecycle import reconcile_scheduler_on_load
from plugins.auto_update.runner import run_scheduled_update
from plugins.auto_update.systemd import (
    ProbeOutcome,
    ReconcileResult,
    format_status,
    linger_warning,
    probe_timer_is_active,
    reconcile_units,
)


def _effective_enabled() -> bool:
    return not plugin_explicitly_disabled() and load_auto_update_config()["enabled"]


def _management_failed(result: ReconcileResult, *, want_enabled: bool) -> bool:
    """True when reconcile did not achieve the intended scheduler state."""
    if not result.supported:
        return True
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


def cmd_status() -> int:
    cfg = load_auto_update_config()
    scope = detect_install_scope()
    warnings: list[str] = []
    linger = linger_warning(scope)
    if linger:
        warnings.append(linger)
    result = ReconcileResult(
        supported=platform_supported() and scope is not None,
        scope=scope,
        changed=False,
        enabled=_effective_enabled(),
        timer_active=False,
        legacy=(),
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
            legacy=(),
            warnings=tuple(warnings),
            timer_active_known=active_probe.known,
        )
    print(format_status(result))
    print(f"  Config enabled: {'yes' if cfg['enabled'] else 'no'}")
    print(f"  Idle minutes: {cfg['idle_minutes']}")
    print(f"  Schedule: {cfg['schedule']}")
    print(f"  Randomized delay: {cfg['randomized_delay_sec']}s")
    if plugin_explicitly_disabled():
        print("  Explicit disable: yes (config/plugins.disabled)")
    return 0


def _save_enabled_flag(enabled: bool) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("auto_update") or {})
    section["enabled"] = enabled
    cfg["auto_update"] = section
    save_config(cfg)


def cmd_enable() -> int:
    if not platform_supported():
        print(
            "Hermes auto-update requires Linux with systemd; nothing was installed."
        )
        return 1
    _save_enabled_flag(True)
    result = reconcile_scheduler_on_load()
    if result is None:
        result = reconcile_units(load_auto_update_config(), enabled=True)
    print(format_status(result))
    if _management_failed(result, want_enabled=True):
        return 1
    print(f"Scheduler installed under {display_hermes_home()}.")
    return 0


def cmd_disable() -> int:
    _save_enabled_flag(False)
    result = reconcile_scheduler_on_load()
    if result is None:
        result = reconcile_units(load_auto_update_config(), enabled=False)
    if result is not None and _management_failed(result, want_enabled=False):
        print(format_status(result))
        return 1
    print("Hermes auto-update disabled; timer stopped.")
    return 0


def cmd_reconcile() -> int:
    result = reconcile_scheduler_on_load()
    if result is None:
        enabled = _effective_enabled()
        result = reconcile_units(load_auto_update_config(), enabled=enabled)
    print(format_status(result))
    if not result.supported and platform_supported():
        return 1
    if _management_failed(result, want_enabled=_effective_enabled()):
        return 1
    return 0


def cmd_run() -> int:
    outcome = run_scheduled_update()
    if outcome.reason != "disabled":
        print(outcome.reason)
    return 0 if outcome.code == 0 else outcome.code


def cmd_activate() -> int:
    """Finish a prepared update — but only when Hermes is idle.

    Phase B of a scheduler tick, dispatched by ``run_scheduled_update`` in a
    FRESH process. That is what makes the import below safe: this interpreter
    loads the code the prepare phase just pulled, while the parent tick (still
    running pre-pull modules) never touches it.

    Contract:

    - nothing pending → silent no-op, exit 0;
    - pending without proof the preparation finished → exit 1, nothing
      restarted. The generic marker is written as soon as HEAD advances,
      i.e. before dependency sync / build / migration, so a preparation
      that failed late leaves exactly this shape and the next (up-to-date)
      tick must not restart the fleet onto it. Only a ``--defer-restart``
      run that finished every preparation step — and durably published its
      prepared generation into ``fleet_restart_prepared`` — leaves a
      record that parses;
    - pending, prepared, but Hermes is busy → exit 0 and leave the marker, so
      the prepared update waits for a later tick (or a manual ``/restart``);
    - pending, prepared, and idle → take the SAME lock a stock/manual update
      holds, re-check idleness, then the strict activation: every piece of
      durable state (marker, bound receipt, HEAD, plan, live fleet) is
      re-read and re-validated under that lock, so no updater can move HEAD
      or the generation between the inspection and the restart. Lock
      contention is a retryable non-success (exit 2) that preserves the
      obligation.
    """
    from hermes_cli import update_cmd

    if not update_cmd._pending_fleet_restart_needed():
        return 0

    if not update_cmd._fleet_restart_pending_prepared():
        print("⚠ Update is pending but was never fully prepared —")
        print("  not activating it. Run `hermes update` to finish (or repair)")
        print("  the preparation first.")
        return 1

    from plugins.auto_update.idle import evaluate_idle

    settings = load_auto_update_config()

    def _busy() -> int:
        snapshot = evaluate_idle(idle_minutes=int(settings["idle_minutes"]))
        if snapshot.idle:
            return 0
        blocker = snapshot.blockers[0].code if snapshot.blockers else "busy"
        print(f"→ Hermes is busy ({blocker}) — prepared update stays pending.")
        return 1

    # Idle FIRST, deliberately: waiting for Hermes to go quiet can take whole
    # ticks, and the updater lock must never be held across that wait.
    if _busy():
        return 0

    from hermes_cli.update_lock import (
        UPDATE_EXIT_CONCURRENT,
        UpdateLock,
        describe_holder,
    )

    lock = UpdateLock()
    if not lock.acquire():
        print(describe_holder(lock.holder))
        return UPDATE_EXIT_CONCURRENT
    try:
        # Re-check under the lock: the fleet may have gone busy (or idle)
        # while we were acquiring it.
        if _busy():
            return 0
        # Re-reads and strictly re-validates the marker, its bound receipt,
        # the checkout HEAD, the runtime plan and the live fleet before it
        # restarts anything — and clears the obligation only when the fleet
        # is demonstrably serving the prepared generation.
        return update_cmd._activate_pending_fleet_restart_strict()
    finally:
        lock.release()


def register_management_cli(subparser: argparse.ArgumentParser):
    """Register management subcommands only (no ``run`` oneshot entrypoint)."""
    subs = subparser.add_subparsers(dest="auto_update_command")
    subs.add_parser("status", help="Show scheduler and config status")
    subs.add_parser("enable", help="Enable unattended updates and install the timer")
    subs.add_parser("disable", help="Disable unattended updates and stop the timer")
    subs.add_parser(
        "reconcile",
        help="Rewrite systemd units idempotently (respects explicit disable)",
    )
    return subs


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = register_management_cli(subparser)
    subs.add_parser(
        "run",
        help="Run one scheduled update attempt (systemd oneshot entrypoint)",
    )
    subs.add_parser(
        "activate",
        help=(
            "Restart the fleet onto a fully prepared update, but only when idle"
        ),
    )


def auto_update_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "auto_update_command", None)
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    if sub == "run":
        return cmd_run()
    if sub == "activate":
        return cmd_activate()
    print("usage: hermes auto_update {status,enable,disable,reconcile,run,activate}")
    return 2


def management_auto_update_command(args: argparse.Namespace) -> int:
    """Handler for disabled-management CLI — no ``run`` oneshot entrypoint."""
    sub = getattr(args, "auto_update_command", None)
    if sub == "run":
        print("usage: hermes auto_update {status,enable,disable,reconcile}")
        return 2
    if sub == "status":
        return cmd_status()
    if sub == "enable":
        return cmd_enable()
    if sub == "disable":
        return cmd_disable()
    if sub == "reconcile":
        return cmd_reconcile()
    print("usage: hermes auto_update {status,enable,disable,reconcile}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cmd_run())
