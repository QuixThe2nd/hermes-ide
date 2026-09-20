"""Platform capability and systemd install-scope detection."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class InstallScope:
    system: bool
    unit_dir: Path
    systemctl_prefix: tuple[str, ...]


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def platform_supported(
    *,
    is_linux_fn: Callable[[], bool] = is_linux,
    systemctl_fn: Callable[[], bool] = systemctl_available,
    supports_systemd_fn: Callable[[], bool] | None = None,
) -> bool:
    if not is_linux_fn():
        return False
    if not systemctl_fn():
        return False
    if supports_systemd_fn is None:
        from hermes_cli.gateway import supports_systemd_services

        supports_systemd_fn = supports_systemd_services
    return bool(supports_systemd_fn())


def _gateway_system_unit_exists() -> bool:
    try:
        from hermes_cli.gateway import get_systemd_unit_path

        return get_systemd_unit_path(system=True).exists()
    except Exception:
        return False


def _gateway_user_unit_exists() -> bool:
    try:
        from hermes_cli.gateway import get_systemd_unit_path

        return get_systemd_unit_path(system=False).exists()
    except Exception:
        return False


def _user_systemd_reachable() -> bool:
    try:
        from hermes_cli.gateway import _user_systemd_socket_ready

        return bool(_user_systemd_socket_ready())
    except Exception:
        return False


def detect_install_scope(
    *,
    hermes_home: Path | None = None,
    euid: int | None = None,
    gateway_system_unit_exists: Callable[[], bool] = _gateway_system_unit_exists,
    gateway_user_unit_exists: Callable[[], bool] = _gateway_user_unit_exists,
    user_systemd_reachable: Callable[[], bool] = _user_systemd_reachable,
    is_linux_fn: Callable[[], bool] = is_linux,
) -> InstallScope | None:
    """Pick system vs user systemd scope from real install metadata."""
    if not is_linux_fn():
        return None

    home = (hermes_home or get_hermes_home()).resolve()
    if euid is None:
        uid = os.getuid() if hasattr(os, "getuid") else 0  # windows-footgun: ok — Linux-only scope probe
    else:
        uid = euid

    use_system = False
    if gateway_system_unit_exists() and not gateway_user_unit_exists():
        use_system = True
    elif uid == 0:
        use_system = True
    else:
        try:
            st = home.stat()
            if st.st_uid not in (0, uid):
                use_system = True
        except OSError:
            return None

    if use_system:
        return InstallScope(
            system=True,
            unit_dir=Path("/etc/systemd/system"),
            systemctl_prefix=("systemctl",),
        )

    if not user_systemd_reachable():
        return None

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    return InstallScope(
        system=False,
        unit_dir=unit_dir,
        systemctl_prefix=("systemctl", "--user"),
    )


def build_systemctl_cmd(scope: InstallScope, *args: str) -> list[str]:
    return [*scope.systemctl_prefix, *args]


def resolve_python_executable() -> str:
    """Return the interpreter that should run ``hermes_cli.main`` in units."""
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv:
        candidate = Path(venv) / "bin" / "python"
        if candidate.is_file():
            # NOTE(future-me): do NOT resolve() here. A venv's ``python`` is a
            # symlink to the base interpreter; resolving it yields a python
            # without hermes_cli installed (observed with uv-managed pythons),
            # producing units that die with ModuleNotFoundError. The symlink
            # path itself is the correct interpreter for systemd units.
            return str(candidate)
    # Prefer the interpreter running enable/reconcile. shutil.which("python3")
    # is often /usr/bin/python3 with no hermes_cli, so units die immediately
    # when VIRTUAL_ENV is unset (interactive shells, systemd without the var).
    return sys.executable or shutil.which("python3") or "python3"


def build_hermes_argv(*extra: str) -> list[str]:
    python = resolve_python_executable()
    return [python, "-m", "hermes_cli.main", *extra]


def profile_cli_args() -> list[str]:
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        return ["-p", profile]
    return []


def unit_exec_start_argv() -> list[str]:
    return build_hermes_argv(*profile_cli_args(), "auto_update", "run")
