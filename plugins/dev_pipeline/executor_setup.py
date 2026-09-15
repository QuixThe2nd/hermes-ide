"""Self-install of the dev-pipeline executor systemd user service.

The executor used to ship as a "copy and edit paths" unit (see
``systemd/README.md``), so fresh deployments queued ``delegate_development``
jobs that nothing ever claimed. Mirroring ``plugins/auto_update`` (reconcile on
gateway start), the unit is now written, daemon-reloaded when changed, and
enabled on plugin load — Linux with a reachable systemd **user** manager only.
Every other platform logs once and stays out of the way; reconcile never
raises into plugin load.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from hermes_constants import (
    get_hermes_home,
    hermes_managed_node_tree_present,
    iter_hermes_node_dirs,
)
from plugins.auto_update.platform import (
    InstallScope,
    build_systemctl_cmd,
    detect_install_scope,
    platform_supported,
)
from plugins.auto_update.systemd import (
    atomic_write,
    default_systemctl_runner,
    format_environment,
    format_exec_start,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "hermes-dev-executor.service"

# Repo root = two levels up from plugins/dev_pipeline/executor_setup.py
# (same anchor as ``_REPO_ROOT`` in executor.py) — never the cwd of whatever
# process happened to load the plugin.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Standard bins for the minimal PATH a systemd service starts with (mirrors
# the gateway unit's ``common_bin_paths``).
_COMMON_BIN_DIRS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)

SystemctlRunner = Callable[[Sequence[str]], tuple[int, str, str]]

_skip_logged = False


@dataclass(frozen=True)
class ExecutorSetupResult:
    supported: bool
    scope: InstallScope | None
    changed: bool
    enabled: bool
    warnings: tuple[str, ...]


def executor_python_executable() -> str:
    """The interpreter running the plugin — the right venv python for the unit.

    Deliberately NOT ``shutil.which("python3")``: resolving the ambient python
    produced units pointing at ``/usr/bin/python3`` with no hermes tree
    importable (observed live), which crash-looped the executor at import.
    """
    return sys.executable


def executor_service_path(
    *,
    home_dir: Path,
    hermes_home: Path,
    python_executable: str,
) -> str:
    """PATH for the executor unit: venv bin, managed Node, ``~/.local/bin``.

    Both agent lanes shell out to node-based CLIs (Cursor / Claude Code), and a
    systemd service starts with a minimal PATH — so the unit carries its own.
    Managed-Node probing follows ``_append_node_dir_for_service`` in
    ``hermes_cli/gateway.py``: the Hermes-managed tree wins when present; the
    ambient ``node`` lookup is only the fallback rung for installs without one.
    """
    entries: list[str] = []

    def _add(entry: str) -> None:
        if entry and entry not in entries:
            entries.append(entry)

    # Lexical parent of the interpreter (never .resolve() — a venv python is a
    # symlink; chasing it yields the base interpreter's bin dir).
    _add(str(Path(python_executable).parent))

    if hermes_managed_node_tree_present(hermes_home):
        for node_dir in iter_hermes_node_dirs(hermes_home):
            try:
                if node_dir.is_dir():
                    _add(str(node_dir))
            except OSError:
                continue
    else:
        resolved_node = shutil.which("node")
        if resolved_node:
            _add(str(Path(resolved_node).parent))

    local_bin = home_dir / ".local" / "bin"
    try:
        if local_bin.is_dir():
            _add(str(local_bin))
    except OSError:
        pass

    entries.extend(_COMMON_BIN_DIRS)
    return os.pathsep.join(entries)


def render_executor_service_unit(
    *,
    python_executable: str,
    home_dir: str,
    hermes_home: str,
    repo_root: str,
    path_value: str,
) -> str:
    return f"""[Unit]
Description=Hermes dev-pipeline executor (durable Cursor lane)
After=network.target

