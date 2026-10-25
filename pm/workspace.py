"""The generated uv-workspace root that unions plugin deps into the venv.

Design (settled 2026-09-02, .hermes/plans/2026-09-02_164500-plugin-deps-
workspace-union.md):

- The workspace root is pm-GENERATED, never the committed pyproject.toml.
  Sealed installs are read-only, and the member list is machine-specific
  (which plugins the user enabled). Generated root lives beside the byte
  store: ``<store_root()>/.pm-workspace`` — per-install, writable on every
  install kind.
- Its pyproject = core's pyproject verbatim + a ``[tool.uv.workspace]``
  members block of relative ``../``-escaping paths to each enabled plugin
  dir. Relative members can escape the workspace root (probed live).
- ``uv lock`` resolves core + plugin deps as ONE graph: existing core pins
  are preserved (a plugin's range spec does not move them) and a conflict
  fails loudly, so the plugin simply does not install.
- ``uv sync --frozen --extra ...`` at the root installs the union into the
  project venv; ``UV_PROJECT_ENVIRONMENT`` pins WHICH venv that is.
- Plugin-member extras are banned/ignored for now (settled): uv selects
  only ROOT extras, and the root mirrors core's extras.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from pm import paths

WORKSPACE_DIRNAME = ".pm-workspace"


def workspace_root() -> Path:
    """The generated workspace root — per-install, beside the byte store."""
    return paths.store_root() / WORKSPACE_DIRNAME


def _member_rel(root: Path, plugin_dir: Path) -> str:
    """Workspace member string: relative path from the root to the plugin
    dir, always escaping the root (``../…``) — probed to resolve."""
    return os.path.relpath(plugin_dir.resolve(), root.resolve()).replace("\\", "/")


def members_stamp(plugin_dirs: list[Path]) -> str:
    """Content hash of the member SET — order-independent, so venv stamps
    compare the set of unioned plugins, not the discovery order."""
    resolved = sorted({str(p.resolve()) for p in plugin_dirs})
    h = hashlib.sha256()
    for entry in resolved:
        h.update(entry.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def build_root(plugin_dirs: list[Path]) -> Path:
    """(Re)generate the workspace root's pyproject.toml from core's
    pyproject + the enabled plugin members. Idempotent — same inputs,
    same bytes."""
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)

    core_pyproject = paths.repo_root() / "pyproject.toml"
    core_text = core_pyproject.read_text(encoding="utf-8-sig")

    members = [_member_rel(root, Path(p)) for p in {str(Path(p).resolve()) for p in plugin_dirs}]

    lines = [core_text.rstrip("\n")]
    if members:
        lines.append("")
        lines.append("[tool.uv.workspace]")
        lines.append("members = [" + ", ".join(f'"{m}"' for m in sorted(members)) + "]")

    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _plugin_dir_roots() -> set[Path]:
    """All profile plugin dirs + the default home's, machine-wide."""
    roots: set[Path] = set()
    try:
        profiles_root = Path.home() / ".hermes" / "profiles"
        if profiles_root.is_dir():
            for profile in profiles_root.iterdir():
                if profile.is_dir():
                    roots.add(profile / "plugins")
    except OSError:
        pass
    try:
        from hermes_constants import get_default_hermes_root

        roots.add(get_default_hermes_root() / "plugins")
    except Exception:
        pass
    return roots


def _is_member_candidate(plugin_dir: Path) -> bool:
    """A plugin dir is a workspace-member candidate when it declares python
    deps: a pyproject.toml (modern), or legacy pip_dependencies/
    python_dependencies in plugin.yaml (the bridge materializes those)."""
    try:
        if (plugin_dir / "pyproject.toml").is_file():
            return True
        manifest = plugin_dir / "plugin.yaml"
        if manifest.is_file():
            text = manifest.read_text(encoding="utf-8-sig")
            return "pip_dependencies" in text or "python_dependencies" in text
    except OSError:
        return False
    return False


def enabled_member_dirs() -> list[Path]:
    """Plugin dirs that carry python deps, machine-wide across profiles.
    Per-install union: profiles share the venv, so their enabled plugins
    share the resolution graph (settled)."""
    member_dirs: list[Path] = []
    for plugins_dir in sorted(_plugin_dir_roots(), key=str):
        try:
            if not plugins_dir.is_dir():
                continue
            entries = sorted(plugins_dir.iterdir(), key=str)
        except OSError:
            continue
        for plugin_dir in entries:
            try:
                if plugin_dir.is_dir() and _is_member_candidate(plugin_dir):
                    member_dirs.append(plugin_dir)
            except OSError:
                continue
    return member_dirs


def lock_and_sync(
    plugin_dirs: list[Path],
    extras: Optional[list[str]] = None,
    *,
    venv_dir: Optional[Path] = None,
) -> None:
    """Build the root, then `uv lock` + `uv sync --frozen --extra ...`.

    Raises InstallError on any failure (the caller surfaces the resolver's
    message — that IS the plugin-conflict UX). ``venv_dir`` pins
    UV_PROJECT_ENVIRONMENT; default is the core project venv.
    """
    from pm.package import InstallError
    from pm.packages import uv_env

    root = build_root(plugin_dirs)

    if venv_dir is None:
        venv_dir = _default_venv_dir()

    uv_bin = _uv_binary()
    if uv_bin is None:
        raise InstallError("venv", "uv is not installed (pm ensure uv)")

    env = uv_env()
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)

    import subprocess

    lock = subprocess.run(
        [uv_bin, "lock"], cwd=str(root), env=env, capture_output=True, text=True
    )
    if lock.returncode != 0:
        raise InstallError("venv", f"uv lock exited {lock.returncode}: {lock.stderr[-600:]}")

    cmd = [uv_bin, "sync", "--frozen"]
    for extra in sorted(set(extras or [])):
        cmd += ["--extra", extra]
    sync = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True)
    if sync.returncode != 0:
        raise InstallError(
            "venv", f"uv sync exited {sync.returncode}: {sync.stderr[-600:]}"
        )


def _default_venv_dir() -> Path:
    from pm.packages import Venv

    return Venv().venv_dir()


def _uv_binary() -> Optional[str]:
    from pm.ensure import uv as pm_uv

    uv_bin, _env = pm_uv(realize=False)
    return uv_bin
