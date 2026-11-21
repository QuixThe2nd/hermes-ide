"""Dependency install execution shared between early recovery and full recovery.

Both callers need to run the same core ``.[all]`` reinstall:

- ``hermes_cli._early_recovery.recover_if_needed`` — stdlib-only, runs BEFORE
  ``hermes_cli.main``'s third-party imports, so it can complete a pending
  update while no native extension is mapped yet (#83569).
- ``hermes_cli.main_install_repair._recover_core_update_marker_locked`` — the historical
  post-import recovery path. Kept as a fallback for installs the early pass
  could not complete (marker left in place on failure).

This module is deliberately **stdlib-only** so importing it can never fail in
the corrupted-venv state it exists to repair. ``hermes_cli.main_install_repair``
imports ``pm``, ``hermes_constants``, and friends only in its late path; the
early path must not. Where the late path uses ``pm.uv()`` to
realize uv if missing, the early path uses the stdlib
:func:`hermes_cli._early_recovery._find_uv_binary` lookup and falls back to
plain pip when uv is absent — a degraded but working installer (the late
recovery will bootstrap uv on the next launch if it ever matters).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

# _early_recovery owns the recovery-lock lifecycle and uv lookup; importing it is free (stdlib).
from hermes_cli import _early_recovery as _er


def _stdout_to_stderr():
    """Route fd 1 (and sys.stdout) to stderr for the duration of an install.

    ``hermes acp`` speaks JSON-RPC on stdout; an inherited-fd install child writing there would
    corrupt the protocol.
    """
    saved_sys_stdout = sys.stdout
    try:
        saved_fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        saved_fd = None
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved_sys_stdout
        if saved_fd is not None:
            with contextlib.suppress(OSError):
                os.dup2(saved_fd, 1)
            with contextlib.suppress(OSError):
                os.close(saved_fd)


def _resolve_install_target(root: Path) -> tuple[list[str], dict | None]:
    """(install_cmd_prefix, env) for the project venv — stdlib uv lookup.

    Mirrors ``main_install_repair::_default_venv_install_target`` but without ``pm``. ``VIRTUAL_ENV`` steers ``uv pip`` at the project venv even
    when invoked from the base interpreter (the early-recovery case).
    """
    uv_bin = _er._find_uv_binary()
    if uv_bin:
        from hermes_constants import project_venv_dir

        env = {**os.environ, "VIRTUAL_ENV": str(project_venv_dir(root) or root / "venv")}
        return [uv_bin, "pip"], env
    return [sys.executable, "-m", "pip"], None






def _run_install_cmd(cmd: list[str], *, env: dict | None, root: Path) -> None:
    """Run an install command; raises CalledProcessError on failure (callers implement the
    per-extra fallback ladder)."""
    subprocess.run(cmd, cwd=root, check=True, env=env)


def _load_installable_optional_extras(root: Path, group: str) -> list[str]:
    """Optional extras referenced by a dependency group (all)."""
    project = _er._load_pyproject_project(root)
    if project is None:
        return []
    optional_deps = project.get("optional-dependencies", {})
    if not isinstance(optional_deps, dict):
        return []
    referenced: list[str] = []
    for ref in optional_deps.get(group, []):
        if "[" in ref and "]" in ref:
            name = ref.split("[", 1)[1].split("]", 1)[0]
            if name in optional_deps:
                referenced.append(name)
    return referenced


def run_core_install(root: Path) -> None:
    """Full core ``.[all]`` editable reinstall — the recovery install.

    Equal in behavior to the install half of
    ``main_install_repair::_recover_core_update_marker_locked``:

    - bootstrap pip via ensurepip (a killed install can leave the venv with no
      pip module at all)
    - prefer ``uv pip`` with VIRTUAL_ENV pointed at the project venv; fall back
      to ``python -m pip`` when no uv binary is available
    - target ``.[all]`` with the per-extra fallback ladder when the combined
      extras resolve fails
    - route ALL install output to stderr (acp/JSON-RPC safety)

    Raises ``subprocess.CalledProcessError`` when even the base install fails;
    callers own marker lifecycle (clear on success, keep on failure).
    """
    prefix, env = _resolve_install_target(root)
    group = "all"

    def install(target: str) -> None:
        _run_install_cmd(prefix + ["install", "-e", target], env=env, root=root)

    with _stdout_to_stderr():
        _er._run_ensurepip(root)
        try:
            install(f".[{group}]")
            return
        except subprocess.CalledProcessError:
            print("  ⚠ Optional extras failed, reinstalling base dependencies "
                  "and retrying extras individually...")
        install(".")
        failed_extras: list[str] = []
        installed_extras: list[str] = []
        for extra in _load_installable_optional_extras(root, group):
            try:
                install(f".[{extra}]")
                installed_extras.append(extra)
            except subprocess.CalledProcessError:
                failed_extras.append(extra)
        if installed_extras:
            print("  ✓ Reinstalled optional extras individually: " + ", ".join(installed_extras))
        if failed_extras:
            print("  ⚠ Skipped optional extras that still failed: " + ", ".join(failed_extras))


def bump_marker_attempts(marker_path: Path) -> int:
    """Increment the attempts counter stored inside the marker file's JSON body.

    The marker's existence is the signal; the body carries the retry count so a persistently
    failing install can back off. Corrupt/missing bodies restart at 1. Never raises.
    """
    attempts = _er._read_marker_attempts(marker_path) + 1
    with contextlib.suppress(OSError):
        marker_path.write_text(json.dumps({"attempts": attempts}), encoding="utf-8")
    return attempts
