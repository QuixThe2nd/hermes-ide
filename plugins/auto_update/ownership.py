"""Persistent ownership for the machine-scoped auto-update scheduler.

The sidecar lives with the systemd units, not under HERMES_HOME: every profile
must see the same ownership record. Existing installations are backfilled from
the service unit's HERMES_HOME environment entry. A failed write after the
sidecar is claimed is retried by the same owner or repaired with an explicit
manual takeover; no existing unit or state is deleted during migration.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

from plugins.auto_update.platform import InstallScope

OWNER_FILENAME = "hermes-auto-updater.owner.json"
_OWNER_VERSION = 1
_REFUSAL_PREFIX = "auto_update scheduler ownership refusal"


def ownership_path(
    scope: InstallScope, *, state_dir: str | Path | None = None
) -> Path:
    root = Path(state_dir) if state_dir is not None else scope.unit_dir
    return root / OWNER_FILENAME


def is_disposable_home(home: str | Path) -> bool:
    """Recognize tmpfs/runtime homes without treating all of $HOME as temporary."""
    candidate = Path(home).resolve()
    roots = {
        Path(path).resolve() for path in ("/tmp", "/var/tmp", "/dev/shm", "/run")
    }
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root != Path(temp_root.anchor):
        roots.add(temp_root)
    return any(candidate == root or root in candidate.parents for root in roots)


def _normalize_home(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return str(path.resolve())


def _read_record(path: Path) -> tuple[bool, str | None, str | None]:
    if not path.exists():
        return False, None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _OWNER_VERSION:
            raise ValueError("unsupported ownership record")
        home = _normalize_home(raw.get("hermes_home"))
        if home is None:
            raise ValueError("invalid HERMES_HOME in ownership record")
        return True, home, None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return True, None, f"cannot read {path}: {exc}"


def _read_service_home(path: Path) -> tuple[bool, str | None, str | None]:
    if not path.exists():
        return False, None, None
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        return True, None, f"cannot read {path}: {exc}"

    homes: set[str] = set()
    try:
        for line in body.splitlines():
            if not line.startswith("Environment="):
                continue
            for assignment in shlex.split(line[len("Environment=") :], posix=True):
                if assignment.startswith("HERMES_HOME="):
                    home = _normalize_home(
                        assignment[len("HERMES_HOME=") :].replace("%%", "%")
                    )
                    if home is None:
                        raise ValueError("invalid HERMES_HOME environment entry")
                    homes.add(home)
    except ValueError as exc:
        return True, None, f"cannot parse {path}: {exc}"

    if len(homes) != 1:
        detail = "missing HERMES_HOME" if not homes else "conflicting HERMES_HOME entries"
        return True, None, f"cannot establish ownership from {path}: {detail}"
    return True, next(iter(homes)), None


def _write_record(path: Path, home: str, *, exclusive: bool) -> str | None:
    payload = json.dumps(
        {"version": _OWNER_VERSION, "hermes_home": home}, sort_keys=True
    ) + "\n"
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        else:
            fd, temporary_name = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            temporary_path = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary_path is not None:
            os.replace(temporary_path, path)
        return None
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # Preserve the original write refusal if cleanup also fails.
                pass
        return f"failed to persist auto_update scheduler ownership at {path}: {exc}"


def _refusal(detail: str) -> str:
    return (
        f"{_REFUSAL_PREFIX}: {detail}; use "
        "`hermes auto_update reconcile --takeover` to explicitly replace it"
    )


def enforce_scheduler_ownership(
    scope: InstallScope,
    *,
    service_path: Path,
    timer_path: Path,
    hermes_home: str,
    enabled: bool,
    takeover: bool = False,
    state_dir: str | Path | None = None,
) -> str | None:
    """Return a refusal before mutation, or persist ownership and return None."""
    current = _normalize_home(hermes_home)
    if current is None:
        return _refusal(f"invalid current HERMES_HOME {hermes_home!r}")
    path = ownership_path(scope, state_dir=state_dir)

    if takeover:
        if not enabled:
            # An explicit reconcile must not re-enable or re-own a scheduler the
            # operator deliberately disabled; it only permits stop/disable.
            return None
        return _write_record(path, current, exclusive=False)

    if enabled and is_disposable_home(current):
        return _refusal(f"HERMES_HOME {current} is disposable")

    record_present, recorded_home, record_error = _read_record(path)
    service_present, unit_home, unit_error = _read_service_home(service_path)
    units_present = service_present or timer_path.exists()
    error = record_error or unit_error
    if error:
        return _refusal(error)

    if (
        recorded_home is not None
        and unit_home is not None
        and recorded_home != unit_home
    ):
        return _refusal(
            f"recorded owner {recorded_home} disagrees with unit owner {unit_home}"
        )

    known_home = recorded_home or unit_home
    if known_home is None and (record_present or units_present) and not enabled:
        return _refusal("existing scheduler has no verifiable owner")
    if known_home is not None and known_home != current:
        return _refusal(
            f"existing scheduler belongs to {known_home}, not current HERMES_HOME {current}"
        )
    if known_home is None and units_present:
        return _refusal("existing scheduler has no verifiable owner")

    if not enabled or (record_present and recorded_home == current):
        return None
    return _write_record(path, current, exclusive=not record_present)
