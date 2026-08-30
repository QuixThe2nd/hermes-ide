"""systemd unit rendering and idempotent reconciliation for claude-viewer.

Scope detection comes from ``plugins.auto_update.platform`` (imported, not
duplicated) so this service and the schedulers always agree on system- vs
user-scoped installs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from hermes_constants import display_hermes_home, get_hermes_home
from tools.claude_viewer_url import public_base_url

from plugins.auto_update.platform import (
    InstallScope,
    build_systemctl_cmd,
    detect_install_scope,
    platform_supported,
    resolve_python_executable,
)
from plugins.auto_update.systemd import (
    atomic_write,
    default_systemctl_runner,
    format_exec_start,
    format_environment,
)
from plugins.claude_viewer.config import (
    load_claude_viewer_config,
    plugin_explicitly_disabled,
)
from plugins.claude_viewer.port import FREE, PortState, probe_port_state

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-claude-viewer.service"

# Sentinels written into the unit so a human (or a future reconcile) can see
# why this plugin stands down on a box that already runs a viewer.
_COEXIST_NOTE = (
    "# Coexistence: if something already serves the configured port — e.g. a\n"
    "# hand-started claude-viewer or a foreign unit such as\n"
    "# /etc/systemd/system/claude-viewer.service — reconcile deliberately does\n"
    "# NOT install this unit. Two copies would just crash-loop on\n"
    "# 'Address already in use', and the running one already serves the runs\n"
    "# directory. Stand-down is reported as a warning, never an error, so it\n"
    "# cannot fail gateway start."
)


class ProbeOutcome:
    """String sentinels for systemctl probe classification."""

    TRUE = "true"
    FALSE = "false"
    QUERY_FAILED = "query_failed"


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.outcome != ProbeOutcome.QUERY_FAILED

    @property
    def as_bool(self) -> bool:
        return self.outcome == ProbeOutcome.TRUE


@dataclass(frozen=True)
class ReconcileResult:
    supported: bool
    scope: Optional[InstallScope]
    changed: bool
    enabled: bool
    service_active: bool
    unit_installed: bool
    port: PortState
    warnings: tuple[str, ...] = field(default_factory=tuple)
    enabled_known: bool = True
    service_active_known: bool = True


def service_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / SERVICE_NAME


def log_dir_path(hermes_home: Optional[Path] = None) -> Path:
    return (hermes_home or get_hermes_home()) / "claude-runs"


def viewer_script_path() -> Path:
    """Bundled stdlib-only server shipped inside this plugin."""
    return Path(__file__).resolve().parents[1] / "claude_viewer" / "viewer" / "server.py"


def build_exec_start_argv(
    cfg: Mapping[str, object],
    *,
    hermes_home: Optional[Path] = None,
    python: Optional[str] = None,
    script: Optional[Path] = None,
) -> list[str]:
    """argv for the unit: repo python running the bundled server.py."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    interpreter = python or resolve_python_executable()
    return [
        interpreter,
        str(script or viewer_script_path()),
        "--bind",
        str(cfg.get("bind") or "0.0.0.0"),
        "--port",
        str(int(cfg.get("port") or 8787)),
        "--log-dir",
        str(log_dir_path(home)),
    ]


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


def render_service_unit(
    *,
    hermes_home: str,
    exec_start: Sequence[str],
    scope: Optional[InstallScope] = None,
) -> str:
    exec_line = format_exec_start(exec_start)
    working_dir = hermes_home.replace("%", "%%")
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
Description=Hermes Claude Code run viewer (delegate_claude_agent live log UI)
After=network-online.target
Wants=network-online.target
{_COEXIST_NOTE}

[Service]
Type=simple
# The viewer is a long-running UI, not a oneshot: restart it when it dies,
# but never respawn so fast that a port conflict becomes a hot loop.
Restart=on-failure
RestartSec=2s
{identity}ExecStart={exec_line}
WorkingDirectory={working_dir}
{format_environment("HERMES_HOME", hermes_home)}
StandardOutput=journal
StandardError=journal

