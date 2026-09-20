"""Legacy ``hermes-auto-update.*`` unit handling."""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hermes_constants import get_hermes_home

from plugins.auto_update.platform import InstallScope, build_systemctl_cmd

logger = logging.getLogger(__name__)

LEGACY_SERVICE = "hermes-auto-update.service"
LEGACY_TIMER = "hermes-auto-update.timer"
LEGACY_WRAPPER_NAME = "hermes-auto-update.sh"
LEGACY_KNOWN_EXEC_START = "ExecStart=/opt/hermes/bin/hermes-auto-update.sh"
LEGACY_KNOWN_ON_CALENDAR = "OnCalendar=*-*-* *:00,30:00"

# Positive fingerprints for the shipped legacy units (behavioral reference only).
LEGACY_KNOWN_REFERENCE_TIMER = """[Unit]
Description=Hermes Agent - Idle-Gated Auto-Update Schedule (root canonical deployment)

[Timer]
OnCalendar=*-*-* *:00,30:00
RandomizedDelaySec=0
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
"""

LEGACY_KNOWN_REFERENCE_SERVICE = """[Unit]
Description=Hermes Agent - Idle-Gated Auto-Update (root canonical deployment)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=30min
SuccessExitStatus=0
Restart=no
ExecStart=/opt/hermes/bin/hermes-auto-update.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=timers.target
"""

LEGACY_KNOWN_SERVICE_HASH = hashlib.sha256(
    LEGACY_KNOWN_REFERENCE_SERVICE.strip().encode("utf-8")
).hexdigest()
LEGACY_KNOWN_TIMER_HASH = hashlib.sha256(
    LEGACY_KNOWN_REFERENCE_TIMER.strip().encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class LegacyMigrationResult:
    disabled_units: tuple[str, ...]
    warnings: tuple[str, ...]
    refused: tuple[str, ...]
    backed_up_units: tuple[str, ...]


def _read_unit(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _unit_content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _backup_unit(path: Path) -> Path | None:
    backup_dir = get_hermes_home() / "auto-update" / "legacy-units"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / path.name
    if dest.exists():
        return dest
    try:
        shutil.copy2(path, dest)
        return dest
    except OSError as exc:
        logger.debug("legacy unit backup failed for %s: %s", path, exc)
        return None


def identify_legacy_service(content: str) -> str:
    """Return ``known``, ``unknown``, or ``absent`` for legacy service content."""
    text = content or ""
    if not text.strip():
        return "absent"
    lines = {line.strip() for line in text.splitlines() if line.strip()}
    if LEGACY_KNOWN_EXEC_START in lines:
        return "known"
    if _unit_content_hash(text) == LEGACY_KNOWN_SERVICE_HASH:
        return "known"
    return "unknown"


def identify_legacy_timer(content: str) -> str:
    """Return ``known``, ``unknown``, or ``absent`` for legacy timer content."""
    text = content or ""
    if not text.strip():
        return "absent"
    lines = {line.strip() for line in text.splitlines() if line.strip()}
    if LEGACY_KNOWN_ON_CALENDAR in lines:
        return "known"
    if _unit_content_hash(text) == LEGACY_KNOWN_TIMER_HASH:
        return "known"
    return "unknown"


def _disable_unit(
    scope: InstallScope,
    unit: str,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]],
) -> bool:
    for args in (("disable", "--now", unit), ("stop", unit)):
        code, _, _ = run_systemctl(build_systemctl_cmd(scope, *args))
        if code not in (0, 5):
            return False
    return True


def migrate_legacy_units(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]],
) -> LegacyMigrationResult:
    disabled: list[str] = []
    warnings: list[str] = []
    refused: list[str] = []
    backed_up: list[str] = []

    for unit, identify in (
        (LEGACY_SERVICE, identify_legacy_service),
        (LEGACY_TIMER, identify_legacy_timer),
    ):
        path = scope.unit_dir / unit
        if not path.exists():
            continue
        content = _read_unit(path)
        if content is None:
            warnings.append(f"could not read legacy unit {path}")
            refused.append(unit)
            continue

        verdict = identify(content)
        if verdict == "unknown":
            warnings.append(
                f"legacy unit {unit} has unknown content; refusing to modify administrator-owned file"
            )
            refused.append(unit)
            continue
        if verdict != "known":
            continue

        backup = _backup_unit(path)
        if backup is not None:
            backed_up.append(unit)

        if _disable_unit(scope, unit, run_systemctl=run_systemctl):
            disabled.append(unit)
        else:
            warnings.append(f"failed to disable legacy unit {unit}")

    return LegacyMigrationResult(
        disabled_units=tuple(disabled),
        warnings=tuple(warnings),
        refused=tuple(refused),
        backed_up_units=tuple(backed_up),
    )


def legacy_timer_still_enabled(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]],
) -> bool:
    if not (scope.unit_dir / LEGACY_TIMER).exists():
        return False
    code, out, _ = run_systemctl(
        build_systemctl_cmd(scope, "is-enabled", LEGACY_TIMER)
    )
    state = out.strip().lower()
    return code == 0 and state in {"enabled", "static"}


def duplicate_scheduler_present(
    scope: InstallScope,
    *,
    run_systemctl: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
) -> bool:
    """True when a legacy timer remains enabled alongside the plugin timer."""
    if run_systemctl is None:
        from plugins.auto_update.systemd import default_systemctl_runner

        run_systemctl = default_systemctl_runner
    return legacy_timer_still_enabled(scope, run_systemctl=run_systemctl)
