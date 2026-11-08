"""Systemd unit rendering and idempotent reconciliation for drift_watch.

Scope detection and timer/probe mechanics come from ``plugins.auto_update``
(imported, not duplicated) so both schedulers always agree on system-vs-user
installs and systemctl behavior. Only drift-watch's unit names and unit bodies
stay local.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_constants import display_hermes_home, get_hermes_home

from plugins.auto_update.platform import (
    InstallScope,
    build_systemctl_cmd,
    detect_install_scope,
    platform_supported,
    resolve_python_executable,
)
from plugins.auto_update.systemd import (  # noqa: F401 — shared systemd surface
    ProbeOutcome,
    ProbeResult,
    _classify_is_active_probe,
    _classify_is_enabled_probe,
    _reconcile_probe_fields,
    _systemd_quote,
    atomic_write,
    default_systemctl_runner,
    disable_timer as _disable_timer,
    expected_timer_disable_argv as _expected_timer_disable_argv,
    linger_warning,
    probe_timer_is_active as _probe_timer_is_active,
    probe_timer_is_enabled as _probe_timer_is_enabled,
    user_linger_enabled,
    write_units_if_changed as _write_units_if_changed,
)
from plugins.drift_watch.config import (
    DEFAULT_SCHEDULE_CALENDAR,
    load_drift_watch_config,
    plugin_explicitly_disabled,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-drift-watch.service"
TIMER_NAME = "hermes-drift-watch.timer"

# Bind only the unit identities; the implementations and probe result types
# remain shared with auto_update.
write_units_if_changed = partial(
    _write_units_if_changed,
    service_name=SERVICE_NAME,
    timer_name=TIMER_NAME,
)
probe_timer_is_active = partial(_probe_timer_is_active, timer_name=TIMER_NAME)
probe_timer_is_enabled = partial(_probe_timer_is_enabled, timer_name=TIMER_NAME)
expected_timer_disable_argv = partial(
    _expected_timer_disable_argv, timer_name=TIMER_NAME
)
disable_timer = partial(_disable_timer, timer_name=TIMER_NAME)


@dataclass(frozen=True)
class ReconcileResult:
    supported: bool
    scope: InstallScope | None
    changed: bool
    enabled: bool
    timer_active: bool
    warnings: tuple[str, ...]
    inert: bool = False
    enabled_known: bool = True
    timer_active_known: bool = True


def service_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / SERVICE_NAME


def timer_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / TIMER_NAME


def format_exec_start(argv: Sequence[str]) -> str:
    return " ".join(_systemd_quote(part) for part in argv)


def format_environment(key: str, value: str) -> str:
    return f"Environment={_systemd_quote(f'{key}={value}')}"


def unit_exec_start_argv() -> list[str]:
    return [resolve_python_executable(), "-m", "hermes_cli.main", "drift_watch", "run"]


def render_service_unit(
    *,
    hermes_home: str,
    tree: str,
    exec_start: Sequence[str],
    scope: InstallScope | None = None,
) -> str:
    exec_line = format_exec_start(exec_start)
    working_dir = tree.replace("%", "%%")
    if any(ch in working_dir for ch in ' \t\n"\\$'):
        working_dir = _systemd_quote(working_dir)
    identity = ""
    wanted_by = "WantedBy=multi-user.target"
    if scope and not scope.system:
        wanted_by = "WantedBy=default.target"
    elif scope and scope.system:
        try:
            import grp
            import pwd

            st = Path(hermes_home).stat()
            user = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            identity = f"User={user}\nGroup={group}\n"
        except (ImportError, KeyError, OSError):
            pass
    return f"""[Unit]
Description=Hermes live-tree drift watch (oneshot)

[Service]
Type=oneshot
{identity}ExecStart={exec_line}
WorkingDirectory={working_dir}
{format_environment("HERMES_HOME", hermes_home)}
{format_environment("HERMES_PROJECT", tree)}
StandardOutput=journal
StandardError=journal
TimeoutStartSec=10min

[Install]
{wanted_by}
"""


def render_timer_unit(*, schedule: str) -> str:
    return f"""[Unit]
Description=Hermes live-tree drift watch schedule

[Timer]
OnCalendar={schedule}
RandomizedDelaySec=0
AccuracySec=1s
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _resolve_systemctl_runner(
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None,
) -> Callable[[Sequence[str]], tuple[int, str, str]]:
    return run_systemctl or default_systemctl_runner


