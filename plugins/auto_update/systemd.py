"""Systemd unit rendering and idempotent reconciliation."""

from __future__ import annotations

import json
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
OWNER_FILENAME = "hermes-auto-updater.owner.json"
OWNER_FORMAT = 1


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


def scheduler_owner_path(scope: InstallScope) -> Path:
    return scope.unit_dir / OWNER_FILENAME


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


def _canonical_home(path: Path) -> Path:
    return path.expanduser().resolve()


def _systemd_words(value: str) -> list[str]:
    words: list[str] = []
    chars: list[str] = []
    quote: str | None = None
    escaped = False
    started = False
    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            started = True
        elif char == "\\":
            escaped = True
            started = True
        elif quote is not None:
            if char == quote:
                quote = None
            else:
                chars.append(char)
        elif char in {"'", '"'}:
            quote = char
            started = True
        elif char.isspace():
            if started:
                words.append("".join(chars))
                chars = []
                started = False
        else:
            chars.append(char)
            started = True
    if escaped or quote is not None:
        raise ValueError("unterminated systemd quoting")
    if started:
        words.append("".join(chars))
    return words


def _canonical_legacy_home(value: str) -> Path | None:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "%":
            decoded.append(value[index])
            index += 1
        elif index + 1 < len(value) and value[index + 1] == "%":
            decoded.append("%")
            index += 2
        else:
            return None
    try:
        path = Path("".join(decoded))
        if not path.is_absolute():
            return None
        return _canonical_home(path)
    except (OSError, RuntimeError, ValueError):
        return None


def _legacy_service_home(content: str) -> Path | None:
    environment_values: list[str] = []
    working_directories: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        try:
            if line.startswith("Environment="):
                for assignment in _systemd_words(line[len("Environment=") :]):
                    if assignment.startswith("HERMES_HOME="):
                        environment_values.append(assignment.split("=", 1)[1])
            elif line.startswith("WorkingDirectory="):
                words = _systemd_words(line[len("WorkingDirectory=") :])
                if len(words) != 1:
                    return None
                working_directories.append(words[0])
        except ValueError:
            return None
    if environment_values:
        return _canonical_legacy_home(environment_values[-1])
    if working_directories:
        return _canonical_legacy_home(working_directories[-1])
    return None


def _takeover_warning(reason: str) -> str:
    return (
        f"failed to reconcile scheduler ownership: {reason}; "
        "run `hermes auto_update reconcile --takeover` to explicitly replace it"
    )


def _read_owner_record(scope: InstallScope) -> tuple[Path | None, str | None]:
    path = scheduler_owner_path(scope)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as exc:
        return None, f"owner record is unreadable: {exc}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"format", "hermes_home"}:
            raise ValueError("wrong owner record shape")
        if type(data["format"]) is not int or data["format"] != OWNER_FORMAT:
            raise ValueError("unsupported owner record format")
        stored = data["hermes_home"]
        if not isinstance(stored, str) or not Path(stored).is_absolute():
            raise ValueError("owner home is not an absolute path")
        recorded = _canonical_home(Path(stored))
        if str(recorded) != stored:
            raise ValueError("owner home is not canonical")
        return recorded, None
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        return None, f"owner record is invalid: {exc}"


def _ownership_refusal(scope: InstallScope, current_home: Path) -> str | None:
    recorded, record_error = _read_owner_record(scope)
    if record_error:
        return _takeover_warning(record_error)
    if recorded is not None:
        if recorded == current_home:
            return None
        return _takeover_warning(
            f"existing units belong to {recorded}, not {current_home}"
        )

    service = service_unit_path(scope)
    timer = timer_unit_path(scope)
    try:
        if not service.exists() and not timer.exists():
            return None
        content = service.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _takeover_warning(f"existing units cannot be inspected: {exc}")
    legacy_home = _legacy_service_home(content)
    if legacy_home is None:
        return _takeover_warning("existing service has no verifiable canonical HERMES_HOME")
    if legacy_home != current_home:
        return _takeover_warning(
            f"existing units belong to {legacy_home}, not {current_home}"
        )
    return None


def _write_owner_record(scope: InstallScope, home: Path) -> None:
    path = scheduler_owner_path(scope)
    payload = json.dumps(
        {"format": OWNER_FORMAT, "hermes_home": str(home)}, sort_keys=True
    ) + "\n"
    try:
        if path.read_text(encoding="utf-8") == payload:
            return
    except (OSError, UnicodeError):
        pass
    atomic_write(path, payload)


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
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing == body:
            continue
        atomic_write(path, body)
        changed = True
    return changed


def _rollback_unit_writes(
    scope: InstallScope,
    previous_units: Sequence[tuple[Path, bytes | None]],
    runner: Callable[[Sequence[str]], tuple[int, str, str]],
    warnings: list[str],
) -> None:
    for path, previous in previous_units:
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(previous)
                os.replace(tmp, path)
        except OSError as exc:
            warnings.append(f"failed to restore {path.name} after failed reconcile: {exc}")

    code, _, err = runner(build_systemctl_cmd(scope, "daemon-reload"))
    if code != 0:
        warnings.append(
            f"failed to daemon-reload after unit rollback: {err.strip() or code}"
        )


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
    write_stamp: Callable[[Path, str], None] | None = None,
    takeover: bool = False,
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

    refusal: str | None = None
    try:
        current_home = _canonical_home(get_hermes_home())
    except (OSError, RuntimeError, ValueError) as exc:
        current_home = None
        refusal = _takeover_warning(f"HERMES_HOME cannot be canonicalized: {exc}")
    if current_home is not None and not takeover:
        refusal = _ownership_refusal(selected, current_home)
    if current_home is None or refusal:
        return ReconcileResult(
            supported=True,
            scope=selected,
            changed=False,
            enabled=False,
            timer_active=False,
            legacy=(),
            warnings=(refusal,),
            enabled_known=False,
            timer_active_known=False,
        )
    hermes_home = str(current_home)
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

    unit_paths = (service_unit_path(selected), timer_unit_path(selected))
    previous_units = tuple(
        (path, path.read_bytes() if path.is_file() else None) for path in unit_paths
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
            _rollback_unit_writes(selected, previous_units, runner, warnings)
            return ReconcileResult(
                supported=True,
                scope=selected,
                changed=changed,
                enabled=False,
                timer_active=False,
                legacy=(),
                warnings=tuple(warnings),
                enabled_known=False,
                timer_active_known=False,
            )

    legacy = migrate_legacy_units(selected, run_systemctl=runner)
    warnings.extend(legacy.warnings)
    if duplicate_scheduler_present(selected, run_systemctl=runner):
        warnings.append(
            "legacy hermes-auto-update.timer still present; refusing duplicate schedulers"
        )
        if changed:
            _rollback_unit_writes(selected, previous_units, runner, warnings)
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
        if changed:
            _rollback_unit_writes(selected, previous_units, runner, warnings)
    else:
        try:
            _write_owner_record(selected, current_home)
        except OSError as exc:
            warnings.append(f"failed to record scheduler ownership: {exc}")
            if changed:
                _rollback_unit_writes(selected, previous_units, runner, warnings)

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
