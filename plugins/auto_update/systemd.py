"""Systemd unit rendering and idempotent reconciliation."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hermes_constants import display_hermes_home, get_hermes_home

from plugins.auto_update.config import (
    DEFAULT_ACCURACY_SEC,
    DEFAULT_RANDOMIZED_DELAY_SEC,
    default_schedule_calendar,
)
from plugins.auto_update.legacy import duplicate_scheduler_present, migrate_legacy_units
from plugins.auto_update.platform import (
    InstallScope,
    build_systemctl_cmd,
    detect_install_scope,
    platform_supported,
    profile_cli_args,
    unit_exec_start_argv,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-auto-updater.service"
TIMER_NAME = "hermes-auto-updater.timer"


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
    legacy: tuple[str, ...]
    warnings: tuple[str, ...]
    enabled_known: bool = True
    timer_active_known: bool = True


def service_unit_path(scope: InstallScope, service_name: str = SERVICE_NAME) -> Path:
    return scope.unit_dir / service_name


def timer_unit_path(scope: InstallScope, timer_name: str = TIMER_NAME) -> Path:
    return scope.unit_dir / timer_name


def _systemd_quote(value: str) -> str:
    if not value:
        return '""'
    special = set(" \t\n\"'\\$%")
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


def _logical_unit_lines(body: str):
    pending = ""
    for line in body.splitlines():
        if pending and line.lstrip().startswith(("#", ";")):
            continue
        stripped = line.rstrip()
        continued = stripped.endswith("\\")
        pending += stripped[:-1] if continued else line
        if continued:
            pending += " "
            continue
        yield pending
        pending = ""
    if pending:
        raise ValueError("unterminated unit line continuation")


_SYSTEMD_WHITESPACE = " \t\n\r"
_SYSTEMD_SIMPLE_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
    "'": b"'",
    "s": b" ",
    # format_environment() has always emitted \$; Environment= treats $ literally.
    "$": b"$",
}


def _decode_systemd_escape(value: str, index: int) -> tuple[bytes, int]:
    if index >= len(value):
        raise ValueError("trailing backslash in Environment= value")
    marker = value[index]
    simple = _SYSTEMD_SIMPLE_ESCAPES.get(marker)
    if simple is not None:
        return simple, index + 1

    widths = {"x": 2, "u": 4, "U": 8}
    if marker in widths:
        width = widths[marker]
        digits = value[index + 1 : index + 1 + width]
        if len(digits) != width or any(
            digit not in "0123456789abcdefABCDEF" for digit in digits
        ):
            raise ValueError(f"invalid \\{marker} escape in Environment= value")
        number = int(digits, 16)
        end = index + 1 + width
        if number == 0:
            raise ValueError("NUL is not allowed in Environment= values")
        if marker == "x":
            return bytes((number,)), end
        try:
            return chr(number).encode("utf-8"), end
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("invalid Unicode escape in Environment= value") from exc

    if marker in "01234567":
        digits = value[index : index + 3]
        if len(digits) != 3 or any(digit not in "01234567" for digit in digits):
            raise ValueError("invalid octal escape in Environment= value")
        number = int(digits, 8)
        if number == 0 or number > 255:
            raise ValueError("invalid byte in Environment= octal escape")
        return bytes((number,)), index + 3

    raise ValueError(f"unknown \\{marker} escape in Environment= value")


def _split_systemd_environment(environment: str) -> list[str]:
    assignments: list[str] = []
    index = 0
    while index < len(environment):
        while index < len(environment) and environment[index] in _SYSTEMD_WHITESPACE:
            index += 1
        if index >= len(environment):
            break

        decoded = bytearray()
        quote: str | None = None
        while index < len(environment):
            char = environment[index]
            if char == "\\":
                escaped, index = _decode_systemd_escape(environment, index + 1)
                decoded.extend(escaped)
                continue
            if quote is not None:
                if char == quote:
                    quote = None
                else:
                    decoded.extend(char.encode("utf-8"))
                index += 1
                continue
            if char in "'\"":
                quote = char
                index += 1
                continue
            if char in _SYSTEMD_WHITESPACE:
                break
            decoded.extend(char.encode("utf-8"))
            index += 1
        if quote is not None:
            raise ValueError("unterminated quote in Environment= value")
        try:
            assignments.append(decoded.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("Environment= value is not valid UTF-8") from exc
    return assignments


def _literal_systemd_percent(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "%":
            decoded.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] != "%":
            raise ValueError("HERMES_HOME contains an unresolved systemd specifier")
        decoded.append("%")
        index += 2
    return "".join(decoded)


def _parse_unit_environment_home(body: str) -> str:
    section = ""
    home: str | None = None
    for line in _logical_unit_lines(body):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != "Service":
            continue
        key, separator, environment = stripped.partition("=")
        if not separator or key.strip() != "Environment":
            continue
        try:
            assignments = _split_systemd_environment(environment)
        except ValueError as exc:
            raise ValueError(f"invalid Environment= syntax: {exc}") from exc
        if not assignments:
            home = None
            continue
        for assignment in assignments:
            name, separator, value = assignment.partition("=")
            if not separator:
                raise ValueError("invalid Environment= assignment without '='")
            if name == "HERMES_HOME":
                home = value
    if home is None:
        raise ValueError("installed service has no HERMES_HOME assignment")
    home = _literal_systemd_percent(home)
    if any(ord(char) < 32 or ord(char) == 127 for char in home):
        raise ValueError("installed service has a non-printable HERMES_HOME")
    if not home:
        raise ValueError("installed service has an empty HERMES_HOME")
    if not Path(home).is_absolute():
        raise ValueError(f"installed HERMES_HOME is not absolute: {home!r}")
    return home


def _ambiguous_ownership_warning(service_path: Path, detail: object) -> str:
    return (
        f"cannot determine HERMES_HOME ownership of {service_path}: {detail}; "
        "refusing to modify the existing auto-update units. Set "
        "auto_update.systemd_takeover: true to take over the unit explicitly."
    )


def render_service_unit(
    *,
    hermes_home: str,
    exec_start: Sequence[str],
    scope: InstallScope | None = None,
) -> str:
    exec_line = format_exec_start(exec_start)
    profile = profile_cli_args()
    profile_env = ""
    if profile:
        profile_env = format_environment("HERMES_PROFILE", profile[-1]) + "\n"
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
Description=Hermes unattended update (oneshot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{identity}ExecStart={exec_line}
WorkingDirectory={working_dir}
{format_environment("HERMES_HOME", hermes_home)}
{profile_env}StandardOutput=journal
StandardError=journal

[Install]
{wanted_by}
"""