[Install]
{wanted_by}
"""


def write_unit_if_changed(scope: InstallScope, *, body: str) -> bool:
    path = service_unit_path(scope)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing == body:
        return False
    atomic_write(path, body)
    return True


def _classify_probe(code: int, stdout: str, stderr: str, *, active_words: set[str]) -> ProbeResult:
    blob = f"{stderr} {stdout}".lower()
    query_markers = (
        "failed to connect to bus",
        "connection timed out",
        "operation timed out",
        "timed out after",
        "access denied",
        "permission denied",
        "transport endpoint",
        "can't connect",
    )
    detail = " ".join((stderr or "").split())[:200]
    if any(marker in blob for marker in query_markers):
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail or f"exit code {code}")
    text = stdout.strip().lower()
    if code == 0 and text in active_words:
        return ProbeResult(ProbeOutcome.TRUE)
    if code in {3, 4}:
        return ProbeResult(ProbeOutcome.FALSE)
    if code == 1:
        if text and text not in {"unknown", "not-found"}:
            return ProbeResult(ProbeOutcome.FALSE)
        if not detail:
            return ProbeResult(ProbeOutcome.FALSE)
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if detail:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, detail)
    if code != 0:
        return ProbeResult(ProbeOutcome.QUERY_FAILED, f"exit code {code}")
    return ProbeResult(ProbeOutcome.FALSE)


def probe_service_is_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
) -> ProbeResult:
    runner = run_systemctl or default_systemctl_runner
    code, out, err = runner(
        build_systemctl_cmd(scope, "is-enabled", SERVICE_NAME)
    )
    return _classify_probe(
        code, out, err, active_words={"enabled", "enabled-runtime", "static"}
    )


def probe_service_is_active(
    scope: InstallScope,
    *,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
) -> ProbeResult:
    runner = run_systemctl or default_systemctl_runner
    code, out, err = runner(build_systemctl_cmd(scope, "is-active", SERVICE_NAME))
    return _classify_probe(code, out, err, active_words={"active"})


def disable_service(
    scope: InstallScope,
    *,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
) -> tuple[str, ...]:
    """Stop/disable this plugin's unit only — never a foreign viewer unit."""
    runner = run_systemctl or default_systemctl_runner
    warnings: list[str] = []
    for verb in ("stop", "disable"):
        code, _, err = runner(build_systemctl_cmd(scope, verb, SERVICE_NAME))
        if code != 0:
            warnings.append(
                f"failed to {verb} {SERVICE_NAME}: {err.strip() or code}"
            )
    return tuple(warnings)


