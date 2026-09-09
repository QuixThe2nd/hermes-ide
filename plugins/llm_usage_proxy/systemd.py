"""systemd unit rendering and idempotent reconciliation for the usage proxy.

Scope detection comes from ``plugins.auto_update.platform`` (imported, not
duplicated) so this service, the viewer, and the schedulers always agree on
system- vs user-scoped installs.

The unit name carries a short hash of this profile's HERMES_HOME: two named
profiles share a systemd user scope (and would otherwise clobber each
other's unit file), so each profile owns a distinct unit and never stops or
adopts another profile's listener.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from hermes_constants import display_hermes_home, get_hermes_home

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
from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
from plugins.llm_usage_proxy.probe import FREE, PortState, probe_port_state
from plugins.llm_usage_proxy.routes import build_route_table
from plugins.llm_usage_proxy.server import BIND_HOST, DEFAULT_PORT, compute_identity

logger = logging.getLogger(__name__)

SERVICE_BASE_NAME = "hermes-llm-usage-proxy"
SERVICE_SUFFIX = ".service"

# Sentinels written into the unit so a human (or a future reconcile) can see
# the stand-down and capture rules this plugin runs by.
_COEXIST_NOTE = (
    "# Coexistence: if something already serves the configured port — e.g. a\n"
    "# copy from another profile or a foreign unit — reconcile deliberately\n"
    "# does NOT adopt or stop it; routing stays off for this profile instead\n"
    "# of trusting a listener whose identity does not verify. Stand-down is a\n"
    "# warning, never an error, so it cannot fail gateway start.\n"
)
_CAPTURE_NOTE = (
    "# Upstream routes are resolved from this profile's own provider\n"
    "# configuration (explicit llm_usage_proxy.upstreams, well-known provider\n"
    "# defaults, the provider's declared base-URL env override, and credential\n"
    "# pool entries) — never from a process-global env sweep — then baked into\n"
    "# this unit. Changing provider endpoints needs one\n"
    "# `hermes llm_usage_proxy reconcile` to take effect.\n"
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
    routes: tuple[tuple[str, str], ...] = ()
    identity: str = ""


def service_name(hermes_home: Optional[Path] = None) -> str:
    """Unit name unique to this profile's HERMES_HOME."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    digest = hashlib.sha256(str(home.resolve() if home.exists() else home).encode())
    return f"{SERVICE_BASE_NAME}-{digest.hexdigest()[:8]}{SERVICE_SUFFIX}"


def profile_identity(hermes_home: Optional[Path] = None) -> str:
    """Opaque identity token this profile's proxy must echo in /health."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return compute_identity(str(home.resolve() if home.exists() else home))


def service_unit_path(scope: InstallScope, hermes_home: Optional[Path] = None) -> Path:
    return scope.unit_dir / service_name(hermes_home)


def data_dir_path(hermes_home: Optional[Path] = None) -> Path:
    return (hermes_home or get_hermes_home()) / "usage-proxy"


def db_path(hermes_home: Optional[Path] = None) -> Path:
    return data_dir_path(hermes_home) / "usage.sqlite"


def server_script_path() -> Path:
    """Bundled stdlib-only proxy shipped inside this plugin."""
    return Path(__file__).resolve().parent / "server.py"


def build_exec_start_argv(
    cfg: Mapping[str, object],
    *,
    hermes_home: Optional[Path] = None,
    python: Optional[str] = None,
    script: Optional[Path] = None,
    environ=None,
) -> list[str]:
    """argv for the unit: repo python running the bundled server.py."""
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    interpreter = python or resolve_python_executable()
    port = int(cfg.get("port") or DEFAULT_PORT)
    argv = [
        interpreter,
        str(script or server_script_path()),
        "--port",
        str(port),
        "--db",
        str(db_path(home)),
        "--identity",
        profile_identity(home),
    ]
    targets = build_route_table(cfg, environ=environ)
    for name in sorted(targets):
        argv.extend(("--upstream", f"{name}={targets[name]}"))
    return argv


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
Description=Hermes LLM usage proxy (loopback token-usage recorder)
After=network-online.target
Wants=network-online.target
{_COEXIST_NOTE}{_CAPTURE_NOTE}
[Service]
Type=simple
# The proxy is on the request path for every routed model call: restart it
# unconditionally and quickly when it dies, with a small delay so a config
# error cannot become a hot crash loop.
Restart=always
RestartSec=2s
# usage.sqlite and its WAL/SHM siblings hold request metadata only, but keep
# them 0600 from creation anyway (the server also sets umask 0077 itself).
UMask=0077
{identity}ExecStart={exec_line}
WorkingDirectory={working_dir}
{format_environment("HERMES_HOME", hermes_home)}
StandardOutput=journal
StandardError=journal

[Install]
{wanted_by}
"""


