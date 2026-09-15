"""Gateway fleet restart + post-update verification for ``hermes update``.

Split out of ``hermes_cli/update_cmd.py``; every name is re-imported there so
``hermes_cli.update_cmd.<name>`` keeps resolving/monkeypatching. Origin helpers are
imported lazily inside each function (no import cycle; test patches stay effective).
"""

import logging
from contextlib import suppress
import os
import re
import uuid
import subprocess
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hermes_cli.durable_state import durable_publish_bytes
from hermes_cli.update_cmd_common import _best_effort
from hermes_cli.update_inventory import _gateway_service_matches_profile

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.update_cmd")

# Under HERMES_HOME (not next to the venv): records the fleet-restart obligation
# after a pull advanced HEAD; cleared only when the restart completes or nothing ran.
# The existing ``.update-incomplete`` / ``.lazy-refresh-incomplete`` markers gate dependency/venv repair;
# this one is the fleet-restart obligation after a git pull that advanced HEAD (#95294).
_FLEET_RESTART_PENDING_NAME = "fleet_restart_pending"

_FRESH_RESTART_SUPERVISORS = frozenset({"systemd", "launchd", "service", "s6"})

_SYSTEMD_SCOPES = (("user", ["systemctl", "--user"]), ("system", ["systemctl"]))
_LIST_GATEWAY_UNITS = ["list-units", "hermes-gateway*", "hermes-serve*", "hermes-dashboard*",
                       "--plain", "--no-legend", "--no-pager"]  # dashboard: serve-only catch-up (fork P1 B)


def _write_gateway_update_exit_code(ok: bool) -> None:
    from hermes_cli.update_cmd import get_hermes_home
    path = get_hermes_home() / ".update_exit_code"
    with suppress(OSError):
        path.write_text("0" if ok else "1", encoding="utf-8")


def _fleet_restart_pending_marker_path() -> Path:
    """HERMES_HOME breadcrumb for a pull that has not yet restarted the fleet."""
    from hermes_cli.update_cmd import get_hermes_home
    return get_hermes_home() / _FLEET_RESTART_PENDING_NAME


def _write_fleet_restart_pending_marker(*, expected_sha: str = "") -> bool:
    """Drop the pull→restart obligation breadcrumb. Never raises.

    Returns whether the breadcrumb is durable on disk (fork). This writes the *obligation only* —
    HEAD advanced and running runtimes still serve the old code — with no claim about preparation
    state, and never touches the authoritative prepared record: a re-prepare that fails or times out
    after this write leaves a previous completed preparation's record byte-identical. The activatable
    ``prepared`` record is published separately, and only by a deferred run whose whole preparation
    transaction succeeded (see :func:`_publish_prepared_generation`).
    """
    from hermes_cli.update_cmd import _m
    path = _fleet_restart_pending_marker_path()
    if _m()._pytest_owns_live_checkout(path.parent):
        logger.debug("Skipping fleet-restart-pending marker under pytest (live checkout)")
        return True
    lines = [f"started={_time.time()}", f"pid={os.getpid()}"]
    if expected_sha:
        lines.append(f"expected_sha={expected_sha}")
    try:
        # Resolve through the origin module: fork-era tests patch
        # ``update_cmd._atomic_write_text`` to simulate an undurable write.
        _origin()._atomic_write_text(path, "\n".join(lines) + "\n")
    except OSError as exc:
        logger.debug("Could not write fleet-restart-pending marker: %s", exc)
        return False
    return True


def _clear_fleet_restart_pending_marker() -> None:
    """Remove the pull→restart obligation state. Never raises.

    Stock/manual recovery semantics (fork): once the stock restart machinery has discharged the
    obligation (or the live fleet demonstrably already serves the checkout), both the generic
    breadcrumb and any prepared generation bound to it are moot, so both go. Strict activation never
    uses this — it clears through :func:`_clear_prepared_generation_strict` only after post-restart
    verification.
    """
    from hermes_cli.update_cmd import _m
    _m()._clear_marker_file(_fleet_restart_pending_marker_path(), label="fleet-restart-pending")
    _m()._clear_marker_file(_fleet_restart_prepared_path(), label="fleet-restart-prepared")


def _current_checkout_sha() -> str | None:
    """Current on-disk checkout HEAD, or None if it cannot be resolved."""
    from hermes_cli.update_cmd import _capture_head_sha, _m
    try:
        from hermes_cli.build_info import get_code_identity
        sha = (get_code_identity(refresh=True) or {}).get("sha")
        return str(sha) if sha else None
    except Exception:
        return _capture_head_sha(["git"], _m().PROJECT_ROOT)


def _receipt_looks_unfinished(receipt: dict) -> bool:
    """True when *receipt* is from an update that did not finish cleanly.

    The command boundary stamps a ``stop_reason`` on every receipt, including clean
    ones (``completed at command boundary``, ``sys.exit(0)``); it must not make a
    successful receipt look unfinished, or the next ``hermes update`` retriggers
    ``fleet_restart_pending`` from pre-pull plan SHAs (#98022).
    """
    exit_code = receipt.get("exit_code")
    outcome = receipt.get("outcome")
    if exit_code not in (0, None) or outcome in ("failed", "partial", "running"):
        return True
    gateway_restart = receipt.get("gateway_restart")
    if isinstance(gateway_restart, dict) and gateway_restart.get("incomplete"):
        return True
    # A stop_reason alone (update_contract refusals: outcome="refused", no exit_code)
    # counts only when nothing else vouched for success.
    succeeded = exit_code == 0 or outcome == "success"
    return bool(receipt.get("stop_reason")) and not succeeded


def _receipt_reports_stale_runtime(expected_sha: str | None = None) -> bool:
    """True when ``update_receipts/latest.json`` records a runtime SHA skew.

    Prefer the post-restart ``fleet`` matrix. ``plan.runtimes[].code_sha`` is captured
    *before* the pull, so a finished update's plan always looks stale and must not
    retrigger a restart; consult it only for an unfinished receipt.

    See #95294.
    """
    from hermes_cli.update_cmd import _current_checkout_sha
    try:
        from hermes_cli.update_receipt import read_latest_receipt
        receipt = read_latest_receipt()
    except Exception:
        receipt = None
    if not isinstance(receipt, dict):
        return False
    expected_sha = expected_sha or _current_checkout_sha()
    if not expected_sha:
        return False

    def _sha_mismatch(code_sha) -> bool:
        return bool(code_sha) and str(code_sha) != str(expected_sha)

    fleet = receipt.get("fleet")
    if isinstance(fleet, list) and fleet:
        return any(
            isinstance(entry, dict)
            and (entry.get("state") == "stale" or _sha_mismatch(entry.get("code_sha")))
            for entry in fleet
        )

    if not _receipt_looks_unfinished(receipt):
        return False
    plan = receipt.get("plan")
    if not isinstance(plan, dict):
        return False
    return any(
        isinstance(runtime, dict) and _sha_mismatch(runtime.get("code_sha"))
        for runtime in plan.get("runtimes") or []
    )


def _live_fleet_covers_receipt(expected_sha: str | None) -> bool:
    """Require current successors for every recorded runtime, not just any live row.

    A PID changes on restart; the stable identity is (runtime kind, profile).
    The gateway matrix cannot vouch for serve/dashboard or unidentified runtimes.
    Keep the historical receipt intact: a manual restart is not a successful update.
    """
    if not expected_sha:
        return False
    from hermes_cli.update_receipt import collect_fleet_versions, read_latest_receipt

    try:
        receipt = read_latest_receipt() or {}
        plan = receipt.get("plan") or {}
        runtimes = plan.get("runtimes") or []
        recorded_fleet = receipt.get("fleet") or []
        owed = set()
        entries: list[tuple[object, str | None]] = [(entry, None) for entry in runtimes]
        entries.extend((entry, "gateway") for entry in recorded_fleet)
        for entry, default_kind in entries:
            if not isinstance(entry, dict):
                return False
            kind = entry.get("kind", default_kind)
            profile = entry.get("profile")
            if kind != "gateway" or not profile or profile == "unknown":
                return False
            owed.add((kind, profile))
        if not owed:
            return False
        fleet = collect_fleet_versions()
        if not fleet or any(
            row.get("state") != "current" or row.get("code_sha") != expected_sha
            for row in fleet
        ):
            return False
        return owed <= {("gateway", row.get("profile")) for row in fleet}
    except Exception as exc:
        logger.debug("Could not reconcile pending fleet identities: %s", exc)
        return False


def _pending_fleet_restart_needed() -> bool:
    """Reconcile old restart obligations against current, identity-matched gateways."""
    from hermes_cli.update_cmd import _current_checkout_sha

    # The marker has no runtime inventory and may belong to a newer, killed update
    # than latest.json. An older receipt cannot discharge that unknown obligation.
    with suppress(OSError):
        if _fleet_restart_pending_marker_path().is_file():
            return True
        # A published prepared generation is itself a restart obligation, even if the
        # generic breadcrumb alongside it was lost (fork).
        if _fleet_restart_prepared_path().is_file():
            return True
    if not _receipt_reports_stale_runtime():
        return False
    return not _live_fleet_covers_receipt(_current_checkout_sha())


def _warn_pending_fleet_restart(*, startup: bool = False) -> None:
    """Print the specific interrupted-update fleet-restart warning."""
    stream = sys.stderr if startup else sys.stdout
    print("⚠ A previous `hermes update` pulled new code but did not restart running gateways.", file=stream)
    print("  Gateways may still be serving pre-update modules (mixed sys.modules).", file=stream)
    if startup:
        print("  Run `hermes update` or `hermes gateway restart`.", file=stream)


def _warn_pending_fleet_restart_on_startup() -> None:
    """Cheap CLI-startup hint. Never restarts; never raises."""
    with suppress(Exception):
        if _pending_fleet_restart_needed():
            _warn_pending_fleet_restart(startup=True)


def _systemd_gateway_unit_listings(on_list_timeout=None):
    """Yield ``(scope, scope_cmd, list-units CompletedProcess)`` per systemd scope that answered.

    A missing systemctl skips the scope silently; a listing timeout skips it after
    ``on_list_timeout(scope, exc)`` (when given) so the other scope is still processed.
    """
    for scope, scope_cmd in _SYSTEMD_SCOPES:
        try:
            result = _systemctl(scope_cmd + _LIST_GATEWAY_UNITS, timeout=10)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            if on_list_timeout is not None:
                on_list_timeout(scope, exc)
            continue
        yield scope, scope_cmd, result


def _needs_sudo(scope: str) -> bool:
    return (
        scope == "system"
        and hasattr(os, "geteuid")
        and os.geteuid() != 0  # windows-footgun: ok — systemd path, Linux-only
    )


def _restart_systemd_gateway_units_best_effort(failed: list, listings) -> None:
    """Best-effort ``systemctl restart`` of every hermes-gateway/serve unit."""
    answered = set()
    for scope, scope_cmd, result in listings:
        answered.add(scope)
        if result.returncode != 0:
            failed.append(f"systemd-{scope} (listing failed)")
            continue

        def process_unit(svc_name: str, _scope=scope, _cmd=scope_cmd) -> None:
            manage_cmd = list(_cmd) + ["--no-ask-password"]
            if _needs_sudo(_scope):
                manage_cmd = ["sudo", "-n"] + manage_cmd
            result = _systemctl_reset_and_restart(manage_cmd, svc_name, scope_cmd=_cmd)
            if result.returncode != 0 or not _wait_for_service_active(_cmd, svc_name):
                failed.append(svc_name)

        _for_each_systemd_gateway_unit(
            result.stdout,
            process_unit=process_unit,
            on_unit_timeout=lambda svc_name, exc: failed.append(svc_name),
        )
    # A timeout or missing executable is not an empty scope.
    failed.extend(f"systemd-{scope} (listing unavailable)" for scope, _ in _SYSTEMD_SCOPES if scope not in answered)


# Fork (Codex P1 B): unit-pattern sweeps for fleets with no gateway PIDs at
# all. Unlike the gateway sweep above, an unanswered/unavailable scope here is
# skipped, never a failure — a host without a user manager is a normal shape,
# and this path only reports what it actually found and restarted.
_GATEWAY_SERVE_UNIT_PATTERNS = ("hermes-gateway*", "hermes-serve*")
_SERVE_DASHBOARD_UNIT_PATTERNS = ("hermes-serve*", "hermes-dashboard*")


