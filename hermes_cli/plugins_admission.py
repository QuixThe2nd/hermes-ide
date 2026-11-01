"""Validate a proposed plugin set, then publish config and runtime selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


class AdmissionRefused(RuntimeError):
    """The candidate set was refused; config and environment untouched."""


def candidate_member_dirs(
    candidate_enabled: Iterable[str],
    candidate_disabled: Iterable[str] = (),
    *,
    active_plugins_dir: Optional[Path] = None,
    extra_dirs: Iterable[Path] = (),
) -> list[Path]:
    """Member dirs implied by the PROPOSED enabled sets: per plugins dir,
    its enabled names — the active dir's replaced by the candidate set
    (removals excluded), other homes unchanged — filtered to dirs that
    actually declare python deps. ``extra_dirs`` (the install target)
    join when they declare deps."""
    from pm.workspace import enabled_member_dirs, _is_member_candidate

    active = Path(active_plugins_dir) if active_plugins_dir else None
    members = enabled_member_dirs(
        proposed_home=active.parent if active else None,
        enabled=candidate_enabled, disabled=candidate_disabled,
    )
    for directory in extra_dirs:
        directory = Path(directory)
        if directory not in members and _is_member_candidate(directory):
            members.append(directory)
    return members


def _config_path() -> Path:
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "config.yaml"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".admission-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _config_commit(candidate_enabled: set, candidate_disabled: set):
    """The before_publish hook: commit the config EXACTLY ONCE while pm
    holds the install lock. Returns an undo callable — invoked by
    sync_venv only when the subsequent facts write fails — that restores
    the previous config bytes atomically (or removes a config this
    commit created)."""
    path = _config_path()
    existed = path.is_file()
    previous = path.read_bytes() if existed else None

    commit_plugin_sets(candidate_enabled, candidate_disabled)

    def undo() -> None:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, previous)

    return undo


def commit_plugin_sets(enabled: set, disabled: set) -> None:
    """Write BOTH plugin keys in ONE atomic config save."""
    from hermes_cli.config import read_raw_config
    from utils import atomic_roundtrip_yaml_save

    path = _config_path()
    config = read_raw_config()
    plugins_cfg = config.setdefault("plugins", {})
    if not isinstance(plugins_cfg, dict):
        raise ValueError(f"plugins must be a mapping in {path}")
    plugins_cfg["enabled"] = sorted(enabled)
    plugins_cfg["disabled"] = sorted(disabled)
    atomic_roundtrip_yaml_save(path, config)


def admit_plugin_set_change(
    candidate_enabled: set,
    candidate_disabled: set,
    *,
    active_plugins_dir: Optional[Path] = None,
    extra_dirs: Iterable[Path] = (),
) -> None:
    """Validate the proposed sets against the active environment and
    commit — env selection and config move together, inside pm's single
    locked transaction.

    Raises :class:`AdmissionRefused` BEFORE anything is published when
    the candidate union fails to resolve (conflict, frozen feature set,
    …) or when the config write itself fails. The active environment and
    the previous config bytes are kept EXACTLY — no rollback re-resolve.
    """
    from pm.ensure import sync_venv

    members = candidate_member_dirs(
        candidate_enabled, candidate_disabled, active_plugins_dir=active_plugins_dir, extra_dirs=extra_dirs
    )
    try:
        sync_venv(
            explicit=True,
            plugin_dirs=members,
            before_publish=lambda: _config_commit(candidate_enabled, candidate_disabled),
        )
    except Exception as exc:
        raise AdmissionRefused(str(exc)) from exc