def write_unit_if_changed(
    scope: InstallScope, *, body: str, hermes_home: Optional[Path] = None
) -> bool:
    path = service_unit_path(scope, hermes_home)
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
    hermes_home: Optional[Path] = None,
) -> ProbeResult:
    runner = run_systemctl or default_systemctl_runner
    code, out, err = runner(
        build_systemctl_cmd(scope, "is-enabled", service_name(hermes_home))
    )
    return _classify_probe(
        code, out, err, active_words={"enabled", "enabled-runtime", "static"}
    )


def probe_service_is_active(
    scope: InstallScope,
    *,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
    hermes_home: Optional[Path] = None,
) -> ProbeResult:
    runner = run_systemctl or default_systemctl_runner
    code, out, err = runner(
        build_systemctl_cmd(scope, "is-active", service_name(hermes_home))
    )
    return _classify_probe(code, out, err, active_words={"active"})


def disable_service(
    scope: InstallScope,
    *,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
    hermes_home: Optional[Path] = None,
) -> tuple[str, ...]:
    """Stop/disable this profile's unit only — never another profile's."""
    runner = run_systemctl or default_systemctl_runner
    warnings: list[str] = []
    for verb in ("stop", "disable"):
        code, _, err = runner(
            build_systemctl_cmd(scope, verb, service_name(hermes_home))
        )
        if code != 0:
            warnings.append(
                f"failed to {verb} {service_name(hermes_home)}: {err.strip() or code}"
            )
    return tuple(warnings)


def reconcile_service(
    cfg: Mapping[str, object],
    *,
    enabled: bool,
    run_systemctl: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None,
    scope: Optional[InstallScope] = None,
    environ=None,
) -> ReconcileResult:
    """Install/start this profile's proxy, standing down when the port is taken.

    Idempotent: rewrites the unit only when its content changed, restarts a
    running unit whose argv changed (``enable --now`` alone would leave the
    old routes serving), and never touches a listener it cannot verify as
    this profile's own proxy. Any failure is reported as a warning on the
    result — reconcile never raises, so a broken systemd cannot take gateway
    startup down with it.
    """
    runner = run_systemctl or default_systemctl_runner
    warnings: list[str] = []
    home = get_hermes_home()
    identity = profile_identity(home)
    routes = build_route_table(cfg, environ=environ)

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
            routes=tuple(sorted(routes.items())),
            identity=identity,
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
            routes=tuple(sorted(routes.items())),
            identity=identity,
        )

    # Probed only once we know systemd is real: an unsupported platform does
    # no socket work at all. Identity+routes are part of the probe so a
    # foreign or stale listener is never mistaken for ours. The probe binds
    # loopback by default — this service never listens anywhere else.
    port_state = probe_port_state(
        int(cfg.get("port") or DEFAULT_PORT),
        expect_identity=identity,
        expect_routes=routes,
    )

    if not enabled:
        warnings.extend(disable_service(selected, run_systemctl=runner, hermes_home=home))
        enabled_probe = probe_service_is_enabled(
            selected, run_systemctl=runner, hermes_home=home
        )
        active_probe = probe_service_is_active(
            selected, run_systemctl=runner, hermes_home=home
        )
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
            unit_installed=service_unit_path(selected, home).is_file(),
            port=port_state,
            warnings=tuple(warnings),
            enabled_known=enabled_probe.known,
            service_active_known=active_probe.known,
            routes=tuple(sorted(routes.items())),
            identity=identity,
        )

    # Port already serving: never race a second copy and never adopt a
    # listener this profile cannot verify (foreign service, another
    # profile's proxy, or a stale route table — all stand down the same way,
    # with routing left off and the reason reported).
    if port_state.occupied:
        if port_state.healthy:
            warnings.append(
                "this profile's usage proxy already running on the configured"
                f" port ({port_state.detail})"
            )
        else:
            warnings.append(
                "port already bound by a listener that does not verify as this"
                f" profile's usage proxy; leaving it alone ({port_state.detail})"
            )
        enabled_probe = probe_service_is_enabled(
            selected, run_systemctl=runner, hermes_home=home
        )
        active_probe = probe_service_is_active(
            selected, run_systemctl=runner, hermes_home=home
        )
        if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
            warnings.append(f"failed to query enabled state: {enabled_probe.detail}")
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=enabled_probe.as_bool,
            service_active=active_probe.as_bool,
            unit_installed=service_unit_path(selected, home).is_file(),
            port=port_state,
            warnings=tuple(warnings),
            enabled_known=enabled_probe.known,
            service_active_known=active_probe.known,
            routes=tuple(sorted(routes.items())),
            identity=identity,
        )

    home_str = str(home.resolve())
    body = render_service_unit(
        hermes_home=home_str,
        exec_start=build_exec_start_argv(cfg, hermes_home=home, environ=environ),
        scope=selected,
    )
    try:
        changed = write_unit_if_changed(selected, body=body, hermes_home=home)
    except OSError as exc:
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=False,
            service_active=False,
            unit_installed=False,
            port=port_state,
            warnings=(f"failed to write {service_name(home)}: {exc}",),
            routes=tuple(sorted(routes.items())),
            identity=identity,
        )

    if changed:
        code, _, err = runner(build_systemctl_cmd(selected, "daemon-reload"))
        if code != 0:
            warnings.append(f"failed to daemon-reload: {err.strip() or code}")

    code, _, err = runner(
        build_systemctl_cmd(selected, "enable", "--now", service_name(home))
    )
    if code != 0:
        warnings.append(
            f"failed to enable {service_name(home)}: {err.strip() or code}"
        )

    # A running unit keeps its old argv until restarted; without this a
    # changed route table would serve stale routes (and then fail identity
    # verification, leaving routing off) instead of picking the new one up.
    if changed:
        code, _, err = runner(
            build_systemctl_cmd(selected, "restart", service_name(home))
        )
        if code != 0:
            warnings.append(
                f"failed to restart {service_name(home)} after unit change:"
                f" {err.strip() or code}"
            )

    enabled_probe = probe_service_is_enabled(
        selected, run_systemctl=runner, hermes_home=home
    )
    active_probe = probe_service_is_active(
        selected, run_systemctl=runner, hermes_home=home
    )
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
        warnings.append("usage proxy unit enabled but not active")

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
        routes=tuple(sorted(routes.items())),
        identity=identity,
    )