def _restart_systemd_units_best_effort(
    failed: list,
    restarted: Optional[list] = None,
    *,
    patterns: tuple = _GATEWAY_SERVE_UNIT_PATTERNS,
) -> bool:
    """Best-effort ``systemctl restart`` of the hermes units matching
    *patterns*, in both the user and the system scope.

    Returns ``True`` when at least one unit was found (whether its restart
    then succeeded or timed out into *failed*), ``False`` when no matching
    unit exists in either scope. *restarted*, when given, collects every
    unit a restart was attempted for, in discovery order — the caller's
    evidence that the enumeration matched something at all.
    """
    found_any = False
    for scope, scope_cmd in _SYSTEMD_SCOPES:
        try:
            result = _systemctl(
                scope_cmd
                + [
                    "list-units",
                    *patterns,
                    "--plain",
                    "--no-legend",
                    "--no-pager",
                ],
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue

        def process_unit(svc_name: str, _scope=scope, _cmd=scope_cmd) -> None:
            nonlocal found_any
            found_any = True
            if restarted is not None:
                restarted.append(svc_name)
            restart_cmd = list(_cmd) + ["--no-ask-password", "restart", svc_name]
            if _needs_sudo(_scope):
                restart_cmd = ["sudo", "-n"] + restart_cmd
            _systemctl(restart_cmd, timeout=30)

        def on_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
            failed.append(svc_name)

        _for_each_systemd_gateway_unit(
            result.stdout,
            process_unit=process_unit,
            on_unit_timeout=on_timeout,
        )
    return found_any


def _run_pending_fleet_restart() -> bool:
    """Catch-up restart for gateways left on pre-update code. Never raises.

    True when all discovered targets recovered (or none exist); False if incomplete.

    See #95294.
    """
    from hermes_cli.update_cmd import _m
    print("→ Restarting gateways left on pre-update code...")
    with suppress(Exception):
        _m()._purge_stale_hermes_modules()
    # Warn if legacy Hermes gateway unit files are still installed. When both hermes.service (from a
    # pre-rename install) and the current hermes-gateway.service are enabled, they SIGTERM-fight for the
    # same bot token (see PR #11909). Flagging here means every `hermes update` surfaces the issue until the
    # user migrates.
    try:
        from hermes_cli.gateway import (
            find_gateway_pids, is_macos, is_windows, kill_gateway_processes, supports_systemd_services,
            _wait_for_gateway_exit,
        )
    except Exception as exc:
        _warn_gateway_restart_phase_aborted(exc, None)
        return False

    try:
        pids = list(find_gateway_pids(all_profiles=True))
    except Exception as exc:
        logger.debug("Pending fleet restart: gateway probe failed: %s", exc)
        pids = None

    if pids == [] and supports_systemd_services():
        # Codex P1 B: a serve-only fleet has no gateway PIDs, but its
        # systemd-managed serve/dashboard backends still run pre-update code.
        # Treating "no gateways" as done left activation verifying against
        # the ORIGINAL serve PID forever. Probe every scope tolerantly first:
        # a fleet with gateway units — or one whose scopes cannot answer at
        # all — goes through the conservative general path below; only a
        # fleet provably holding serve/dashboard units alone (or nothing) is
        # settled right here.
        scope_answered = False
        gateway_units_present = False
        for _scope, _scope_cmd in _SYSTEMD_SCOPES:
            try:
                _listing = _systemctl(_scope_cmd + _LIST_GATEWAY_UNITS, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if _listing.returncode != 0:
                continue
            scope_answered = True
            for _line in (_listing.stdout or "").strip().splitlines():
                _unit = _line.split()[0] if _line.split() else ""
                if _unit == "hermes-gateway.service" or _unit.startswith("hermes-gateway-"):
                    gateway_units_present = True
                    break
            if gateway_units_present:
                break
        if scope_answered and not gateway_units_present:
            restarted_units: list = []
            failed_units: list = []
            found_units = _restart_systemd_units_best_effort(
                failed_units,
                restarted_units,
                patterns=_SERVE_DASHBOARD_UNIT_PATTERNS,
            )
            if failed_units:
                _warn_incomplete_gateway_fleet_restart(failed_units)
                return False
            if found_units:
                print(
                    "  ✓ No running gateways — restarted"
                    f" {len(restarted_units)} systemd serve/dashboard"
                    " unit(s)."
                )
                return True
            print("  ✓ No running gateways — nothing to restart.")
            return True

    failed: list = []
    try:
        # Snapshot before stopping: Restart=no units can disappear from list-units on a clean exit.
        systemd_listings = list(_systemd_gateway_unit_listings()) if supports_systemd_services() else None
        # Stop old processes before supervisor recovery, never its freshly verified workers.
        if pids != []:
            try:
                leftover = list(find_gateway_pids(all_profiles=True))
            except Exception:
                leftover = list(pids or [])
            if leftover:
                with _best_effort('Pending fleet restart: PID stop failed: %s'):
                    kill_gateway_processes(all_profiles=True)
                    _wait_for_gateway_exit(timeout=5.0, force_after=None)
        # --- Systemd services (Linux) --- Discover all hermes-gateway* units (default + profiles) plus
        # hermes-serve* units (the Desktop app's backend, #83438).
        if systemd_listings is not None:
            _restart_systemd_gateway_units_best_effort(failed, systemd_listings)
        # --- Launchd services (macOS) --- Restart EVERY ai.hermes.gateway* LaunchAgent, not only the
        # invoking profile's — parity with the systemd branch above (#41403). Per-label TimeoutExpired
        # isolation happens inside.
        if is_macos():
            try:
                _restart_macos_launchd_gateways([], failed, 45.0, require_supervision=True)
            except Exception as exc:
                logger.debug("Pending fleet restart: launchd failed: %s", exc)
                failed.append("launchd")
        if is_windows():
            try:
                from hermes_cli import gateway_windows
                if gateway_windows.is_installed():
                    gateway_windows.restart()
            except Exception as exc:
                logger.debug("Pending fleet restart: Windows failed: %s", exc)
                failed.append("windows-gateway")
        if failed:
            _warn_incomplete_gateway_fleet_restart(failed)
            return False
        print("  ✓ Pending fleet restart completed.")
        return True
    except Exception as exc:
        try:
            surviving = list(find_gateway_pids(all_profiles=True))
        except Exception:
            surviving = pids
        _warn_gateway_restart_phase_aborted(exc, surviving)
        return False


def _expected_gateway_profiles_from_receipt() -> set[str] | None:
    """Profiles the latest receipt's plan saw running a gateway, or None.

    ``None`` means "no usable plan": missing, malformed, or unreadable
    receipt. The caller then has no evidence about which profiles are
    required, so it must fail closed rather than treat the snapshot as
    complete.
    """
    try:
        from hermes_cli.update_receipt import read_latest_receipt

        receipt = read_latest_receipt()
    except Exception:
        return None
    if not isinstance(receipt, dict):
        return None
    plan = receipt.get("plan")
    if not isinstance(plan, dict):
        return None
    profiles = {
        str(runtime.get("profile"))
        for runtime in plan.get("runtimes") or []
        if isinstance(runtime, dict)
        and runtime.get("kind") == "gateway"
        and runtime.get("profile")
    }
    return profiles or None


def _live_fleet_already_serves_checkout(fleet: list) -> bool:
    """True when every runtime that should be live already runs disk HEAD.

    The manual-``/restart`` shape: the fleet was restarted by hand, so every
    live gateway stamps the current checkout's sha, but ``fleet_restart_pending``
    survived (a plain restart never consumes it). Restarting again would bounce
    every gateway a second time for nothing.

    Fail closed: an empty snapshot means "nothing verifiably live", not
    "everything fine", and any stale / down / unknown row — or a profile the
    receipt's plan saw running a gateway that the probe can no longer see —
    keeps the normal catch-up behavior. So does a missing, malformed, or
    unreadable receipt/plan: without expected-profile evidence this snapshot
    cannot prove that no required profile went missing, and clearing the
    marker without a restart needs that proof.
    """
    if not fleet:
        return False
    for entry in fleet:
        if not isinstance(entry, dict) or entry.get("state") != "current":
            return False
    expected = _origin()._expected_gateway_profiles_from_receipt()
    if not expected:
        return False
    live = {
        str(entry.get("profile")) for entry in fleet if isinstance(entry, dict)
    }
    return expected.issubset(live)


def _apply_pending_fleet_restart_catchup() -> None:
    """On an already-up-to-date ``hermes update``, finish a skipped restart.

    No-op when nothing is pending. Exits 1 when the catch-up restart is
    incomplete so automation does not treat the fleet as healthy.
    """
    if not _origin()._pending_fleet_restart_needed():
        return
    print()
    _warn_pending_fleet_restart()
    # A manual `/restart` (or `hermes gateway restart`) brings the fleet onto
    # the current code without consuming this marker. Probe the real live
    # fleet first so that case clears the marker instead of restarting a
    # fleet that is already current. Anything stale, down, unknown, missing,
    # or unverifiable falls through to the real restart — never cleared on a
    # failed inspection.
    try:
        from hermes_cli.update_receipt import collect_fleet_versions

        fleet = collect_fleet_versions()
    except Exception as exc:
        logger.debug("Pending fleet inspection failed: %s", exc)
        fleet = None
    if fleet is not None and _origin()._live_fleet_already_serves_checkout(fleet):
        print("→ Live gateways already run the current code — clearing the pending restart.")
        _clear_fleet_restart_pending_marker()
        return
    print("→ Running the pending fleet restart...")
    if _origin()._run_pending_fleet_restart():
        _clear_fleet_restart_pending_marker()
        return
    print("  ⚠ Fleet restart incomplete. Recover with: hermes gateway restart")
    sys.exit(1)


def _systemctl(cmd: list, *, timeout: float):
    """Run a systemctl (or sudo systemctl) invocation, capturing utf-8 text with a timeout."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


# poll() takes signed 32-bit milliseconds; keep headroom for rounding in communicate().
_SYSTEMCTL_RESTART_TIMEOUT_MAX = (2**31 - 1) // 1000 - 1


def _systemd_restart_timeout(scope_cmd: list, svc_name: str, *, start_only: bool = False) -> float:
    """Outwait the unit's stop + start budgets, not just the systemctl client.

    A client timeout does not cancel the manager's queued restart. Unknown or
    infinite limits use systemd's usual 90s per phase so automation stays bounded.
    Custom ExecStop chains or EXTEND_TIMEOUT_USEC can still exceed this budget;
    genuine timeouts must continue through the existing per-unit failure path.
    """
    from gateway.shutdown_forensics import parse_systemd_duration_to_us

    budgets = {"TimeoutStartUSec": 90.0}
    if not start_only:
        budgets["TimeoutStopUSec"] = 90.0
    try:
        show = _systemctl(
            scope_cmd + ["show", svc_name, "--property=TimeoutStopUSec,TimeoutStartUSec"],
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return sum(budgets.values()) + 15.0
    if show.returncode == 0:
        for line in (show.stdout or "").splitlines():
            key, _, raw = line.partition("=")
            if key in budgets:
                # The shared parser returns None for infinity/unrecognized units.
                try:
                    raw = raw.strip()
                    duration = int(raw) if raw.isascii() and raw.isdigit() else parse_systemd_duration_to_us(raw)
                    if duration is not None and duration > 0:
                        budgets[key] = duration / 1_000_000
                except (ValueError, OverflowError):
                    pass
    return min(sum(budgets.values()) + 15.0, _SYSTEMCTL_RESTART_TIMEOUT_MAX)


def _systemctl_reset_and_restart(manage_cmd: list, svc_name: str, *, scope_cmd: list | None = None):
    """``reset-failed`` then ``restart``: a unit parked in failed state by systemd's own
    auto-restart can wedge a plain ``restart`` against RestartSec backoff and stay dead."""
    # Property reads need no manage-units privileges: narrow sudoers may permit
    # restart/reset-failed but deny show. Keep the same user/system manager scope.
    timeout = _systemd_restart_timeout(scope_cmd if scope_cmd is not None else manage_cmd, svc_name)
    _systemctl(manage_cmd + ["reset-failed", svc_name], timeout=10)
    return _systemctl(manage_cmd + ["restart", svc_name], timeout=timeout)


def _is_hermes_gateway_unit(unit: str) -> bool:
    """Exact base unit or hyphenated profile family only: ``startswith("hermes-serve")``
    would accept ``hermes-server.service``."""
    return (
        # list-units is already pattern-filtered, but keep the name gate so a stray non-gateway/serve line
        # cannot enter the restart path. See #83595.
        unit == "hermes-gateway.service"
        or unit.startswith("hermes-gateway-")
        or unit == "hermes-serve.service"
        or unit.startswith("hermes-serve-")
        or unit == "hermes-dashboard.service"
        or unit.startswith("hermes-dashboard-")
    )


def _for_each_systemd_gateway_unit(list_units_stdout: str, *, process_unit, on_unit_timeout) -> None:
    """Process each hermes-gateway*/hermes-serve* unit from ``systemctl list-units``.

    ``TimeoutExpired`` from ``process_unit`` is isolated per unit via ``on_unit_timeout``
    so one wedged systemctl call cannot abort the rest of the fleet.

    See #68523.
    """
    for line in (list_units_stdout or "").strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith(".service") or not _is_hermes_gateway_unit(unit):
            continue
        svc_name = unit.removesuffix(".service")
        try:
            process_unit(svc_name)
        except subprocess.TimeoutExpired as exc:
            on_unit_timeout(svc_name, exc)


def _service_unit_supports_graceful_sigusr1_restart(svc_name: str) -> bool:
    """Whether *svc_name* wires SIGUSR1 to a graceful drain-then-restart.

    Only ``hermes-gateway*`` runs ``gateway/run.py`` (the handler); SIGUSR1 would just
    kill ``hermes-serve*`` and burn the drain budget, so those go straight to the blunt
    restart. Same exact/hyphenated shape as ``_for_each_systemd_gateway_unit`` so a
    near-prefix unit like ``hermes-gatewayd`` is never signalled.

    See #83438.
    """
    return svc_name == "hermes-gateway" or svc_name.startswith("hermes-gateway-")


def _warn_incomplete_gateway_fleet_restart(failed_units: list) -> None:
    """Print an explicit incomplete-update warning for unrestarted units."""
    from hermes_cli.gateway import is_macos
    if not failed_units:
        return
    ordered = list(dict.fromkeys(failed_units))  # de-dup, discovery order
    print()
    print("⚠ Update incomplete — some units were not restarted:")
    for name in ordered:
        print(f"    - {name}")
    if is_macos():
        # A label lands here when launchd wasn't supervising a live process after
        # the restart — likely deregistered, which `launchctl kickstart` can't revive.
        # See #88848.
        print("  Listed services may be deregistered from launchd, or still")
        print("  running pre-update code (mixed sys.modules). Recover with:")
        print("    hermes gateway status")
        print("    launchctl list | grep <label>")
        print("    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist")
        return
    print("  Skipped units may still be running pre-update code (mixed")
    print("  sys.modules). Restart them manually, then verify:")
    print("    hermes gateway status")
    if any(not name.startswith("ai.hermes.") for name in ordered):
        print("    systemctl --user restart <unit>   # user-scope")
        print("    sudo systemctl restart <unit>     # system-scope")
    if any(name.startswith("ai.hermes.") for name in ordered):
        print("    launchctl kickstart -k gui/$UID/<label>   # macOS (or user/$UID)")


def _restart_launchd_gateway_after_update(*, supervision_verify: bool = True) -> tuple[list, list]:
    """Restart the invoking profile's launchd gateway after an update.

    No ``launchctl list`` gating: a booted-out job (plist present, definition
    deregistered) fails it, and it can exit non-zero while the job is alive — gating on it
    silently skipped the restart yet printed "Update complete!". When the plist exists
    ``launchd_restart()`` always runs; every failure path is loud with a manual recovery
    command. Returns ``(restarted_labels, failed_labels)``; with ``supervision_verify``
    success also requires a fresh supervised PID ("the call returned" is not "supervised").

    74973 (salvage #75021 by @jeff-mettel): the restart used to be gated on ``launchctl list <label>``
    exiting 0. A *booted-out* job — plist present, definition deregistered from launchd (crashed helper,
    manual bootout, failed prior update) — fails that check, so the whole branch silently skipped: no
    restart, no message, ``KeepAlive`` unable to revive a definition launchd no longer knows, and the update
    still printed "Update complete!".
    See #88848.
    """
    from hermes_cli.gateway import (
        get_launchd_label, get_launchd_plist_path, launchd_restart, wait_for_launchd_gateway_supervision,
    )
    current_label = get_launchd_label()
    try:
        if not get_launchd_plist_path().exists():
            return [], []  # not a launchd install — nothing to do or warn
        try:
            launchd_restart()
        except subprocess.CalledProcessError as e:
            stderr = (getattr(e, "stderr", "") or "").strip()
            print(
                f"  ⚠ Gateway restart failed: {stderr}\n"
                "    The gateway may be DOWN on pre-update code. "
                "Recover manually: hermes gateway restart"
            )
            return [], [current_label]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # A plist exists, so a gateway is SUPPOSED to be supervised; a broken/wedged
        # launchctl is not proof nothing needs restarting. Count it, tell the operator.
        print(
            # The old code `pass`ed here (#74973's second silent variant); count it and tell the operator.
            "  ⚠ Could not restart the gateway "
            f"({e.__class__.__name__}: {e}).\n"
            "    Recover manually: hermes gateway restart"
        )
        return [], [current_label]

    if not supervision_verify:
        return [current_label], []

    # launchd_restart() returning only means "restart REQUESTED" (async). A helper dying
    # before first bootstrap, or a bootstrap exiting 0 without registering (macOS 26.6.1),
    # would otherwise reach "Update complete!" unsupervised. Verified domain-agnostically:
    # domain locate fails on macOS-26 per-user domains.
    # launchd_restart() returning is only "restart REQUESTED" — the self-restart branch hands work to the
    # running gateway, a plist reload to a detached helper; both asynchronous. See #88848.
    if wait_for_launchd_gateway_supervision(label=current_label):
        return [current_label], []
    print(
        f"  ✗ {current_label} restarted but launchd is not supervising it.\n"
        "    Check logs, then: hermes gateway restart"
    )
    return [], [current_label]


def _restart_macos_launchd_gateways(
    restarted_services: list, failed_or_stale_units: list, drain_budget: float, *, require_supervision: bool = False,
) -> None:
    """Restart every launchd-managed gateway after an update (macOS).

    The pull is shared across profiles, so every ``ai.hermes.gateway*`` LaunchAgent
    must reload it or siblings stay on pre-update ``sys.modules`` (systemd parity).
    Invoking profile uses ``launchd_restart()``; siblings get the same drain-first
    sequence with their domain (``gui/<uid>`` vs ``user/<uid>``) resolved per label so
    none is kickstarted in the wrong domain. ``TimeoutExpired`` is isolated per label.

    See #41403.
    The invoking profile keeps the existing ``launchd_restart()`` treatment (self-restart request → graceful
    drain → kickstart). ``subprocess.TimeoutExpired`` is isolated per label so one wedged launchctl call
    cannot leave the rest of the fleet on old code (#68523).
    """
    from hermes_cli.gateway import (
        get_launchd_label, get_launchd_plist_path, launchd_gateway_labels_for_install, _graceful_restart_via_sigusr1, _launchd_kickstart,
        _locate_launchd_gateway_service, _wait_for_launchd_service_pid,
    )
    if require_supervision:
        listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        if listing.returncode != 0:
            failed_or_stale_units.append("launchd (listing failed)")
            return
    _restarted, _failed = _restart_launchd_gateway_after_update(supervision_verify=True)
    restarted_services.extend(_restarted)
    failed_or_stale_units.extend(_failed)
    current_label = get_launchd_label()

    for label in launchd_gateway_labels_for_install():
        if label == current_label:
            continue
        try:
            # Locate = liveness + domain in one probe; kickstart and fresh-PID checks
            # reuse that domain so a sibling is never probed in one and restarted in another.
            domain, old_pid = _locate_launchd_gateway_service(label)
            if domain is None:
                if require_supervision and get_launchd_plist_path().with_name(f"{label}.plist").exists():
                    failed_or_stale_units.append(label)
                continue  # A profile without an installed job has no restart target.
            graceful_ok = False
            if old_pid is not None and old_pid > 0:
                print(f"  → {label}: draining (up to {int(drain_budget)}s)...")
                graceful_ok = _graceful_restart_via_sigusr1(old_pid, drain_timeout=drain_budget)
            if graceful_ok and _wait_for_launchd_service_pid(label, old_pid=old_pid, timeout=10.0, domain=domain):
                # KeepAlive already respawned it on new code — a kickstart would kill it.
                restarted_services.append(label)
                continue
            try:
                _launchd_kickstart(label, domain)
            except subprocess.CalledProcessError as e:
                stderr = (getattr(e, "stderr", "") or "").strip()
                failed_or_stale_units.append(label)
                print(
                    f"  ⚠ Failed to restart {label}: {stderr}\n"
                    f"    Recover manually: launchctl kickstart -k {domain}/{label}"
                )
                continue
            if _wait_for_launchd_service_pid(label, old_pid=old_pid, timeout=15.0, domain=domain):
                restarted_services.append(label)
            else:
                failed_or_stale_units.append(label)
                print(
                    f"  ✗ {label} failed to come back after restart.\n"
                    f"    Check logs, then: launchctl kickstart -k {domain}/{label}"
                )
        except subprocess.TimeoutExpired:
            failed_or_stale_units.append(label)
            print(f"  ⚠ launchctl timed out restarting {label}; continuing with remaining gateways")


def _surviving_gateway_pids_after_failed_restart():
    """Best-effort PIDs of gateways still running after the restart phase died.

    ``None`` when undeterminable (notably ``hermes_cli.gateway`` no longer importing
    under the replaced checkout). Callers treat ``None`` and non-empty as "assume
    stale"; only a positive empty result proves nothing needs restarting.
    """
    try:
        from hermes_cli.gateway import find_gateway_pids
        return list(find_gateway_pids(all_profiles=True))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not probe for surviving gateways after update: %s", exc)
        return None


_MANUAL_GATEWAY_SKIP_REASON = (
    "manual gateway has no supervisor relaunch authority; left running for explicit operator restart"
)
_DESKTOP_SERVE_SKIP_REASON = (
    "desktop app owns and respawns this serve backend;"
    " the recovery pass must not restart it out from under its supervisor"
)
# NOT a claim that no supervisor exists: a systemd-launched serve sets neither HERMES_SPAWN
# nor HERMES_PARENT_PID ("manual-serve"). Unit-backed serves are recovered by the fresh
# child's systemd pass; survivors reported by _surviving_pre_update_serve_runtimes.
_SERVE_SKIP_REASON = (
    "no per-profile relaunch command reaches a serve/dashboard runtime; recovered by the fresh"
    " systemd unit pass when it owns a hermes-serve* unit, else left running for explicit"
    " operator restart"
)


def _gateway_recovery_partition(plan, *, skip_profiles: set[str] | None = None) -> tuple[dict[str, str], list[dict]]:
    """Partition pre-update runtimes into fresh-restart candidates and skips.

    Uses only the pre-checkout inventory: re-importing ``hermes_cli.gateway`` in the
    failing interpreter is what raises the original ``ImportError``. Returns
    ``(candidates, skipped)``: profile → supervisor for supervised gateways the fresh
    process may restart; every other inventoried runtime with an explicit reason so
    nothing vanishes silently. Skipped serve/dashboard is NOT unrecoverable: the fresh
    child's ``hermes-serve*`` systemd pass enumerates units from systemd; leftovers are
    caught by :func:`_surviving_pre_update_serve_runtimes`.
    """
    skip_profiles = skip_profiles or set()
    candidates: dict[str, str] = {}
    skipped: list[dict] = []
    with _best_effort('Could not prepare fresh gateway restart profiles: %s'):
        for runtime in getattr(plan, "runtimes", ()) or ():
            kind = getattr(runtime, "kind", None)
            profile = getattr(runtime, "profile", None)
            supervisor = getattr(runtime, "supervisor", None)
            if not isinstance(profile, str) or not profile:
                continue
            if kind == "gateway":
                if profile in skip_profiles:
                    continue
                if supervisor in _FRESH_RESTART_SUPERVISORS:
                    candidates.setdefault(profile, str(supervisor))
                    continue
                reason = _MANUAL_GATEWAY_SKIP_REASON
            elif kind in ("serve", "dashboard"):
                reason = _DESKTOP_SERVE_SKIP_REASON if supervisor == "desktop" else _SERVE_SKIP_REASON
            else:
                continue
            skipped.append({"profile": profile, "kind": str(kind), "supervisor": str(supervisor), "reason": reason})
    return candidates, skipped


def _warn_gateway_restart_phase_aborted(exc: BaseException, pids) -> None:
    """Print a recovery warning when the whole restart phase raised.

    Previously a blanket debug-logged ``except Exception`` erased every drain/restart
    line, so "Update complete!" exited 0 while the gateway kept serving pre-update
    modules and died on the next turn with an ImportError.

    Issue #78574: the gateway auto-restart phase was wrapped in a blanket ``except Exception`` that only
    logged at debug level, so an early failure (e.g. importing ``hermes_cli.gateway`` from the freshly
    pulled checkout) erased every drain/restart line from the update output.
    """
    print()
    print(f"⚠ Update incomplete — gateway auto-restart failed: {exc}")
    if pids:
        listed = ", ".join(str(pid) for pid in pids)
        print(f"  Gateway process(es) still running pre-update code: {listed}")
    else:
        print("  Any gateway still running is serving pre-update code")
        print("  (mixed sys.modules) against the updated checkout.")
    print("  Restart it manually, then verify:")
    print("    hermes gateway restart")
    print("    hermes gateway status")


def _drain_or_signal_gateway_for_update(pid: int, drain_budget: float, label: str) -> bool:
    """Three-way triage (shared by systemd and bare-process paths) for handing a
    running gateway over to new code. Returns True when signalled/stopped.

    1. Gateway is an ancestor of this process (auto-update cron inside the gateway
       tree): waiting is circular (gateway waits on in-flight work → cron session
       waits on update → update waits on gateway) and the 1800s force-drain cap burns.
       So fire-and-forget: signal restart and return; it completes once THIS process exits.
    2. Event loop provably wedged: SIGUSR1 can never drain it; bounded SIGTERM→SIGKILL.
    3. Live out-of-tree gateway: graceful SIGUSR1 drain up to ``drain_budget``.

    The wedged-loop probe cannot break it: the cron session posts activity every ~180s (process-tool poll
    return), so it is "actively waiting forever" and never marked wedged — the gateway burns the full
    force-drain cap (1800s) before killing its own updater's session. See #86684.
    """
    from hermes_cli.gateway import (
        GATEWAY_LOOP_WEDGED, _escalate_wedged_gateway, _graceful_restart_via_sigusr1,
        _is_pid_ancestor_of_current_process, _request_gateway_self_restart, probe_gateway_loop_liveness,
    )
    if _is_pid_ancestor_of_current_process(pid):
        print(
            f"  → {label}: update is running inside this gateway's "
            "process tree — signalling restart and letting the gateway "
            "drain itself (avoids the cron-update deadlock, #100179)"
        )
        return _request_gateway_self_restart(pid)
    if probe_gateway_loop_liveness(pid) == GATEWAY_LOOP_WEDGED:
        print(f"  ⚠ {label}: gateway event loop is unresponsive — skipping drain, forcing a bounded stop...")
        _escalate_wedged_gateway(pid)
        return True
    print(f"  → {label}: draining (up to {int(drain_budget)}s)...")
    return _graceful_restart_via_sigusr1(pid, drain_timeout=drain_budget)


def _resolve_manage_cmd(cache: dict, scope_: str, scope_cmd_: list, svc_name_: str):
    """Resolve the command prefix for manage-units verbs (None ⇒ no privilege path).

    Manage-units verbs on a *system* service trigger a polkit prompt for non-root
    users, which flashes and dies inside our captured 10-15s subprocess. Root → plain
    systemctl; else ``sudo -n`` blanket probe, then a targeted ``reset-failed`` probe
    so a least-privilege sudoers entry scoped to hermes-gateway* qualifies (idempotent
    no-op we run before every privileged restart anyway). On None the caller must SKIP
    the restart (without draining first!). ``--no-ask-password`` prevents polkit hangs.
    """
    if scope_ in cache:
        return cache[scope_]
    cmd = scope_cmd_ + ["--no-ask-password"]
    if _needs_sudo(scope_):
        sudo_cmd = ["sudo", "-n"] + cmd
        try:
            sudo_ok = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
            if not sudo_ok:
                # Blanket sudo refused — a targeted NOPASSWD sudoers entry may still work.
                sudo_ok = subprocess.run(
                    sudo_cmd + ["reset-failed", svc_name_], capture_output=True, timeout=5
                ).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            sudo_ok = False
        cmd = sudo_cmd if sudo_ok else None
    cache[scope_] = cmd
    return cmd


def _restart_one_systemd_gateway_unit(
    svc_name: str, *, scope: str, scope_cmd: list, drain_budget: float, _manage_cmd_cache: dict,
    restarted_services: list, failed_or_stale_units: list,
) -> None:
    """Restart one active systemd gateway/serve unit: graceful SIGUSR1 drain, then forced restart.

    Appends settled names to ``restarted_services`` and failures to ``failed_or_stale_units``.
    """
    check = _systemctl(scope_cmd + ["is-active", svc_name], timeout=5)
    if check.stdout.strip() != "active":
        return

    # None ⇒ no non-interactive privilege path; avoid manage-units verbs
    # entirely or polkit prompts inside the captured subprocess.
    _manage_cmd = _resolve_manage_cmd(_manage_cmd_cache, scope, scope_cmd, svc_name)

    # Graceful SIGUSR1 first so in-flight runs drain: handler → request_restart(via_service=True)
    # → drain → exit, Restart=always respawns. hermes-serve has no handler → blunt restart below.
    _main_pid = 0
    if _service_unit_supports_graceful_sigusr1_restart(svc_name):
        try:
            _show = _systemctl(scope_cmd + ["show", svc_name, "--property=MainPID", "--value"], timeout=5)
            _main_pid = int((_show.stdout or "").strip() or 0)
        except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
            _main_pid = 0

    # Three-way triage (ancestor / wedged / graceful drain).
    _graceful_ok = _main_pid > 0 and _drain_or_signal_gateway_for_update(_main_pid, drain_budget, svc_name)

    if _graceful_ok:
        # ``Restart=always`` respawns only after RestartSec (60s in our unit; dead time for a
        # voluntary restart). ``reset-failed`` + ``start`` skips it (~1-3s); if RestartSec already
        # elapsed, ``start`` is a no-op and we fall through to the poll. Needs manage-units
        # privileges; without them auto-restart still fires after RestartSec.
        if _manage_cmd is not None:
            _systemctl(_manage_cmd + ["reset-failed", svc_name], timeout=10)
            _systemctl(
                _manage_cmd + ["start", svc_name],
                timeout=_systemd_restart_timeout(scope_cmd, svc_name, start_only=True),
            )
            if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
                restarted_services.append(svc_name)
                return
        # Passive poll: auto-restart fires after RestartSec regardless of
        # privileges — primary when _manage_cmd is None, fallback otherwise.
        _restart_sec = _service_restart_sec(scope_cmd, svc_name, default=0.0)
        if _manage_cmd is None and _restart_sec > 5.0:
            print(
                f"  → {svc_name}: waiting for systemd "
                f"auto-restart (~{int(_restart_sec)}s; "
                "no root for an immediate restart)..."
            )
        if _wait_for_service_active(scope_cmd, svc_name, timeout=max(10.0, _restart_sec + 10.0)):
            restarted_services.append(svc_name)
            return
        # Exited but not respawned (older unit without Restart=on-failure /
        # RestartForceExitStatus=75); fall through to forced restart.
        print(f"  ⚠ {svc_name} drained but didn't relaunch — forcing restart")

    # Forcing needs manage-units privileges; without a non-interactive path
    # polkit would prompt inside the captured subprocess — skip, instruct.
    if _manage_cmd is None:
        failed_or_stale_units.append(svc_name)
        print(
            f"  ⚠ {svc_name} is a system service and restarting it needs root.\n"
            f"    Restart it manually to load the new version:\n"
            f"      sudo systemctl restart {svc_name}\n"
            f"    To let `hermes update` restart it automatically, allow\n"
            f"    passwordless sudo for systemctl, or run updates with sudo."
        )
        return

    # Blunt restart — only when the graceful path failed (no SIGUSR1 wiring, drain over
    # budget, restart-policy mismatch). Mirrors `hermes gateway restart` (`systemd_restart()`).
    restart = _systemctl_reset_and_restart(_manage_cmd, svc_name, scope_cmd=scope_cmd)
    if restart.returncode != 0:
        failed_or_stale_units.append(svc_name)
        print(f"  ⚠ Failed to restart {svc_name}: {restart.stderr.strip()}")
        return
    # restart returns 0 even if the new process crashes at once — verify.
    if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
        restarted_services.append(svc_name)
        return
    # Retry once — transient startup failures (stale module cache,
    # import race) often clear; reset-failed so the retry isn't blocked.
    print(f"  ⚠ {svc_name} died after restart, retrying...")
    _systemctl_reset_and_restart(_manage_cmd, svc_name, scope_cmd=scope_cmd)
    if _wait_for_service_active(scope_cmd, svc_name, timeout=10.0):
        restarted_services.append(svc_name)
        print(f"  ✓ {svc_name} recovered on retry")
        return
    failed_or_stale_units.append(svc_name)
    _scope_flag = "--user " if scope == "user" else ""
    _sudo_hint = "sudo " if scope == "system" else ""
    print(
        f"  ✗ {svc_name} failed to stay running after restart.\n"
        f"    Check logs: {_sudo_hint}journalctl {_scope_flag}-u {svc_name} --since '2 min ago'\n"
        f"    Recover manually:\n"
        f"      {_sudo_hint}systemctl {_scope_flag}reset-failed {svc_name}\n"
        f"      {_sudo_hint}systemctl {_scope_flag}restart {svc_name}"
    )


def _restart_systemd_gateway_units(restarted_services, failed_or_stale_units, restarted_scoped_units, drain_budget):
    """Restart every active hermes-gateway*/hermes-serve* systemd unit (user + system).

    Settled units → ``restarted_services`` (bare) and ``restarted_scoped_units``
    (``scope/name``); failures → ``failed_or_stale_units``. Per-unit timeouts isolated.
    """
    from hermes_cli.gateway import supports_systemd_services, _ensure_user_systemd_env
    if not supports_systemd_services():
        return
    _manage_cmd_cache: dict = {}
    with suppress(Exception):
        _ensure_user_systemd_env()

    def _on_list_timeout(scope: str, exc: subprocess.TimeoutExpired) -> None:
        # Discovery timeout — skip this scope, keep the other.
        print(
            f"  ⚠ systemctl timed out listing {scope}-scope "
            f"gateway units ({exc.cmd if exc.cmd else 'unknown command'}). "
            f"Check the gateway with: hermes gateway status"
        )

    def _on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
        # Isolate to this unit; a scope-wide handler used to abort every
        # later gateway and leave the fleet on mixed code.
        failed_or_stale_units.append(svc_name)
        print(
            # See #68523.
            f"  ⚠ systemctl timed out restarting {svc_name} "
            f"({exc.cmd if exc.cmd else 'unknown command'}); "
            f"continuing with remaining gateways"
        )

    for scope, scope_cmd, result in _systemd_gateway_unit_listings(_on_list_timeout):
        # Scope-qualify this scope's additions before the next scope can add a
        # same-named unit; ``finally`` so a mid-scope abort keeps settled units.
        _scope_mark = len(restarted_services)
        try:
            _for_each_systemd_gateway_unit(
                result.stdout,
                process_unit=lambda svc_name: _restart_one_systemd_gateway_unit(
                    svc_name,
                    scope=scope,
                    scope_cmd=scope_cmd,
                    drain_budget=drain_budget,
                    _manage_cmd_cache=_manage_cmd_cache,
                    restarted_services=restarted_services,
                    failed_or_stale_units=failed_or_stale_units,
                ),
                on_unit_timeout=_on_unit_timeout,
            )
        finally:
            restarted_scoped_units.update(f"{scope}/{name}" for name in restarted_services[_scope_mark:])


@dataclass
class _GatewayRestartOutcome:
    """Restart-phase bookkeeping. ``restarted_services`` keeps bare unit names (fleet
    probe, receipt, summary read it); ``incomplete`` ⇒ a gateway may still be stale."""

    incomplete: bool
    phase_errors: list
    pre_restart_gateway_pids: "list | None"
    restarted_services: list
    failed_or_stale_units: list
    relaunched_profiles: list
    externally_supervised_profiles: list
    killed_pids: set
    #: Gateways stopped with NO successor (no profile mapping / relaunch could not be armed);
    #: the summary tells the user to restart them by hand, so the fleet probe must not expect
    #: a row for them.
    stopped_unmapped_pids: set = field(default_factory=set)

    def fleet_probe_signals(self) -> tuple:
        """``(pre_restart_pids, killed_pids)`` with the unmapped stops removed — the signals that
        legitimately predict a fleet-matrix row."""
        pre = self.pre_restart_gateway_pids
        if pre is not None:
            pre = [pid for pid in pre if pid not in self.stopped_unmapped_pids]
        return pre, self.killed_pids - self.stopped_unmapped_pids

    def record_receipt(self, **extra) -> None:
        """Best-effort ``record_gateway_restart`` from the current bookkeeping."""
        with suppress(Exception):
            from hermes_cli.update_receipt import record_gateway_restart
            record_gateway_restart(
                restarted_services=self.restarted_services, relaunched_profiles=self.relaunched_profiles,
                externally_supervised_profiles=self.externally_supervised_profiles,
                killed_pids=sorted(self.killed_pids), failed_units=self.failed_or_stale_units,
                incomplete=self.incomplete, **extra,
            )


def _restart_manual_gateways(out: _GatewayRestartOutcome, _drain_budget) -> None:
    """Drain/stop every manual (non-service) gateway and print the restart summary.

    Mutates ``out`` in place; raises so the caller's abort recovery fires.
    """
    import signal as _signal
    from hermes_cli.gateway import (
        find_gateway_pids, find_profile_gateway_processes, _prepare_profile_gateway_update_restart, _get_service_pids,
        _wait_for_gateway_exit,
    )
    # Exclude just-restarted service PIDs so we don't kill what systemd/launchd spawned.
    service_pids = _get_service_pids(all_profiles=True)
    manual_pids = find_gateway_pids(exclude_pids=service_pids, all_profiles=True)
    profile_processes = {
        proc.pid: proc
        for proc in find_profile_gateway_processes(exclude_pids=service_pids)
        if proc.pid in manual_pids
    }
    # Profile gateways we couldn't arm a relaunch for must NOT keep running stale:
    # the unmapped sweep below stops them and lists them under "Restart manually".
    # These must NOT be left running: their modules are the pre-update ones and every lazy import from here
    # on mixes versions against the new code on disk (#88654). Handing them to the unmapped sweep below
    # stops them and surfaces them in the "Stopped N manual gateway process(es) / Restart manually" summary,
    # which is the contract already used for gateways with no profile mapping.
    unrestartable_pids = set()
    for pid, proc in profile_processes.items():
        restart_mode = _prepare_profile_gateway_update_restart(proc.profile, pid)
        if restart_mode is None:
            # A bare ``continue`` here left it serving stale modules with no signal.
            print(
                f"  ⚠ {proc.profile}: could not arm an automatic "
                f"gateway restart for PID {pid} — stopping it instead "
                "so it cannot keep running pre-update code"
            )
            unrestartable_pids.add(pid)
            continue
        # SIGUSR1 drain first, SIGTERM fallback if unsupported/over budget — the watcher
        # relaunches either way. The helper announces its choice first because a silent
        # full-budget wait reads as a hung update.
        if not _drain_or_signal_gateway_for_update(pid, _drain_budget, proc.profile):
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, _signal.SIGTERM)
        # Wait ≤5s for exit: Telegram keeps the old getUpdates session ~30s; a new gateway
        # inside that window gets a 409 (_handle_polling_conflict retries, but a brief
        # wait avoids it on fast machines).
        _wait_for_gateway_exit(timeout=5.0, force_after=None)
        out.killed_pids.add(pid)
        if restart_mode == "external-supervisor":
            out.externally_supervised_profiles.append(proc.profile)
        else:
            out.relaunched_profiles.append(proc.profile)

    for pid in manual_pids:
        if pid in profile_processes and pid not in unrestartable_pids:
            continue
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, _signal.SIGTERM)
            out.killed_pids.add(pid)
            out.stopped_unmapped_pids.add(pid)

    if out.restarted_services or out.killed_pids:
        print()
        for svc in out.restarted_services:
            print(f"  ✓ Restarted {svc}")
        if out.relaunched_profiles:
            print(f"  ✓ Restarting manual gateway profile(s): {', '.join(out.relaunched_profiles)}")
        if out.externally_supervised_profiles:
            names = ", ".join(out.externally_supervised_profiles)
            print(f"  ✓ Handed gateway profile(s) back to their external supervisor: {names}")
        unmapped_count = (len(out.killed_pids) - len(out.relaunched_profiles) - len(out.externally_supervised_profiles))
        if unmapped_count:
            print(f"  → Stopped {unmapped_count} manual gateway process(es)")
            print("    Restart manually: hermes gateway run")
            if unmapped_count > 1:
                print("    (or: hermes -p <profile> gateway run  for each profile)")


def _force_kill_stuck_gateways(killed_pids) -> None:
    """Survivor sweep: gateways ignoring SIGTERM (stuck drain, blocked I/O, zombie) never
    exit, so the watcher never respawns and ImportErrors persist. Give graceful paths a
    moment, then SIGKILL remaining pre-update PIDs."""
    with _best_effort('Post-restart survivor sweep failed: %s'):
        from hermes_cli.gateway import find_gateway_pids, _get_service_pids
        # --- Post-restart survivor sweep ----------------------------- Issue #17648: some gateways ignore
        # SIGTERM (stuck drain, blocked I/O, PID dead but zombie). The detached profile watchers wait 120s
        # for the old PID to exit — if it never does, no respawn happens and the user keeps hitting
        # ImportError against a stale sys.modules.
        _time.sleep(3.0)
        _surviving = find_gateway_pids(exclude_pids=_get_service_pids(all_profiles=True), all_profiles=True)
        # Only PIDs we already tried to kill; newer ones are left alone.
        _stuck = [pid for pid in _surviving if pid in killed_pids]
        if _stuck:
            print()
            print(f"  ⚠ {len(_stuck)} gateway process(es) ignored SIGTERM — force-killing")
            from gateway.status import get_process_start_time, terminate_pid
            for pid in _stuck:
                with suppress(ProcessLookupError, PermissionError, OSError):
                    # taskkill /T /F on Windows (no SIGKILL there), SIGKILL on POSIX.
                    terminate_pid(pid, force=True, expected_start_time=get_process_start_time(pid))
            # Let the OS reap so watchers see the exit and respawn.
            _time.sleep(1.5)


def _recover_after_restart_phase_abort(
    e, _pre_update_plan, out: _GatewayRestartOutcome, *, gateway_mode, restarted_scoped_units
) -> None:
    """Phase-abort recovery: fresh-child restart + fail-closed verdict; updates ``out`` in place."""
    from hermes_cli.update_cmd import (
        _abort_recovery_is_complete, _recover_gateway_restart_after_abort, _surviving_pre_update_serve_runtimes,
        _warn_stale_serve_runtimes, _write_gateway_update_exit_code,
    )
    logger.debug("Gateway restart during update failed: %s", e)
    out.phase_errors.append(str(e))
    # Restart output never printed: assume stale unless provably no gateway runs.
    # Empty ``_surviving`` proves safety only if nothing ran beforehand; a gone
    # pre-restart gateway was stopped without verified replacement → fail closed.
    # An exception escaping the whole phase means the drain/restart output the user relies on never printed.
    # Don't let that pass for a clean update: surface it and treat the fleet as stale unless we can
    # positively prove no gateway is running (#78574). A positive-empty ``_surviving`` is only
    # proof-of-safety when nothing was running before we touched anything. If a gateway was discovered
    # pre-restart and none survive now, it was stopped and its replacement was never verified — the same
    # fail-open contract this fix closes — so we must still fail closed on ``[]``.
    _surviving = _surviving_gateway_pids_after_failed_restart()
    _planned_gateway_profiles = {
        runtime.profile
        for runtime in getattr(_pre_update_plan, "runtimes", ()) or ()
        if getattr(runtime, "kind", None) == "gateway"
        and isinstance(getattr(runtime, "profile", None), str)
    }
    _already_restarted_profiles = set(out.relaunched_profiles) | set(out.externally_supervised_profiles)
    _already_restarted_profiles.update(
        profile
        for profile in _planned_gateway_profiles
        if any(_gateway_service_matches_profile(profile, service) for service in out.restarted_services)
    )
    _recovery_result = _recover_gateway_restart_after_abort(
        _pre_update_plan, gateway_mode=gateway_mode, skip_profiles=_already_restarted_profiles,
        skip_units=set(restarted_scoped_units),
    )
    _serve_units_failed = list((_recovery_result.get("serve_units") or {}).get("failed") or [])
    # Deliberately NOT merged into ``restarted_services`` (gateway vocabulary feeding the
    # fleet probe); serve coverage lives in the recovery result/receipt. A serve/dashboard
    # still the SAME pre-update process is live on old code (unreachable by `gateway
    # restart`): recovery may not claim success while one remains, and must never kill
    # one (manual/Desktop serves have no relaunch authority).
    _stale_runtime_rows = _surviving_pre_update_serve_runtimes(_pre_update_plan)
    _recovery_result["stale_runtimes"] = _stale_runtime_rows
    # Only systemd-VERIFIED outcomes claim coverage; a relaunch that merely exited 0
    # ("relaunch_attempted") was never observed and must not clear incomplete.
    _recovery_verified = set(_recovery_result.get("verified") or [])
    out.relaunched_profiles.extend(
        profile for profile in sorted(_recovery_verified) if profile not in out.relaunched_profiles
    )
    if _abort_recovery_is_complete(
        planned_gateway_profiles=_planned_gateway_profiles,
        covered_gateway_profiles=_already_restarted_profiles | _recovery_verified,
        recovery_result=_recovery_result,
        stale_runtime_rows=_stale_runtime_rows,
    ):
        # Fresh child is terminal; the fleet-version matrix stays the authoritative
        # read-back before success is declared.
        out.incomplete = False
    elif (
        _restart_phase_failure_is_incomplete(_surviving, out.pre_restart_gateway_pids)
        or _stale_runtime_rows
        or _serve_units_failed
    ):
        out.incomplete = True
        _warn_gateway_restart_phase_aborted(e, _surviving)
        _warn_stale_serve_runtimes(_stale_runtime_rows)
        if gateway_mode:
            _write_gateway_update_exit_code(False)
    out.record_receipt(phase_error=str(e), fresh_recovery=_recovery_result)


def _restart_gateway_fleet_after_update(_pre_update_plan, gateway_mode: bool):
    """Restart every running gateway (systemd, launchd, manual) onto the pulled code.

    Never raises: a phase abort runs fresh-child recovery and fails closed unless
    every planned gateway is verifiably covered.
    """
    from hermes_cli.update_cmd import _m, _write_gateway_update_exit_code
    # All bookkeeping is declared before the try so abort recovery and fleet reconciliation
    # can read it even if the phase raises early. ``pre_restart_gateway_pids`` stays empty
    # until we are about to stop/drain, so an early exception has nothing to fail closed on,
    # while a failure after stopping a discovered gateway fails closed on an empty survivor probe.
    out = _GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[], restarted_services=[], failed_or_stale_units=[],
        relaunched_profiles=[], externally_supervised_profiles=[], killed_pids=set(),
    )
    # Scope-qualified twin (``user/hermes-serve`` vs ``system/hermes-serve`` are different
    # processes; abort recovery needs WHICH settled). Bare names stay in
    # ``restarted_services`` for the fleet probe, receipt and summary.
    # Snapshot of gateways running before we touch anything. Stays empty until we successfully import the
    # probe and are about to stop/drain — so an exception raised before we touch any gateway keeps this
    # empty (nothing to fail closed on), while a failure after we have stopped a discovered gateway lets the
    # handler fail closed on an empty survivor probe rather than reporting a clean update (#78574).
    # Declared outside the restart try/except below (and never reset to None) so it's always safe to read
    # afterwards even if that block raises before reaching its own restart bookkeeping — needed to forward
    # already-restarted units to ``_finish_dashboard_update_cleanup`` (review on #83595).
    restarted_scoped_units: set = set()

    # Purge stale cached Hermes modules FIRST: the import below loads new gateway
    # source into this pre-update interpreter, and a cached sibling missing a
    # symbol the new source expects would ImportError and abort the whole phase.
    _m()._purge_stale_hermes_modules()
    try:
        # Every gateway helper the phase needs is imported up front so a broken gateway
        # module aborts into recovery BEFORE any unit is touched.
        from hermes_cli.gateway import (  # noqa: F401
            is_macos,
            find_gateway_pids,
            find_profile_gateway_processes,
            _prepare_profile_gateway_update_restart,
            _get_service_pids,
            _wait_for_gateway_exit,
        )
        # Drain budget covers ``restart_after_turn_timeout`` and stop()'s
        # ``restart_drain_timeout`` so a gateway waiting on a turn isn't hard-killed;
        # units without SIGUSR1 wiring just time out into ``systemctl restart``.
        try:
            from hermes_cli.gateway import _get_restart_exit_wait_budget
            _drain_budget = max(float(_get_restart_exit_wait_budget()), 45.0)
        except Exception:
            _drain_budget = 45.0

        # Snapshot before any stop/drain so an empty survivor probe reads as "stopped
        # and never came back", not "nothing was running"; None fails closed.
        try:
            out.pre_restart_gateway_pids = list(find_gateway_pids(all_profiles=True))
        except Exception:
            out.pre_restart_gateway_pids = None

        _restart_systemd_gateway_units(
            out.restarted_services, out.failed_or_stale_units, restarted_scoped_units, _drain_budget
        )

        # macOS: EVERY ai.hermes.gateway* LaunchAgent (systemd parity).
        if is_macos():
            with suppress(FileNotFoundError, ImportError):
                _restart_macos_launchd_gateways(out.restarted_services, out.failed_or_stale_units, _drain_budget)

        _restart_manual_gateways(out, _drain_budget)

        if out.failed_or_stale_units:
            out.incomplete = True
            if gateway_mode:
                _write_gateway_update_exit_code(False)
        _warn_incomplete_gateway_fleet_restart(out.failed_or_stale_units)
        out.record_receipt()
        _force_kill_stuck_gateways(out.killed_pids)

    except Exception as e:
        _recover_after_restart_phase_abort(
            e, _pre_update_plan, out, gateway_mode=gateway_mode, restarted_scoped_units=restarted_scoped_units
        )

    return out


def _print_legacy_units_warning() -> None:
    """Legacy hermes.service fights hermes-gateway.service over the bot token; warn on
    every update until migrated."""
    from hermes_cli.gateway import (has_legacy_hermes_units, _find_legacy_hermes_units, supports_systemd_services)
    if not (supports_systemd_services() and has_legacy_hermes_units()):
        return
    print()
    print("⚠ Legacy Hermes gateway unit(s) detected:")
    for name, path, is_sys in _find_legacy_hermes_units():
        scope = "system" if is_sys else "user"
        print(f"    {path}  ({scope} scope)")
    print()
    print("  These pre-rename units (hermes.service) fight the current")
    print("  hermes-gateway.service for the bot token and cause SIGTERM")
    print("  flap loops. Remove them with:")
    print()
    print("    hermes gateway migrate-legacy")
    print()
    print("  (add `sudo` if any are in system scope)")


def _collect_fleet_snapshot(restart, rows_expected: bool) -> list:
    """Fleet version rows, polled over a bounded settle window when runtimes are expected.

    Gateways need time to rewrite gateway_state.json; Windows resumes DETACHED (~10s boot),
    so a single 2s sleep reported "no rows" on healthy resumes. A "down" row may be a
    detached replacement still booting: poll until none remain or the deadline passes.
    Pre-restart PIDs make a gateway stopped WITHOUT verified replacement a DOWN row (exit 1)
    instead of no row at all.
    """
    from hermes_cli.update_receipt import collect_fleet_versions
    if not rows_expected:
        return collect_fleet_versions(pre_restart_pids=restart.pre_restart_gateway_pids)
    _fleet_deadline = _time.monotonic() + 30.0
    while True:
        _time.sleep(2.0)
        snapshot = collect_fleet_versions(pre_restart_pids=restart.pre_restart_gateway_pids)
        if snapshot and not any(row.get("state") == "down" for row in snapshot):
            return snapshot
        if _time.monotonic() >= _fleet_deadline:
            return snapshot


def _verify_fleet_after_update(restart, *, _pre_update_plan, _windows_gateway_resume, node_failures, update_complete):
    """Post-restart verification: legacy-unit warning, dashboard cleanup, stale serve
    probe, fleet version matrix, plan-vs-execution reconciliation, receipt finalize.

    Exits 1 (leaving ``fleet_restart_pending`` for the next catch-up) when any gateway
    may still be stale; otherwise clears the marker.
    """
    from hermes_cli.update_cmd import (
        _finish_dashboard_update_cleanup, _m, _surviving_pre_update_serve_runtimes, _warn_stale_serve_runtimes,
    )
    with _best_effort('Legacy unit check during update failed: %s'):
        _print_legacy_units_warning()

    # Restart a managed dashboard via systemd or stop stale manual ones (raw-killing
    # a systemd-owned PID reads as clean stop and leaves the Cloudflare origin dead).
    # Failed Node refresh leaves it untouched; already-restarted units aren't redone.
    _finish_dashboard_update_cleanup(node_failures, already_restarted_units=set(restart.restarted_services))

    # Success-path twin of the abort-recovery probe: the restart phase only touches
    # units, so a unit-less `hermes serve` keeps stale sys.modules. Runs AFTER
    # dashboard cleanup so a respawned manual dashboard isn't a survivor. Rows feed
    # reconciliation (survivor → exit 1); ``None`` = probe failed, stays fail-closed.
    # Check if any pre-update serve/dashboard runtimes survived on pre-update code generations (#100479).
    # This is the SUCCESS-path twin of the abort-recovery probe above: the restart phase only restarts
    # units, so an sshd-spawned `serve --isolated` or a manual `hermes serve` (no unit) is left running its
    # pre-update sys.modules graph — and its cron ticker keeps firing agent jobs that ImportError on every
    # symbol added in the pulled range. The rows also feed the plan-vs-execution reconciliation below, so a
    # survivor is escalated (exit 1) instead of merely printed.
    _stale_serve_rows: "list | None" = None
    with _best_effort('Failed to check for surviving serve runtimes: %s'):
        _stale_serve_rows = _surviving_pre_update_serve_runtimes(_pre_update_plan)
        if _stale_serve_rows:
            _warn_stale_serve_runtimes(_stale_serve_rows)

    print()
    print("Tip: You can now select a provider and model:")
    print("  hermes model              # Select provider and model")

    # Compare every live gateway's stamped code_sha against the fresh checkout
    # instead of assuming the restart phase worked.
    # Phase 1 (#91277): post-update fleet version verification.
    _fleet_snapshot: list = []
    with _best_effort('Fleet version verification failed: %s'):
        from hermes_cli.update_receipt import print_fleet_version_matrix
        # Cross-platform "rows expected" signal: (restarted_services or killed_pids)
        # never fires on Windows (pause/resume populates neither), so a healthy
        # resumed gateway yielded zero rows and exit 0.
        # See #93406.
        # A gateway stopped WITHOUT a successor ("Restart manually") publishes no row by design,
        # so it must not count as an expected one — otherwise an update whose only live gateways
        # were unmapped exits 1 with "no rows" after correctly stopping them.
        _pre_restart, _killed = restart.fleet_probe_signals()
        _fleet_rows_expected = _m()._fleet_probe_expected_runtimes(
            _pre_update_plan, _pre_restart, _windows_gateway_resume, restart.restarted_services, _killed,
        )
        _fleet_snapshot = _collect_fleet_snapshot(restart, _fleet_rows_expected)
        if print_fleet_version_matrix(_fleet_snapshot):
            restart.incomplete = True
        elif not _fleet_snapshot and _fleet_rows_expected:
            # collect_fleet_versions() swallows every failure, so zero rows with
            # expected runtimes is indistinguishable from health — fail (partial, exit 1).
            print(
                # Fleet probe returned zero rows even though at least one gateway runtime was (or may have
                # been) live pre-update — POSIX restart bookkeeping, the pre-restart PID snapshot, the
                # pre-update plan inventory, or the Windows pause/resume token all count as that signal.
                # Every failure path inside collect_fleet_versions() is swallowed via logger.debug(), so an
                # empty list is indistinguishable from a healthy fleet in the current output. Treat it as
                # verification failure so the receipt records "partial" and the exit code is 1 (#93406).
                "\n⚠ Fleet version check returned no rows even though"
                " gateway runtimes were expected — verification incomplete."
            )
            restart.incomplete = True

    # Every runtime the PLAN saw must appear in restart bookkeeping; an
    # unaccounted one is a silent miss and escalates like a STALE/DOWN row.
    with _best_effort('Runtime-outcome reconciliation failed: %s'):
        # An unaccounted runtime is the silent-miss class (a platform branch re-discovered its own targets
        # and skipped one the inventory knew about) — escalate it exactly like a STALE/DOWN fleet row. See
        # #91277.
        if _pre_update_plan is not None and _pre_update_plan.runtimes:
            from hermes_cli.update_inventory import (match_runtime_outcomes, report_unaccounted_runtimes)
            _runtime_outcomes = match_runtime_outcomes(
                _pre_update_plan,
                restarted_services=restart.restarted_services,
                relaunched_profiles=restart.relaunched_profiles,
                externally_supervised_profiles=restart.externally_supervised_profiles,
                killed_pids=restart.killed_pids,
                failed_units=restart.failed_or_stale_units,
                # Serve/dashboard reconcile by incarnation liveness, not unit names.
                # See #100479.
                stale_serve_pids=(
                    {row.get("pid") for row in _stale_serve_rows}
                    if _stale_serve_rows is not None
                    else None
                ),
            )
            if report_unaccounted_runtimes(_runtime_outcomes):
                restart.incomplete = True
            with suppress(Exception):
                import hermes_cli.update_receipt as _ur
                if _ur._current is not None:
                    _ur._current.data["runtime_outcomes"] = _runtime_outcomes

    with _best_effort('Update receipt finalize failed: %s'):
        from hermes_cli.update_receipt import finalize_update_receipt
        _receipt_path = finalize_update_receipt(
            "partial" if restart.incomplete or not update_complete else "success",
            fleet=_fleet_snapshot,
        )
        if _receipt_path is not None:
            logger.info("Update receipt written: %s", _receipt_path)

    if restart.incomplete:
        # Code updated but a gateway may still run stale modules: fail so automation
        # doesn't treat the fleet as healthy; leave the pending marker for catch-up.
        sys.exit(1)
    _clear_fleet_restart_pending_marker()


def _restart_phase_failure_is_incomplete(surviving, pre_restart_pids) -> bool:
    """Whether an escaped restart-phase exception must fail the update.

    Fail closed unless provably safe: ``surviving`` None (unprobeable) or non-empty →
    stale. ``[]`` proves safety ONLY if nothing ran beforehand; a pre-restart gateway
    (``pre_restart_pids`` non-empty or None) now gone was stopped unverified.

    * ``surviving is None`` — the survivor probe could not determine state (typically the freshly-pulled
    ``hermes_cli.gateway`` no longer imports, one of the ways the phase aborts). That is proof-of-safety
    ONLY when nothing was running before we touched anything. If a gateway was discovered pre-restart
    (``pre_restart_pids`` non-empty, or ``None`` meaning the pre-state could not be read), it was stopped
    without a verified replacement, so we still fail closed (#78574).
    """
    if surviving is None or surviving:
        return True
    return pre_restart_pids is None or bool(pre_restart_pids)


def _fleet_probe_expected_runtimes(
    pre_update_plan, pre_restart_pids, windows_resume_token, restarted_services, killed_pids,
) -> bool:
    """Whether the post-update fleet probe should have produced rows.

    ``collect_fleet_versions()`` swallows every failure and an empty matrix prints as
    healthy, so zero rows is only proof-of-safety when NOTHING says a gateway existed
    pre-update. Signals: ``restarted_services``/``killed_pids``; ``pre_restart_pids``
    non-empty or None (same contract as ``_restart_phase_failure_is_incomplete``); plan
    inventoried ≥1 ``kind == "gateway"`` runtime. ``windows_resume_token`` is deliberately EXCLUDED: it is
    pause/resume bookkeeping, not an inventory, and its entries don't map to probe rows
    (``unmapped`` Scheduled-Task gateways never publish gateway_state.json; a paused
    profile resumes DETACHED). Counting it made every Windows update that paused a
    gateway exit 1 after a long silent wait; a live pre-update Windows gateway is already
    covered by ``pre_restart_pids`` and the plan. The same condition gates the settle sleep.

    See #93406.
    See #78574.
    See #93406.
    """
    del windows_resume_token  # excluded on purpose — see docstring
    # See #93406.
    if restarted_services or killed_pids:
        return True
    if pre_restart_pids is None or pre_restart_pids:
        return True
    with suppress(Exception):
        # Gateway-kind only: serve/dashboard plan records never publish a gateway_state.json
        # row, so a dashboard-only plan cannot ground a rows-expected verdict (#97332).
        if pre_update_plan is not None and any(
            getattr(runtime, "kind", None) == "gateway" for runtime in pre_update_plan.runtimes
        ):
            return True
    return False


def _wait_for_service_active(scope_cmd_: list, svc_name_: str, timeout: float = 10.0) -> bool:
    """Poll ``systemctl is-active`` (0.5s) up to ``timeout``: the Stopped -> Started
    transition isn't instantaneous, so a one-shot check falsely reports down."""
    deadline = _time.monotonic() + max(timeout, 0.5)
    while True:
        with suppress(FileNotFoundError, subprocess.TimeoutExpired):
            _verify = _systemctl(scope_cmd_ + ["is-active", svc_name_], timeout=5)
            if _verify.stdout.strip() == "active":
                return True
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.5)


_RESTART_SEC_UNITS = (("ms", 0.001), ("us", 0.000001), ("min", 60.0), ("s", 1.0))


def _service_restart_sec(scope_cmd_: list, svc_name_: str, default: float = 0.0) -> float:
    """Read the unit's ``RestartUSec`` in seconds. ``is-active`` pollers must wait
    >= RestartSec + slack or they give up *during* the cooldown and misreport."""
    try:
        _show = _systemctl(scope_cmd_ + ["show", svc_name_, "--property=RestartUSec", "--value"], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return default
    raw = (_show.stdout or "").strip()
    # Values like "30s", "100ms", "1min 30s", "infinity"; on any miss return default.
    if not raw or raw == "infinity":
        return default
    total = 0.0
    matched = False
    for part in raw.split():
        for _suf, _mult in _RESTART_SEC_UNITS:
            if part.endswith(_suf):
                with suppress(ValueError):
                    total += float(part[: -len(_suf)]) * _mult
                    matched = True
                break
    return total if matched else default


# ─────────────────────────────────────────────────────────────────────────────
# Fork: prepared-generation + strict activation (``--defer-restart`` /
# ``hermes auto_update activate``). The AUTHORITATIVE prepared-generation record lives in its own
# file, ``fleet_restart_prepared``, never inside the generic breadcrumb — so a pull-time marker
# write from a re-prepare attempt that then fails cannot overwrite a COMPLETED preparation.
# ─────────────────────────────────────────────────────────────────────────────

_FLEET_RESTART_PREPARED_NAME = "fleet_restart_prepared"

#: Schema of the prepared-generation record a *completed* ``--defer-restart`` run publishes.
_PREPARED_GENERATION_SCHEMA = 1
_PREPARED_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z|\A[0-9a-fA-F]{64}\Z")
_PREPARED_GENERATION_RE = re.compile(r"\A[0-9a-f]{8,64}\Z")
_PREPARED_RECEIPT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PREPARED_RECORD_MAX_BYTES = 16 * 1024
_PREPARED_RECORD_FIELDS = frozenset(
    {"schema", "generation", "expected_sha", "receipt", "prepared", "restart", "prepared_at", "pid"})

#: Post-restart verification poll budget for strict activation. The budget
#: exists for a real freshly restarted runtime to stamp itself, not to wait
#: out a fleet that never comes back.
_ACTIVATION_VERIFY_BUDGET_SECONDS = 15.0

#: Serve/dashboard supervisors whose lifecycle the pending-restart machinery
#: owns: the ``hermes-serve*`` systemd units it restarts alongside the
#: gateway units. Desktop-supervised and manually-launched backends are
#: deliberately excluded — the stock restart phase leaves those to their own
#: supervisor, so requiring them here would demand what nothing restarts.
_MANAGED_SERVE_SUPERVISORS = frozenset({"systemd"})


def _m():  # origin-module accessor, matching the ported block's idiom
    from hermes_cli.update_cmd import _m as _origin_m
    return _origin_m()


def _origin():  # hermes_cli.update_cmd — fork-era tests patch this block's boundary names there
    from hermes_cli import update_cmd
    return update_cmd


def get_hermes_home():  # lazy origin shim — tests patch hermes_cli.update_cmd.get_hermes_home
    from hermes_cli.update_cmd import get_hermes_home as _impl
    return _impl()


def _capture_head_sha(git_cmd, cwd):  # origin-module helper, lazy to avoid the import cycle
    from hermes_cli.update_cmd import _capture_head_sha as _impl
    return _impl(git_cmd, cwd)


def _normalize_prepared_sha(value) -> str:
    """Canonical (lowercase) full git object id, or ``""`` when not exactly one.

    Accepts only the strict 40/64-hex shapes; mixed-case input is normalized
    so marker, receipt and ``git rev-parse`` output compare like with like.
    Whitespace, newlines, prefixes and abbreviations are rejected, not
    trimmed.
    """
    text = str(value or "")
    return text.lower() if _PREPARED_SHA_RE.match(text) else ""


def _fleet_restart_prepared_path() -> Path:
    """HERMES_HOME record of the authoritative prepared generation.

    Distinct from :func:`_fleet_restart_pending_marker_path`: the generic
    breadcrumb is rewritten by every pull, while this file is written only
    by a completed preparation and read only by strict activation.
    """
    return get_hermes_home() / _FLEET_RESTART_PREPARED_NAME


def _atomic_write_text(path: Path, text: str) -> None:
    """Durably publish ``text`` at ``path`` via the one strict helper.

    Unique same-directory staging with exclusive creation, full-write loop,
    file fsync, atomic ``os.replace``, parent-directory fsync on POSIX and an
    exact read-back — every failure raises :class:`OSError` (a POSIX file or
    directory fsync failure is a hard publication failure, never a
    log-and-continue). Callers that only ever wanted a best-effort
    breadcrumb catch it themselves. The staged sibling is removed on every
    failure path so no ``*.tmp.*`` litter survives.
    """
    durable_publish_bytes(path, text.encode("utf-8"))


def _read_fleet_restart_pending_marker() -> dict[str, str]:
    """Parse the pending-restart breadcrumb into ``key -> value`` fields.

    Empty when the marker is absent, unreadable, or malformed — callers must
    treat that as "no proof", never as "nothing pending".
    """
    try:
        text = _fleet_restart_pending_marker_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields


@dataclass(frozen=True)


class PreparedGeneration:
    """A strictly parsed ``fleet_restart_prepared`` generation record.

    Binds the restart obligation to one exact checkout SHA and to the one
    durable receipt that proves the preparation finished: the receipt
    carries the same generation id, so a record whose receipt was replaced
    by any later update run no longer validates.
    """

    schema: int
    generation: str
    expected_sha: str
    receipt: str


def _read_prepared_record_fields() -> dict[str, str] | None:
    """Strictly parse the prepared-generation record file into fields.

    ``None`` — "no proof", never "nothing pending" — for anything short of
    an intact document: a missing, unreadable, empty or oversize file, a
    line that is not ``key=value``, a duplicate key, an unknown key (a field
    this schema never writes could be a security-relevant injection), CRLF
    or trailing garbage. The generic marker reader stays lenient; the
    authoritative record does not.
    """
    try:
        raw = _fleet_restart_prepared_path().read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _PREPARED_RECORD_MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n") or "\r" in text:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or not key:
            return None
        if key in fields or key not in _PREPARED_RECORD_FIELDS:
            return None
        # Values are taken verbatim — the writer never pads them, so
        # surrounding whitespace is tampering the field validators reject,
        # not something to normalize away.
        fields[key] = value
    return fields


def _parse_prepared_generation() -> PreparedGeneration | None:
    """Strictly parse the prepared record as a published prepared generation.

    Anything short of a complete, well-typed, known-schema record reads as
    unprepared: a missing, malformed, truncated, unreadable, wrong-typed, or
    incomplete record, an unknown schema version, or a bare ``prepared=yes``
    line. ``None`` means "no proof" — callers must fail closed on it, never
    treat it as "nothing pending" *or* as activatable.
    """
    fields = _read_prepared_record_fields()
    if not fields:
        return None
    if fields.get("schema") != str(_PREPARED_GENERATION_SCHEMA):
        return None
    generation = fields.get("generation", "")
    expected_sha = _normalize_prepared_sha(fields.get("expected_sha", ""))
    receipt = fields.get("receipt", "")
    if not _PREPARED_GENERATION_RE.match(generation):
        return None
    if not expected_sha:
        return None
    if not _PREPARED_RECEIPT_RE.match(receipt) or receipt in {".", ".."}:
        return None
    if fields.get("prepared") != "yes":
        return None
    if fields.get("restart") != "pending":
        return None
    return PreparedGeneration(
        schema=_PREPARED_GENERATION_SCHEMA,
        generation=generation,
        expected_sha=expected_sha,
        receipt=receipt,
    )


def _fleet_restart_pending_prepared() -> bool:
    """Durable proof that the pending update's preparation finished.

    True only when the authoritative prepared record is a complete prepared
    generation (see :func:`_parse_prepared_generation`) AND the receipt it
    names still exists and independently claims the same generation, target
    SHA and pending restart — published by a ``--defer-restart`` run after
    pull, dependency sync, builds, migrations and skill sync all succeeded
    and the record read back intact. False for the generic marker a plain or
    interrupted update leaves behind (HEAD advanced, preparation state
    unknown) — a generic obligation alone is never enough — for a record
    whose receipt was replaced by any later update run, and for a pending
    obligation inferred from a skewed receipt, where nothing proves
    anything.
    """
    generation = _parse_prepared_generation()
    return generation is not None and (
        _read_prepared_generation_receipt(generation) is not None
    )


def _current_checkout_head() -> str | None:
    """Current checkout HEAD, probed directly in ``PROJECT_ROOT``.

    Deliberately not ``get_code_identity()``: that resolves the tree the
    *running interpreter* was imported from, which is not necessarily the
    one ``hermes update`` mutates. The prepared generation compares like
    with like — its ``expected_sha`` came from this same probe.
    """
    return _capture_head_sha(["git"], _m().PROJECT_ROOT)


def _read_prepared_generation_receipt(
    generation: PreparedGeneration,
) -> dict | None:
    """Load and validate the receipt a prepared generation is bound to.

    None unless the named receipt exists, parses, and independently claims
    the same schema, generation id, target SHA, and a pending restart for a
    successful, unfinished-free update run.
    """
    from hermes_cli.update_receipt import read_named_receipt

    receipt = read_named_receipt(generation.receipt)
    if receipt is None:
        return None
    bound = receipt.get("prepared_generation")
    if not isinstance(bound, dict):
        return None
    if bound.get("schema") != _PREPARED_GENERATION_SCHEMA:
        return None
    if str(bound.get("generation") or "") != generation.generation:
        return None
    if str(bound.get("expected_sha") or "") != generation.expected_sha:
        return None
    if bound.get("restart") != "pending":
        return None
    if receipt.get("outcome") != "success":
        return None
    # Stricter than the stock recovery heuristic: there, a clean boundary
    # stamp (``completed at command boundary``, ``sys.exit(0)``) must not
    # make a successful receipt look unfinished (#98022), so
    # ``_receipt_looks_unfinished`` ignores stop_reason when something
    # vouches for success. This gate is the proof a preparation FINISHED —
    # any stop_reason at all means the run ended through an exit path and
    # cannot vouch for the generation.
    if receipt.get("stop_reason"):
        return None
    if _receipt_looks_unfinished(receipt):
        return None
    return receipt


def _publish_prepared_generation() -> tuple[bool, str]:
    """Publish the durable prepared generation for a finished deferred run.

    Ordering is the contract: the matching receipt is finalized — and
    therefore durably on disk (atomic write, fsync, directory fsync,
    read-back) — FIRST, then the prepared record is published atomically as
    the commit record pointing at it, then the record is read back and
    strictly re-parsed. Every step is verified against the live checkout, so
    a concurrent HEAD move between the pull and here fails the publication
    instead of stamping a stale generation.

    The record lands in ``fleet_restart_prepared`` — never in the generic
    pull-time marker — so a later re-prepare that fails or times out after
    its own pull-time marker write cannot destroy the generation this run
    published; a later COMPLETED preparation atomically replaces it.

    Returns ``(True, "")`` on success; any failure returns ``(False,
    reason)`` and the caller must exit nonzero — a preparation whose
    readiness cannot be proven durable is not a completed preparation.
    """
    expected_sha = _normalize_prepared_sha(
        _read_fleet_restart_pending_marker().get("expected_sha", "")
    )
    if not expected_sha:
        return False, "the pull-time marker records no full expected_sha"
    head = _origin()._current_checkout_head()
    if not head or head.lower() != expected_sha:
        return False, (
            f"checkout HEAD is {head or 'unresolvable'}, expected {expected_sha}"
        )

    generation = uuid.uuid4().hex
    from hermes_cli.update_receipt import (
        amend_receipt_outcome,
        finalize_update_receipt,
        record_prepared_generation,
    )

    record_prepared_generation(generation, expected_sha)
    receipt_path = finalize_update_receipt("success")
    if receipt_path is None:
        return False, "the matching update receipt could not be made durable"

    record = _fleet_restart_prepared_path()
    body = (
        f"schema={_PREPARED_GENERATION_SCHEMA}\n"
        f"generation={generation}\n"
        f"expected_sha={expected_sha}\n"
        f"receipt={receipt_path.name}\n"
        "prepared=yes\n"
        "restart=pending\n"
        f"prepared_at={int(_time.time())}\n"
        f"pid={os.getpid()}\n"
    )
    try:
        _origin()._atomic_write_text(record, body)
    except OSError as exc:
        amend_receipt_outcome(
            receipt_path,
            outcome="failed",
            error=f"prepared record write failed: {exc}",
        )
        return False, f"the prepared record could not be written durably: {exc}"
    parsed = _origin()._parse_prepared_generation()
    if (
        parsed is None
        or parsed.generation != generation
        or parsed.receipt != receipt_path.name
        or parsed.expected_sha != expected_sha
    ):
        amend_receipt_outcome(
            receipt_path,
            outcome="failed",
            error="prepared record read-back validation failed",
        )
        return False, "the prepared record failed read-back validation"
    return True, ""


def _clear_prepared_generation_strict() -> int:
    """Clear the pending obligation, refusing to claim success it can't prove.

    Unlike the best-effort clear (fine for the stock updater's own recovery
    path), this verifies BOTH files are actually gone — the authoritative
    prepared record and the generic breadcrumb: a permission error or a
    read-only home must leave the obligation pending and return nonzero, not
    silently reschedule a restart that already happened.
    """
    rc = 0
    for path in (_fleet_restart_pending_marker_path(), _fleet_restart_prepared_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠ Could not clear {path.name}: {exc}")
            rc = 1
            continue
        try:
            still_there = path.exists()
        except OSError as exc:
            print(f"⚠ Could not re-check {path.name}: {exc}")
            rc = 1
            continue
        if still_there:
            print(f"⚠ {path.name} is still present after clearing it.")
            rc = 1
    if rc == 0:
        print("  ✓ Pending fleet restart completed.")
    return rc


def _expected_runtimes_from_plan(plan) -> tuple[Optional[set], list, list]:
    """Split a receipt plan into the runtimes strict activation must account for.

    Returns ``(gateway_profiles, managed_serve, unmanaged_serve)``.
    ``gateway_profiles`` is ``None`` when the plan itself is unusable —
    missing, malformed, or without a ``runtimes`` list. That is "no
    evidence", which is deliberately NOT the same value as the empty set an
    explicitly valid empty plan produces: a prepare that saw nothing running
    proves there is nothing to restart, a receipt nobody can parse proves
    nothing. Malformed entries make the whole plan unusable for the same
    reason — an entry that cannot be read could be any runtime.
    """
    if not isinstance(plan, dict):
        return None, [], []
    runtimes = plan.get("runtimes")
    if not isinstance(runtimes, list):
        return None, [], []
    gateways: set = set()
    managed: list = []
    unmanaged: list = []
    for runtime in runtimes:
        kind = runtime.get("kind") if isinstance(runtime, dict) else None
        profile = runtime.get("profile") if isinstance(runtime, dict) else None
        if not isinstance(kind, str) or not isinstance(profile, str) or not profile:
            return None, [], []
        if kind == "gateway":
            gateways.add(profile)
        elif kind in ("serve", "dashboard"):
            supervisor = str(runtime.get("supervisor") or "")
            if supervisor in _MANAGED_SERVE_SUPERVISORS:
                managed.append(dict(runtime))
            else:
                unmanaged.append(dict(runtime))
    return gateways, managed, unmanaged


def _inspect_live_fleet_strict() -> dict:
    """Probe the live fleet, raising on any inspection failure.

    Gateways come from the strict fleet-version snapshot (which raises where
    the historical one shrugs), serve/dashboard backends from the spawn
    ledger (live-verified ``(pid, create_time)`` pairs). Callers must treat
    an exception as "the fleet state is unknown", never as "empty".
    """
    from hermes_cli.update_receipt import collect_fleet_versions_strict

    fleet = collect_fleet_versions_strict()
    from hermes_cli.process_identity import ledger_entries

    ledger = ledger_entries()
    return {"fleet": fleet, "ledger": ledger}


def _live_ledger_rows(ledger: list) -> list[dict]:
    """Live serve/dashboard rows from the spawn ledger, deterministically ordered.

    One row per ledger ENTRY, so a pid the log-shaped file records twice
    yields two rows. A pid is still one live process, never two matchable
    runtimes — the injective matching and the duplicate-identity check
    below enforce that on process identity, not on this row count.
    """
    rows: list[dict] = []
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        purpose = entry.get("purpose")
        if purpose not in ("serve", "dashboard"):
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        rows.append(
            {
                "kind": str(purpose),
                "profile": str(entry.get("profile") or "default"),
                "pid": pid,
                "host": str(entry.get("host") or ""),
                "port": entry.get("port"),
            }
        )
    rows.sort(key=lambda row: (row["kind"], row["profile"], row["pid"]))
    return rows


def _planned_runtime_identity(runtime: dict) -> tuple | None:
    """Stable instance identity the plan recorded, when it recorded one.

    Serve/dashboard plan rows carry the spawn-ledger ``host``/``port`` the
    backend registered with (#63206) — the identity of the instance across a
    restart, unlike its pid. Present only when both fields are usable.
    """
    detail = runtime.get("detail")
    if not isinstance(detail, dict):
        return None
    host = str(detail.get("host") or "")
    port = detail.get("port")
    if host and port:
        return (host, port)
    return None


def _runtime_row_compatible(planned: dict, live: dict) -> bool:
    """True when a live ledger row may satisfy one planned runtime.

    The base predicate is the plan's own kind/profile. When BOTH sides carry
    a stable instance identity, that identity is preferred and must agree —
    a live backend on a different host/port is a different instance, not
    this plan row's replacement.
    """
    if live["kind"] != str(planned.get("kind")):
        return False
    if live["profile"] != str(planned.get("profile") or "default"):
        return False
    identity = _planned_runtime_identity(planned)
    if identity is not None and live["host"] and live["port"]:
        return (live["host"], live["port"]) == identity
    return True


def _match_runtimes_injectively(planned: list, live: list) -> dict[int, int]:
    """Deterministic maximum one-to-one matching of planned rows to live pids.

    Post-restart verification is injective on live PROCESS identity, not on
    ledger-row count: every planned managed runtime must match a DISTINCT
    live process, and one live process satisfies at most one planned row —
    two planned serve backends of the same kind/profile are NOT both
    satisfied by a single live replacement, however many ledger rows
    describe it. Slots are therefore keyed by pid: a pid that appears in
    the ledger twice (identical replay or conflicting fields alike) is ONE
    slot whose identity is the first row in deterministic sort order.
    Returns ``{planned_index: live_index}`` for the first row of each
    matched slot; a planned row missing from the mapping could not be
    matched. Kuhn's augmenting-path algorithm over the slots, so the result
    does not depend on dict/ledger iteration order.
    """
    slots: list[tuple[int, dict]] = []  # (first live row index, identity row)
    slot_by_pid: dict[int, int] = {}
    for row_index, row in enumerate(live):
        pid = row["pid"]
        if pid in slot_by_pid:
            continue
        slot_by_pid[pid] = len(slots)
        slots.append((row_index, row))

    match_slot_to_planned: dict[int, int] = {}

    def _augment(planned_index: int, seen: set) -> bool:
        for slot_index, (_row_index, row) in enumerate(slots):
            if slot_index in seen:
                continue
            if not _runtime_row_compatible(planned[planned_index], row):
                continue
            seen.add(slot_index)
            previous = match_slot_to_planned.get(slot_index)
            if previous is None or _augment(previous, seen):
                match_slot_to_planned[slot_index] = planned_index
                return True
        return False

    for planned_index in range(len(planned)):
        _augment(planned_index, set())
    return {
        planned_index: slots[slot_index][0]
        for slot_index, planned_index in match_slot_to_planned.items()
    }


def _duplicate_live_identity_problems(
    planned: list, live_rows: list[dict]
) -> list[str]:
    """Fail-closed problems for ledger rows that disagree about one pid.

    The ledger's single writer prunes a pid's old rows before appending its
    new one, so two valid serve/dashboard rows for the SAME pid mean the
    file can no longer prove which instance that process is — and without
    this check, row-count matching would let one process stand in for two
    planned runtimes. Identical replays carry no ambiguity (pid-keyed
    matching collapses them into one slot); rows that disagree on identity
    fields are an explicit problem for every planned runtime they could
    have matched, so strict verification refuses to guess which identity is
    the real one. Duplicate rows for runtimes this plan never planned are
    not evidence against it and are left alone.
    """
    planned_pairs = {
        (str(runtime.get("kind")), str(runtime.get("profile") or "default"))
        for runtime in planned
    }
    rows_by_pid: dict[int, list[dict]] = {}
    for row in live_rows:
        rows_by_pid.setdefault(row["pid"], []).append(row)
    problems: list[str] = []
    for pid in sorted(rows_by_pid):
        group = rows_by_pid[pid]
        if len(group) == 1:
            continue
        identities = {
            (row["kind"], row["profile"], row["host"], row["port"]) for row in group
        }
        if len(identities) == 1:
            continue  # one identity recorded twice — collapses, nothing to guess
        if not planned_pairs & {(row["kind"], row["profile"]) for row in group}:
            continue
        problems.append(
            f"spawn ledger holds {len(group)} conflicting rows for live pid"
            f" {pid} — it cannot prove which instance that process is"
        )
    return problems


def _verify_fleet_on_expected_generation(
    snapshot: dict,
    *,
    gateway_profiles: set,
    managed_serve: list,
    expected_sha: str,
) -> list[str]:
    """Problems that stop the fleet counting as serving ``expected_sha``.

    Every runtime the prepared generation's plan saw is accounted for
    injectively: a gateway must be live and stamping exactly ``expected_sha``
    (compared directly against the sha the record binds — not the row's own
    classification, so a probe that misresolved its reference cannot wave a
    stale gateway through); a managed serve/dashboard backend must be live
    AND must no longer be the process the plan recorded, because that
    process was running before the code it now needs existed — and N planned
    backends require N distinct live replacements, so a single survivor of
    the restart can never satisfy two planned rows, no matter how many
    ledger rows describe it.

    The plan proves what must be restarted, not that nothing else is
    running: a live gateway row whose profile the plan never listed (the
    inventory degraded between prepare and activate, or the profile appeared
    since) is held to the SAME standard, because clearing the obligation
    while such a gateway is down, unproven, or serving another sha would
    strand it there with ``problems == []`` (Codex P1 A). Only an extra
    gateway already stamping exactly ``expected_sha`` is genuinely fine.
    """
    problems: list[str] = []
    rows = {
        str(entry.get("profile")): entry
        for entry in (snapshot.get("fleet") or [])
        if isinstance(entry, dict)
    }

    def _gateway_problem(profile: str, row: dict) -> Optional[str]:
        # Shared by planned and unplanned profiles, so the fail-closed rule
        # for a gateway the plan omitted cannot drift from the rule for one
        # it named. Compared directly against the sha the record binds —
        # not the row's own current/stale/unknown classification.
        if row.get("state") == "down":
            return f"gateway for profile '{profile}' is down"
        sha = row.get("code_sha")
        if not isinstance(sha, str) or not sha:
            return f"gateway for profile '{profile}' does not report a code sha"
        if sha != expected_sha:
            return (
                f"gateway for profile '{profile}' runs {sha[:10]},"
                f" not {expected_sha[:10]}"
            )
        return None

    for profile in sorted(gateway_profiles):
        row = rows.get(profile)
        if row is None:
            problems.append(f"no live gateway for profile '{profile}'")
            continue
        problem = _gateway_problem(profile, row)
        if problem is not None:
            problems.append(problem)
    for profile in sorted(rows):
        if profile in gateway_profiles:
            continue
        problem = _gateway_problem(profile, rows[profile])
        if problem is not None:
            problems.append(
                f"{problem} — live gateway not in the prepared plan"
            )
    live_rows = _live_ledger_rows(snapshot.get("ledger") or [])
    problems.extend(_duplicate_live_identity_problems(managed_serve, live_rows))
    matching = _match_runtimes_injectively(managed_serve, live_rows)
    for index, runtime in enumerate(managed_serve):
        kind = str(runtime.get("kind"))
        profile = str(runtime.get("profile") or "default")
        planned_pid = runtime.get("pid")
        if isinstance(planned_pid, int) and any(
            row["pid"] == planned_pid
            and row["kind"] == kind
            and row["profile"] == profile
            for row in live_rows
        ):
            problems.append(
                f"{kind} backend for profile '{profile}' still runs the"
                f" pre-update process (pid {planned_pid})"
            )
            continue
        live_index = matching.get(index)
        if live_index is None:
            if not any(
                _runtime_row_compatible(runtime, row) for row in live_rows
            ):
                problems.append(
                    f"{kind} backend for profile '{profile}' is not running"
                )
            else:
                problems.append(
                    f"{kind} backend for profile '{profile}' has no distinct"
                    " live instance — one live runtime cannot satisfy two"
                    " planned ones"
                )
    return problems


def _activation_refuses(reason: str, *, hint: str = "hermes update") -> int:
    """Print one fail-closed refusal and return the nonzero exit code."""
    print(f"⚠ {reason} —")
    print("  not activating it. Run `" + hint + "` to finish (or repair)")
    print("  the preparation first.")
    return 1


def _activate_pending_fleet_restart_strict() -> int:
    """Fail-closed activation of a prepared update (auto-update Phase B).

    Returns a process exit code. Nothing here restarts anything until the
    durable state proves what should be restarted: the authoritative
    prepared record must parse as a complete prepared generation (the
    generic marker alone is never enough), the bound receipt must still
    exist and agree with it, the checkout HEAD must still be the
    generation's SHA, and the plan must name every runtime the restart has
    to account for. The record (and with it the obligation) is cleared only
    when the live fleet is demonstrably serving that SHA — either because
    it already was (the manual ``/restart`` case, cleared with zero
    restarts) or after the stock restart machinery ran and an independent
    re-inspection confirmed every managed runtime came back on the new
    generation. Every other outcome keeps the obligation and returns
    nonzero.
    """
    if not _origin()._pending_fleet_restart_needed():
        return 0
    generation = _parse_prepared_generation()
    if generation is None:
        return _activation_refuses(
            "Update is pending but its prepared generation cannot be verified"
        )
    head = _origin()._current_checkout_head()
    if not head or head != generation.expected_sha:
        print("⚠ The checkout no longer matches the prepared update —")
        print(
            f"  prepared for {generation.expected_sha[:10]},"
            f" HEAD is {(head or 'unresolvable')[:10]}."
        )
        print("  Leaving it pending; run `hermes update` to re-prepare.")
        return 1
    receipt = _read_prepared_generation_receipt(generation)
    if receipt is None:
        return _activation_refuses(
            "The receipt bound to the prepared update is missing or unreadable"
        )
    gateway_profiles, managed_serve, _unmanaged = _expected_runtimes_from_plan(
        receipt.get("plan")
    )
    if gateway_profiles is None:
        return _activation_refuses(
            "The prepared update's runtime plan cannot be verified"
        )
    expected_sha = generation.expected_sha
    try:
        snapshot = _origin()._inspect_live_fleet_strict()
    except Exception as exc:
        print("⚠ Could not inspect the running fleet — not activating anything.")
        print(f"  ({exc})")
        return 1
    problems = _verify_fleet_on_expected_generation(
        snapshot,
        gateway_profiles=gateway_profiles,
        managed_serve=managed_serve,
        expected_sha=expected_sha,
    )
    if not problems:
        print("→ Live fleet already runs the prepared code — clearing the pending restart.")
        return _clear_prepared_generation_strict()
    print("→ Running the pending fleet restart...")
    if not _origin()._run_pending_fleet_restart():
        print("  ⚠ Fleet restart incomplete. Recover with: hermes gateway restart")
        return 1
    return _verify_activation_restart_completed(
        gateway_profiles=gateway_profiles,
        managed_serve=managed_serve,
        expected_sha=expected_sha,
    )


def _verify_activation_restart_completed(
    *,
    gateway_profiles: set,
    managed_serve: list,
    expected_sha: str,
) -> int:
    """Re-inspect the fleet after a restart and only then clear the obligation.

    A restart command returning cleanly is not proof: units can fail to come
    back, and a gateway that dies before stamping its code identity would
    otherwise clear the marker on the strength of a bookkeeping entry. The
    poll budget only covers a freshly restarted runtime taking a moment to
    stamp itself; anything still unverified after it keeps the obligation
    (and the next activation retries against the real fleet, so a partial
    restart converges instead of double-clearing).
    """
    deadline = _time.monotonic() + _origin()._ACTIVATION_VERIFY_BUDGET_SECONDS
    problems: list[str] = []
    while True:
        try:
            snapshot = _origin()._inspect_live_fleet_strict()
        except Exception as exc:
            print("⚠ Could not re-inspect the fleet after the restart —")
            print("  the pending restart is kept for the next tick.")
            print(f"  ({exc})")
            return 1
        problems = _verify_fleet_on_expected_generation(
            snapshot,
            gateway_profiles=gateway_profiles,
            managed_serve=managed_serve,
            expected_sha=expected_sha,
        )
        if not problems:
            return _clear_prepared_generation_strict()
        if _time.monotonic() >= deadline:
            break
        _time.sleep(1.0)
    print("  ⚠ The restart did not bring every managed runtime onto the")
    print("    prepared code — the pending restart is kept:")
    for problem in problems:
        print(f"    • {problem}")
    print("  Recover with: hermes gateway restart")
    return 1


def _publish_no_update_repair_generation() -> tuple[bool, str]:
    """Publish a prepared generation for a *no-update* repair run.

    A repair on an already-current checkout (unhealthy venv, or the sync
    handed off by the Windows shim) rewrote dependencies the running fleet
    still has loaded, so it owes the same SHA-bound restart obligation a pull
    does — bound to the CURRENT HEAD, because no pull happened. Writing that
    obligation here is not inventing one: the repair demonstrably ran.

    A valid prepared generation already bound to this HEAD is preserved
    untouched — re-publishing would mint a new generation id and orphan the
    receipt the existing record points at, for no new information.
    """
    head = _normalize_prepared_sha(_origin()._current_checkout_head() or "")
    if not head:
        return False, "current HEAD could not be resolved to a full SHA"
    existing = _parse_prepared_generation()
    if existing is not None and existing.expected_sha == head:
        return True, ""
    if not _origin()._write_fleet_restart_pending_marker(expected_sha=head):
        return False, "the repair's restart obligation could not be written"
    return _origin()._publish_prepared_generation()


def _finish_deferred_restart(
    *,
    prepared_update: bool,
    defects: list[str] | None = None,
    repaired_runtime: bool = False,
) -> None:
    """Close out a ``--defer-restart`` run. Never restarts anything.

    ``prepared_update`` is True when this run advanced HEAD and therefore owes
    the fleet a restart; ``repaired_runtime`` when a no-update repair rewrote
    the venv the running fleet still has loaded. ``defects`` collects every
    required preparation step that failed or returned a partial result — with
    any entry present the run is *not* prepared.

    Readiness is published here and nowhere else, via
    :func:`_publish_prepared_generation`: the matching receipt is finalized
    and durable FIRST, then the marker is atomically swapped in as the commit
    record and read back. A run that fails, times out, or dies before this
    point leaves the marker unstamped, and ``hermes auto_update activate``
    refuses it — the cross-tick hazard is the next up-to-date tick restarting
    the fleet onto a half-prepared checkout just because the marker existed.

    Publishing is a hard requirement, not a nicety: when the prepared
    generation cannot be proven durable, this exits 1 rather than print a
    success line, because a preparation whose readiness cannot be proven is
    not a completed preparation.
    """
    defects = [str(d) for d in (defects or [])]
    pending = False
    try:
        pending = _fleet_restart_pending_marker_path().is_file()
    except OSError:
        pending = False
    # The obligation must come from this run's own evidence — the pull-time
    # breadcrumb or an actual repair. Never from an inferred "HEAD is ahead".
    owed = (prepared_update and pending) or repaired_runtime
    ready, reason = False, ""
    try:
        from hermes_cli.update_receipt import record_skip, record_step

        record_step(
            "defer_restart",
            not defects,
            "prepared_update=%s pending_marker=%s repaired_runtime=%s defects=%d"
            % (prepared_update, pending, repaired_runtime, len(defects)),
        )
        record_skip(
            "fleet_restart",
            "deferred by --defer-restart"
            if (prepared_update or repaired_runtime)
            else "skipped by --defer-restart (checkout already up to date)",
        )
    except Exception as exc:
        logger.debug("Could not record deferred restart outcome: %s", exc)

    if owed and not defects:
        if prepared_update and pending:
            ready, reason = _origin()._publish_prepared_generation()
        else:
            ready, reason = _origin()._publish_no_update_repair_generation()

    failed = bool(defects) or (owed and not ready)
    print()
    if defects:
        print("✗ Preparation did not complete — the update is NOT prepared:")
        for defect in defects:
            print(f"    • {defect}")
    elif ready and repaired_runtime and not prepared_update:
        print("✓ Dependencies repaired — fleet restart deferred.")
    elif ready:
        print("✓ Update prepared — fleet restart deferred.")
    elif owed:
        print("✗ Preparation finished, but its prepared generation could not")
        print(f"  be published durably: {reason}")
    else:
        print("✓ Checkout already up to date — nothing to prepare.")

    if ready:
        print("  Restart pending: the next idle auto-update activation (or a")
        print("  plain `hermes update`) restarts the fleet onto this code.")
    elif failed or pending:
        print("  Restart pending from an incomplete preparation — it stays")
        print("  inactive until a full prepare succeeds: hermes update")
    else:
        print("  No fleet restart is pending.")

    if failed:
        stop_reason = "; ".join(defects) or reason or "prepared generation not durable"
        try:
            from hermes_cli.update_receipt import finalize_update_receipt

            finalize_update_receipt("failed", stop_reason=stop_reason)
        except Exception as exc:
            logger.debug("Could not fail the deferred-run receipt: %s", exc)
        sys.exit(1)