def reconcile_units(
    cfg: Mapping[str, object],
    *,
    enabled: bool,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope: InstallScope | None = None,
) -> ReconcileResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    if not platform_supported():
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            warnings=(),
        )

    # No tree configured → the whole feature is inert (but never broken).
    tree = str(cfg.get("tree") or "").strip()
    if not tree:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            warnings=(
                "no tree configured (set drift_watch.tree or HERMES_PROJECT); "
                "drift watch is inert",
            ),
            inert=True,
        )

    selected = scope or detect_install_scope()
    if selected is None:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            warnings=("systemd user manager unavailable for this install",),
        )

    if not enabled:
        disable_warnings = disable_timer(selected, run_systemctl=runner)
        warnings = list(disable_warnings)
        enabled_probe = probe_timer_is_enabled(selected, run_systemctl=runner)
        active_probe = probe_timer_is_active(selected, run_systemctl=runner)
        enabled_state, active_state, enabled_known, active_known = _reconcile_probe_fields(
            enabled_probe,
            active_probe,
            warnings,
        )
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=enabled_state,
            timer_active=active_state,
            warnings=tuple(warnings),
            enabled_known=enabled_known,
            timer_active_known=active_known,
        )

    hermes_home = str(get_hermes_home().resolve())
    service_body = render_service_unit(
        hermes_home=hermes_home,
        tree=tree,
        exec_start=unit_exec_start_argv(),
        scope=selected,
    )
    timer_body = render_timer_unit(
        schedule=str(cfg.get("schedule") or DEFAULT_SCHEDULE_CALENDAR),
    )

    changed = write_units_if_changed(
        selected,
        service_body=service_body,
        timer_body=timer_body,
    )
    warnings: list[str] = []
    if changed:
        code, _, err = runner(
            build_systemctl_cmd(selected, "daemon-reload")
        )
        if code != 0:
            warnings.append(
                f"failed to daemon-reload: {err.strip() or code}"
            )

    # No timer pre-stamping: Persistent=true catching up a missed drift watch
    # is desirable — a boot-time pass just inventories and maybe captures.
    code, _, err = runner(
        build_systemctl_cmd(selected, "enable", "--now", TIMER_NAME)
    )
    if code != 0:
        warnings.append(f"failed to enable timer: {err.strip() or code}")

    enabled_probe = probe_timer_is_enabled(selected, run_systemctl=runner)
    active_probe = probe_timer_is_active(selected, run_systemctl=runner)
    enabled_state, timer_active, enabled_known, active_known = _reconcile_probe_fields(
        enabled_probe,
        active_probe,
        warnings,
    )
    if enabled_known and enabled_state and active_known and not timer_active:
        warnings.append("timer enabled but not active")

    linger = linger_warning(selected)
    if linger:
        warnings.append(linger)

    return ReconcileResult(
        supported=True,
        scope=selected,
        changed=changed,
        enabled=enabled_state,
        timer_active=timer_active,
        warnings=tuple(warnings),
        enabled_known=enabled_known,
        timer_active_known=active_known,
    )


def _format_yes_no(value: bool, *, known: bool) -> str:
    if not known:
        return "unknown (probe failed)"
    return "yes" if value else "no"


def format_status(result: ReconcileResult) -> str:
    home = display_hermes_home()
    if not result.supported:
        if result.inert:
            return (
                "Hermes drift-watch is inert: no tree configured "
                "(set drift_watch.tree or HERMES_PROJECT)."
            )
        return (
            "Hermes drift-watch scheduler is unavailable on this platform "
            "(requires Linux with a functioning systemd installation)."
        )
    lines = [
        f"Hermes drift-watch ({home})",
        f"  Timer unit: {TIMER_NAME}",
        f"  Service unit: {SERVICE_NAME}",
        f"  Scope: {'system' if result.scope and result.scope.system else 'user'}",
        f"  Enabled: {_format_yes_no(result.enabled, known=result.enabled_known)}",
        f"  Timer active: {_format_yes_no(result.timer_active, known=result.timer_active_known)}",
    ]
    for warning in result.warnings:
        lines.append(f"  Warning: {warning}")
    return "\n".join(lines)


# ── gateway-start reconcile hook ────────────────────────────────────────────


def is_oneshot_run_invocation(argv: Sequence[str] | None = None) -> bool:
    """True when argv targets ``hermes drift_watch run`` (systemd oneshot entry)."""
    tokens = [str(tok) for tok in (argv or sys.argv)]
    for idx, tok in enumerate(tokens):
        if tok != "drift_watch":
            continue
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if nxt == "run":
            return True
    return False


def reconcile_scheduler_on_load(
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope=None,
) -> ReconcileResult | None:
    """Install/reconcile hook — never call from ``register()`` or oneshot ``run``.

    Explicit disablement (``drift_watch.enabled: false`` or ``plugins.disabled``)
    stops any installed timer. Enabled installs reconcile idempotently.
    """
    if is_oneshot_run_invocation():
        return None
    if not platform_supported():
        return None

    cfg = load_drift_watch_config()
    enabled = not plugin_explicitly_disabled() and bool(cfg.get("enabled", True))
    kwargs = {}
    if run_systemctl is not None:
        kwargs["run_systemctl"] = run_systemctl
    if scope is not None:
        kwargs["scope"] = scope
    try:
        return reconcile_units(cfg, enabled=enabled, **kwargs)
    except Exception as exc:
        logger.warning(
            "drift_watch scheduler reconcile skipped: %s", exc, exc_info=True
        )
        return None