def reconcile_service(
    cfg: Mapping[str, object],
    *,
    enabled: bool,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
    scope: Optional[InstallScope] = None,
) -> ReconcileResult:
    """Install/start the bundled viewer, standing down when the port is taken.

    Idempotent: rewrites the unit only when its content changed and re-runs
    ``enable --now`` (a no-op when already active). Any failure is reported
    as a warning on the result — reconcile never raises, so a broken
    systemd cannot take gateway startup down with it.
    """
    runner = run_systemctl or default_systemctl_runner
    warnings: list[str] = []

    if not platform_supported():
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=PortState(FREE, "not probed (platform unsupported)"),
            warnings=(),
        )

    selected = scope or detect_install_scope()
    if selected is None:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=PortState(FREE, "not probed (no systemd scope)"),
            warnings=("systemd user manager unavailable for this install",),
        )

    # Probed only once we know systemd is real: an unsupported platform does
    # no socket work at all.
    port_state = probe_port_state(
        int(cfg.get("port") or 8787), bind=str(cfg.get("bind") or "0.0.0.0")
    )

    if not enabled:
        warnings.extend(disable_service(selected, run_systemctl=runner))
        enabled_probe = probe_service_is_enabled(selected, run_systemctl=runner)
        active_probe = probe_service_is_active(selected, run_systemctl=runner)
        if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
            warnings.append(f"failed to query enabled state: {enabled_probe.detail}")
        if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
            warnings.append(f"failed to query active state: {active_probe.detail}")
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=enabled_probe.as_bool,
            service_active=active_probe.as_bool,
            unit_installed=service_unit_path(selected).is_file(),
            port=port_state,
            warnings=tuple(warnings),
            enabled_known=enabled_probe.known,
            service_active_known=active_probe.known,
        )

    # Port already serving: never race a second copy. A healthy claude-viewer
    # means the URL is already live; a foreign listener means ours could not
    # bind anyway. Either way this is a stand-down, not a failure.
    if port_state.occupied:
        if port_state.healthy:
            warnings.append(
                "claude-viewer already running on this port; leaving it in charge"
                f" ({port_state.detail})"
            )
        else:
            warnings.append(
                "port already bound by another process; not starting a second"
                f" viewer ({port_state.detail})"
            )
        enabled_probe = probe_service_is_enabled(selected, run_systemctl=runner)
        active_probe = probe_service_is_active(selected, run_systemctl=runner)
        if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
            warnings.append(f"failed to query enabled state: {enabled_probe.detail}")
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=enabled_probe.as_bool,
            service_active=active_probe.as_bool,
            unit_installed=service_unit_path(selected).is_file(),
            port=port_state,
            warnings=tuple(warnings),
            enabled_known=enabled_probe.known,
            service_active_known=active_probe.known,
        )

    hermes_home = str(get_hermes_home().resolve())
    body = render_service_unit(
        hermes_home=hermes_home,
        exec_start=build_exec_start_argv(cfg, hermes_home=get_hermes_home()),
        scope=selected,
    )
    try:
        changed = write_unit_if_changed(selected, body=body)
    except OSError as exc:
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=port_state,
            warnings=(f"failed to write {SERVICE_NAME}: {exc}",),
        )

    if changed:
        code, _, err = runner(build_systemctl_cmd(selected, "daemon-reload"))
        if code != 0:
            warnings.append(f"failed to daemon-reload: {err.strip() or code}")

    code, _, err = runner(
        build_systemctl_cmd(selected, "enable", "--now", SERVICE_NAME)
    )
    if code != 0:
        warnings.append(
            f"failed to enable {SERVICE_NAME}: {err.strip() or code}"
        )

    enabled_probe = probe_service_is_enabled(selected, run_systemctl=runner)
    active_probe = probe_service_is_active(selected, run_systemctl=runner)
    if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(f"failed to query enabled state: {enabled_probe.detail}")
    if active_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(f"failed to query active state: {active_probe.detail}")
    if (
        enabled_probe.known
        and enabled_probe.as_bool
        and active_probe.known
        and not active_probe.as_bool
    ):
        warnings.append("viewer unit enabled but not active")

    return ReconcileResult(
        supported=True,
        scope=selected,
        changed=changed,
        enabled=enabled_probe.as_bool,
        service_active=active_probe.as_bool,
        unit_installed=True,
        port=port_state,
        warnings=tuple(warnings),
        enabled_known=enabled_probe.known,
        service_active_known=active_probe.known,
    )


def _format_yes_no(value: bool, *, known: bool) -> str:
    if not known:
        return "unknown (probe failed)"
    return "yes" if value else "no"


def format_status(result: ReconcileResult, *, cfg: Optional[Mapping[str, object]] = None) -> str:
    home = display_hermes_home()
    cfg = cfg if cfg is not None else load_claude_viewer_config()
    bind = str(cfg.get("bind") or "0.0.0.0")
    port = int(cfg.get("port") or 8787)
    lines = [
        f"Hermes Claude Code run viewer ({home})",
        f"  Unit: {SERVICE_NAME}",
        f"  Scope: {'system' if result.scope and result.scope.system else 'user'}",
        f"  Bind: {bind}:{port}",
        f"  Public URL: {public_base_url()}",
        f"  Log dir: {log_dir_path()}",
        f"  Unit installed: {'yes' if result.unit_installed else 'no'}",
        f"  Enabled: {_format_yes_no(result.enabled, known=result.enabled_known)}",
        f"  Service active: {_format_yes_no(result.service_active, known=result.service_active_known)}",
        f"  Port: {result.port.status} ({result.port.detail})",
    ]
    if not result.supported:
        lines.append(
            "  Unavailable: requires Linux with a functioning systemd installation."
        )
    for warning in result.warnings:
        lines.append(f"  Warning: {warning}")
    return "\n".join(lines)


# Re-exported for the CLI's import surface; os is used by unit rendering
# callers and kept here so this module stays the single import point.
__all__ = [
    "SERVICE_NAME",
    "ProbeOutcome",
    "ProbeResult",
    "ReconcileResult",
    "build_exec_start_argv",
    "disable_service",
    "format_status",
    "log_dir_path",
    "probe_port_state",
    "probe_service_is_active",
    "probe_service_is_enabled",
    "reconcile_service",
    "render_service_unit",
    "service_unit_path",
    "viewer_script_path",
    "write_unit_if_changed",
]
