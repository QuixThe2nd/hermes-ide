"""Systemd unit rendering and idempotent reconciliation for drift_watch.

Scope detection comes from ``plugins.auto_update.platform`` (imported, not
duplicated) so both schedulers always agree on system-vs-user installs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
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
from plugins.drift_watch.config import (
    DEFAULT_SCHEDULE_CALENDAR,
    load_drift_watch_config,
    plugin_explicitly_disabled,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-drift-watch.service"
TIMER_NAME = "hermes-drift-watch.timer"


class ProbeOutcome(Enum):
    TRUE = "true"
    FALSE = "false"
    QUERY_FAILED = "query_failed"


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.outcome != ProbeOutcome.QUERY_FAILED

    @property
    def as_bool(self) -> bool:
        return self.outcome == ProbeOutcome.TRUE


_PROBE_DETAIL_LIMIT = 200
_SYSTEM_ERROR_MARKERS = (
    "failed to connect to bus",
    "connection timed out",
    "operation timed out",
    "timed out after",
    "timeoutexpired",
    "access denied",
    "permission denied",
    "transport endpoint",
    "can't connect",
)


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


def _systemd_quote(value: str) -> str:
    if not value:
        return '""'
    special = set(' \t\n"\\$%')
    if not any(ch in special for ch in value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


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


def atomic_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


def write_units_if_changed(
    scope: InstallScope,
    *,
    service_body: str,
    timer_body: str,
) -> bool:
    changed = False
    for path, body in (
        (service_unit_path(scope), service_body),
        (timer_unit_path(scope), timer_body),
    ):
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing == body:
            continue
        atomic_write(path, body)
        changed = True
    return changed


def default_systemctl_runner(args: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _resolve_systemctl_runner(
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None,
) -> Callable[[Sequence[str]], tuple[int, str, str]]:
    return run_systemctl or default_systemctl_runner


def _normalize_probe_detail(text: str) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) > _PROBE_DETAIL_LIMIT:
        return collapsed[: _PROBE_DETAIL_LIMIT - 3] + "..."
    return collapsed


def _stderr_indicates_query_failure(stderr: str, stdout: str = "") -> bool:
    blob = f"{stderr} {stdout}".lower()
    return any(marker in blob for marker in _SYSTEM_ERROR_MARKERS)


def _classify_is_enabled_probe(
    code: int, stdout: str, stderr: str
) -> ProbeResult:
    detail = _normalize_probe_detail(stderr)
    if _stderr_indicates_query_failure(stderr, stdout):
        return ProbeResult(
            ProbeOutcome.QUERY_FAILED,
            detail or f"exit code {code}",
        )

    text = stdout.strip().lower()
    if code == 0 and text in {"enabled", "static"}:
        return ProbeResult(ProbeOutcome.TRUE)
    if code == 4:
        return ProbeResult(ProbeOutcome.FALSE)
    if code == 1:
        if text in {
            "disabled",
            "masked",
            "indirect",
            "generated",
            "transient",
            "not-found",
        }:
            return ProbeResult(ProbeOutcome.FALSE)
        if not detail:
            return ProbeResult(ProbeOutcome.FALSE)
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if detail:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if code != 0:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, f"exit code {code}")
    return ProbeResult(ProbeOutcome.FALSE)


def _classify_is_active_probe(code: int, stdout: str, stderr: str) -> ProbeResult:
    detail = _normalize_probe_detail(stderr)
    if _stderr_indicates_query_failure(stderr, stdout):
        return ProbeResult(
            ProbeOutcome.QUERY_FAILED,
            detail or f"exit code {code}",
        )

    text = stdout.strip().lower()
    if code == 0 and text == "active":
        return ProbeResult(ProbeOutcome.TRUE)
    if code in {3, 4}:
        return ProbeResult(ProbeOutcome.FALSE)
    if code == 1:
        if text in {"inactive", "dead", "failed", "unknown", "not-found"}:
            return ProbeResult(ProbeOutcome.FALSE)
        if not detail:
            return ProbeResult(ProbeOutcome.FALSE)
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if detail:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if code != 0:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, f"exit code {code}")
    return ProbeResult(ProbeOutcome.FALSE)


def probe_timer_is_active(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> ProbeResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    code, out, err = runner(build_systemctl_cmd(scope, "is-active", TIMER_NAME))
    return _classify_is_active_probe(code, out, err)


def probe_timer_is_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> ProbeResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    code, out, err = runner(build_systemctl_cmd(scope, "is-enabled", TIMER_NAME))
    return _classify_is_enabled_probe(code, out, err)


def _probe_warning(label: str, probe: ProbeResult) -> str:
    return f"failed to query timer {label} state: {probe.detail or 'unknown error'}"


def _reconcile_probe_fields(
    enabled_probe: ProbeResult,
    active_probe: ProbeResult,
    warnings: list[str],
) -> tuple[bool, bool, bool, bool]:
    if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(_probe_warning("enabled", enabled_probe))
    if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(_probe_warning("active", active_probe))
    return (
        enabled_probe.as_bool,
        active_probe.as_bool,
        enabled_probe.known,
        active_probe.known,
    )


def expected_timer_disable_argv(scope: InstallScope) -> list[list[str]]:
    """Exact stop+disable argv pair for the timer unit (never the oneshot service)."""
    return [
        build_systemctl_cmd(scope, "stop", TIMER_NAME),
        build_systemctl_cmd(scope, "disable", TIMER_NAME),
    ]


def disable_timer(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> tuple[str, ...]:
    """Stop/disable the timer only — never stop the oneshot service."""
    runner = _resolve_systemctl_runner(run_systemctl)
    warnings: list[str] = []
    for argv in expected_timer_disable_argv(scope):
        code, _, err = runner(argv)
        if code != 0:
            action = argv[-2] if len(argv) >= 2 else "control"
            detail = err.strip() or str(code)
            warnings.append(f"failed to {action} timer: {detail}")
    return tuple(warnings)


def user_linger_enabled(*, username: str | None = None) -> bool:
    """Read-only linger probe via /var/lib/systemd/linger/<user> presence."""
    if username is None:
        try:
            import pwd

            username = pwd.getpwuid(os.getuid()).pw_name  # windows-footgun: ok — Linux-only linger probe
        except (ImportError, KeyError, OSError):
            return False
    return (Path("/var/lib/systemd/linger") / username).is_file()


def linger_warning(scope: InstallScope | None) -> str | None:
    if scope is None or scope.system:
        return None
    if user_linger_enabled():
        return None
    return (
        "User-scoped timer selected but loginctl linger is off; the timer may not "
        "run after logout. Enable with: loginctl enable-linger $USER"
    )


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