def _format_yes_no(value: bool, *, known: bool) -> str:
    if not known:
        return "unknown (probe failed)"
    return "yes" if value else "no"


def format_status(
    result: ReconcileResult,
    *,
    cfg: Optional[Mapping[str, object]] = None,
    routing: Optional[Mapping[str, object]] = None,
) -> str:
    home = display_hermes_home()
    cfg = cfg if cfg is not None else load_llm_usage_proxy_config()
    port = int(cfg.get("port") or DEFAULT_PORT)
    lines = [
        f"Hermes LLM usage proxy ({home})",
        f"  Unit: {service_name()}",
        f"  Scope: {'system' if result.scope and result.scope.system else 'user'}",
        f"  Bind: {BIND_HOST}:{port}",
        f"  SQLite: {db_path()}",
        f"  Unit installed: {'yes' if result.unit_installed else 'no'}",
        f"  Enabled: {_format_yes_no(result.enabled, known=result.enabled_known)}",
        f"  Service active: {_format_yes_no(result.service_active, known=result.service_active_known)}",
        f"  Port: {result.port.status} ({result.port.detail})",
        f"  Routes: {len(result.routes)}"
        + (
            f" ({', '.join(name for name, _ in result.routes)})"
            if result.routes
            else ""
        ),
    ]
    if routing is not None:
        active = bool(routing.get("active"))
        reason = str(routing.get("reason") or "")
        lines.append(
            "  In-process routing: "
            + ("active" if active else f"off ({reason or 'not enabled'})")
        )
    if not result.supported:
        lines.append(
            "  Unavailable: requires Linux with a functioning systemd installation."
        )
    for warning in result.warnings:
        lines.append(f"  Warning: {warning}")
    return "\n".join(lines)


__all__ = [
    "ProbeOutcome",
    "ProbeResult",
    "ReconcileResult",
    "SERVICE_BASE_NAME",
    "build_exec_start_argv",
    "data_dir_path",
    "db_path",
    "disable_service",
    "format_status",
    "profile_identity",
    "probe_port_state",
    "probe_service_is_active",
    "probe_service_is_enabled",
    "reconcile_service",
    "render_service_unit",
    "server_script_path",
    "service_name",
    "service_unit_path",
    "write_unit_if_changed",
]