def render_timer_unit(
    *,
    schedule: str,
    randomized_delay_sec: int,
    accuracy_sec: str,
) -> str:
    return f"""[Unit]
Description=Hermes unattended update schedule

[Timer]
OnCalendar={schedule}
RandomizedDelaySec={randomized_delay_sec}
AccuracySec={accuracy_sec}
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
    service_name: str = SERVICE_NAME,
    timer_name: str = TIMER_NAME,
) -> bool:
    changed = False
    for path, body in (
        (service_unit_path(scope, service_name), service_body),
        (timer_unit_path(scope, timer_name), timer_body),
    ):
        existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
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


def timer_stamp_path(scope: InstallScope, timer_name: str = TIMER_NAME) -> Path:
    if scope.system:
        base = Path("/var/lib/systemd/timers")
    else:
        base = Path.home() / ".local/share/systemd/timers"
    return base / f"stamp-{timer_name}"


def prestamp_timer(
    scope: InstallScope,
    *,
    timer_name: str = TIMER_NAME,
    now_usec: int | None = None,
    write_stamp: Callable[[Path, str], None] | None = None,
) -> None:
    """Pre-stamp a timer so ``Persistent=true`` does not catch up on first enable."""
    stamp = timer_stamp_path(scope, timer_name)
    usec = now_usec if now_usec is not None else int(time.time() * 1_000_000)
    writer = write_stamp or _write_timer_stamp
    writer(stamp, str(usec))


def _write_timer_stamp(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


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
    timer_name: str = TIMER_NAME,
) -> ProbeResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    code, out, err = runner(build_systemctl_cmd(scope, "is-active", timer_name))
    return _classify_is_active_probe(code, out, err)


def probe_timer_is_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    timer_name: str = TIMER_NAME,
) -> ProbeResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    code, out, err = runner(build_systemctl_cmd(scope, "is-enabled", timer_name))
    return _classify_is_enabled_probe(code, out, err)


def timer_is_active(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> bool:
    return probe_timer_is_active(scope, run_systemctl=run_systemctl).as_bool


def timer_is_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> bool:
    return probe_timer_is_enabled(scope, run_systemctl=run_systemctl).as_bool


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


def expected_timer_disable_argv(
    scope: InstallScope, *, timer_name: str = TIMER_NAME
) -> list[list[str]]:
    """Exact stop+disable argv pair for the timer unit (never the oneshot service)."""
    return [
        build_systemctl_cmd(scope, "stop", timer_name),
        build_systemctl_cmd(scope, "disable", timer_name),
    ]


def disable_timer(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    timer_name: str = TIMER_NAME,
) -> tuple[str, ...]:
    """Stop/disable the timer only — never stop the oneshot service."""
    runner = _resolve_systemctl_runner(run_systemctl)
    warnings: list[str] = []
    for argv in expected_timer_disable_argv(scope, timer_name=timer_name):
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

            username = pwd.getpwuid(os.getuid()).pw_name
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


def _ownership_refusal_warning(scope: InstallScope, current_home: Path) -> str | None:
    """Warning when the installed unit belongs to another install (or is
    ambiguous); ``None`` means this install may manage the unit."""
    service_path = service_unit_path(scope)
    installed_body: str | None = None
    ownership_warning: str | None = None
    try:
        installed_body = service_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if service_path.is_symlink():
            ownership_warning = _ambiguous_ownership_warning(service_path, exc)
    except (OSError, UnicodeError) as exc:
        ownership_warning = _ambiguous_ownership_warning(service_path, exc)

    if installed_body is not None:
        try:
            installed_home = Path(
                _parse_unit_environment_home(installed_body)
            ).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            ownership_warning = _ambiguous_ownership_warning(service_path, exc)
        else:
            if installed_home != current_home:
                ownership_warning = (
                    f"refusing takeover: installed {SERVICE_NAME} targets "
                    f"HERMES_HOME={installed_home}; this install is {current_home}. "
                    "Set auto_update.systemd_takeover: true to take over the "
                    "existing unit explicitly."
                )
    return ownership_warning


def _ownership_refused_result(scope: InstallScope, warning: str) -> ReconcileResult:
    return ReconcileResult(
        supported=True,
        scope=scope,
        changed=False,
        enabled=False,
        timer_active=False,
        legacy=(),
        warnings=(warning,),
        enabled_known=False,
        timer_active_known=False,
    )


def reconcile_units(
    cfg: Mapping[str, object],
    *,
    enabled: bool,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
    scope: InstallScope | None = None,
    write_stamp: Callable[[Path, str], None] | None = None,
) -> ReconcileResult:
    runner = _resolve_systemctl_runner(run_systemctl)
    if not platform_supported():
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(),
        )

    selected = scope or detect_install_scope()
    if selected is None:
        return ReconcileResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=("systemd user manager unavailable for this install",),
        )

    if not enabled:
        if cfg.get("systemd_takeover") is not True:
            refusal = _ownership_refusal_warning(
                selected, get_hermes_home().resolve()
            )
            if refusal is not None:
                return _ownership_refused_result(selected, refusal)
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
            legacy=(),
            warnings=tuple(warnings),
            enabled_known=enabled_known,
            timer_active_known=active_known,
        )

    current_home = get_hermes_home().resolve()
    hermes_home = str(current_home)
    if cfg.get("systemd_takeover") is not True:
        refusal = _ownership_refusal_warning(selected, current_home)
        if refusal is not None:
            return _ownership_refused_result(selected, refusal)

    service_body = render_service_unit(
        hermes_home=hermes_home,
        exec_start=unit_exec_start_argv(),
        scope=selected,
    )
    timer_body = render_timer_unit(
        schedule=str(cfg.get("schedule") or default_schedule_calendar()),
        randomized_delay_sec=int(
            cfg.get("randomized_delay_sec", DEFAULT_RANDOMIZED_DELAY_SEC)
        ),
        accuracy_sec=str(cfg.get("accuracy_sec", DEFAULT_ACCURACY_SEC)),
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

    legacy = migrate_legacy_units(selected, run_systemctl=runner)
    warnings.extend(legacy.warnings)
    if duplicate_scheduler_present(selected, run_systemctl=runner):
        warnings.append(
            "legacy hermes-auto-update.timer still present; refusing duplicate schedulers"
        )
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=changed,
            enabled=False,
            timer_active=False,
            legacy=legacy.disabled_units,
            warnings=tuple(warnings),
        )

    # Enable/start TIMER ONLY — never start the oneshot service immediately.
    enabled_probe = probe_timer_is_enabled(selected, run_systemctl=runner)
    if enabled_probe.outcome == ProbeOutcome.QUERY_FAILED:
        warnings.append(_probe_warning("enabled", enabled_probe))
    elif enabled_probe.outcome == ProbeOutcome.FALSE:
        prestamp_timer(selected, write_stamp=write_stamp)

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
        legacy=legacy.disabled_units,
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
        return (
            "Hermes auto-update scheduler is unavailable on this platform "
            "(requires Linux with a functioning systemd installation)."
        )
    lines = [
        f"Hermes auto-update ({home})",
        f"  Timer unit: {TIMER_NAME}",
        f"  Service unit: {SERVICE_NAME}",
        f"  Scope: {'system' if result.scope and result.scope.system else 'user'}",
        f"  Enabled: {_format_yes_no(result.enabled, known=result.enabled_known)}",
        f"  Timer active: {_format_yes_no(result.timer_active, known=result.timer_active_known)}",
    ]
    if result.legacy:
        lines.append(f"  Legacy units disabled: {', '.join(result.legacy)}")
    for warning in result.warnings:
        lines.append(f"  Warning: {warning}")
    return "\n".join(lines)