[Service]
Type=simple
ExecStart={format_exec_start([python_executable, "-m", "plugins.dev_pipeline.executor", "run"])}
{format_environment("HOME", home_dir)}
{format_environment("HERMES_HOME", hermes_home)}
{format_environment("PYTHONPATH", repo_root)}
{format_environment("PATH", path_value)}
WorkingDirectory={format_exec_start([repo_root])}
Restart=always
TimeoutStopSec=30
# KillMode=mixed is acceptable HERE only: attempt units run as separate
# transient systemd units outside this service cgroup (hermes-dev-<task>-<run>).
KillMode=mixed
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def executor_unit_path(scope: InstallScope) -> Path:
    return scope.unit_dir / SERVICE_NAME


def _write_unit_if_changed(path: Path, body: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing == body:
        return False
    atomic_write(path, body)
    return True


def _log_skip_once(reason: str) -> None:
    global _skip_logged
    if _skip_logged:
        return
    _skip_logged = True
    logger.info("dev_pipeline executor self-install skipped: %s", reason)


def _reconcile(
    *,
    run_systemctl: SystemctlRunner | None,
    scope: InstallScope | None,
) -> ExecutorSetupResult:
    if not platform_supported():
        _log_skip_once("platform has no systemd user services (Linux/systemd user scope required)")
        return ExecutorSetupResult(
            supported=False, scope=None, changed=False, enabled=False, warnings=()
        )

    selected = scope or detect_install_scope()
    if selected is None:
        _log_skip_once("systemd user manager unavailable for this install")
        return ExecutorSetupResult(
            supported=False,
            scope=None,
            changed=False,
            enabled=False,
            warnings=("systemd user manager unavailable for this install",),
        )
    if selected.system:
        _log_skip_once(
            "system-scope install detected; executor self-install is user-scope "
            "only (see plugins/dev_pipeline/systemd/README.md for manual setup)"
        )
        return ExecutorSetupResult(
            supported=False,
            scope=selected,
            changed=False,
            enabled=False,
            warnings=(
                "system-scope systemd install; executor self-install supports user scope only",
            ),
        )

    runner = run_systemctl or default_systemctl_runner
    python_executable = executor_python_executable()
    hermes_home = get_hermes_home().resolve()
    body = render_executor_service_unit(
        python_executable=python_executable,
        home_dir=str(Path.home()),
        hermes_home=str(hermes_home),
        repo_root=str(REPO_ROOT),
        path_value=executor_service_path(
            home_dir=Path.home(),
            hermes_home=hermes_home,
            python_executable=python_executable,
        ),
    )

    changed = _write_unit_if_changed(executor_unit_path(selected), body)
    warnings: list[str] = []
    if changed:
        code, _, err = runner(build_systemctl_cmd(selected, "daemon-reload"))
        if code != 0:
            warnings.append(f"failed to daemon-reload: {err.strip() or code}")

    # Enable + best-effort start in one step; an already-running service is
    # left alone. Failure here (masked unit, no user bus at this moment) is a
    # warning, never a plugin-load failure.
    code, _, err = runner(
        build_systemctl_cmd(selected, "enable", "--now", SERVICE_NAME)
    )
    enabled = code == 0
    if not enabled:
        warnings.append(f"failed to enable/start {SERVICE_NAME}: {err.strip() or code}")

    return ExecutorSetupResult(
        supported=True,
        scope=selected,
        changed=changed,
        enabled=enabled,
        warnings=tuple(warnings),
    )


def reconcile_executor_on_load(
    *,
    run_systemctl: SystemctlRunner | None = None,
    scope: InstallScope | None = None,
) -> ExecutorSetupResult | None:
    """Install/reconcile the executor user unit. Never raises.

    Runs regardless of ``dev_pipeline.enabled`` — that flag only gates
    work-claiming inside the executor; the unit itself must exist so flipping
    the flag is all a deployment needs.
    """
    try:
        return _reconcile(run_systemctl=run_systemctl, scope=scope)
    except Exception as exc:
        logger.warning(
            "dev_pipeline executor reconcile skipped: %s", exc, exc_info=True
        )
        return None
