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
import time
from pathlib import Path

# _early_recovery owns the recovery-lock lifecycle and uv lookup; importing it is free (stdlib).
from hermes_cli import _early_recovery as _er


def _is_windows() -> bool:
    return sys.platform == "win32"


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


def _venv_scripts_dir(root: Path) -> Path | None:
    """Project venv Scripts/bin dir, when present (hermes_constants is stdlib-only)."""
    # hermes_constants is stdlib-only, so the canonical layout helpers are safe to use from this
    # corrupted-venv repair path (#76105: never open-code the Scripts/bin split).
    from hermes_constants import project_venv_dir, venv_bin_dir

    venv_dir = project_venv_dir(root)
    if venv_dir is None:
        return None
    scripts = venv_bin_dir(venv_dir, windows=_is_windows())
    return scripts if scripts.is_dir() else None


#: Launcher command names install.ps1's Set-PathVariable exposes from the
#: managed binary dir (the default Hermes root's ``bin``, next to uv.exe)
#: on the user PATH. Keep in lockstep with WINDOWS_BIN_LAUNCHERS in
#: hermes_cli/_launchers.py and scripts/install.ps1.
_WINDOWS_BIN_LAUNCHERS = ("hermes", "hermes-acp")


def _normalize_windows_path(value) -> str:
    """Windows path equality key: backslashes, no trailing separator, lowered.

    ``.lower()`` rather than ``os.path.normcase`` (identity on POSIX) so the comparison behaves
    Windows-correct even when tests exercise the Windows branch from another host.
    """
    return str(value).replace("/", "\\").rstrip("\\").lower()


def _windows_user_path_entries() -> list[str]:
    """User PATH entries from the registry (what install.ps1 writes); process PATH fallback."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _kind = winreg.QueryValueEx(key, "Path")
        value = os.path.expandvars(str(raw))
    except (OSError, ImportError):
        value = os.environ.get("PATH", "")
    return [entry for entry in value.split(";") if entry.strip()]


def ensure_windows_bin_launchers(
    root, *, windows: bool | None = None, user_path_entries: list[str] | None = None,
) -> list[str]:
    """Re-stage the Windows ``hermes`` launchers when they vanish or when
    they still boot through the venv.

    On Windows, ``hermes`` resolves through staged launchers — never
    ``venv\\Scripts`` itself on PATH, which would shadow the user's
    ``python`` (#83797) — and under pm the launchers boot the pm STORE
    python with ``PYTHONPATH=<repo>;<venv>/site-packages``, never the venv
    interpreter (no-boot-through-venv; ``pyvenv.cfg`` is inert dead
    config). The canonical launcher home is
    the managed binary dir — the default Hermes root's ``bin``
    (``%LOCALAPPDATA%\\hermes\\bin``, next to the managed uv) — which lives
    OUTSIDE the git checkout so no git operation can ever touch it. It is
    a per-machine dir shared by every profile: ``get_hermes_home()`` would
    point inside ``profiles\\<name>`` under ``hermes -p``, so the anchor
    here is :func:`hermes_constants.get_default_hermes_root`.

    Earlier installer versions staged them at ``<checkout>\\bin`` instead —
    inside the git working tree — where ``hermes update``'s pre-update
    autostash (``git stash push --include-untracked``) swept them off disk;
    once the desktop updater stopped re-applying stashes (``--keep-stash``)
    nothing restored them and ``hermes`` stopped resolving in every new
    terminal. That legacy location is re-staged too, during the transition,
    for installs whose user PATH still resolves through it.

    A name counts as present when an exe exists that does NOT boot the
    venv interpreter — legacy copied-venv trampolines (detected by their
    embedded interpreter path) and placeholder .cmd delegators are replaced
    with a store-python launcher as soon as one can be minted.

    Two targets, two gates, both failing toward inaction:

    - canonical managed binary dir: only when *root* is the managed clone
      (``root.parent == get_default_hermes_root()``), so source checkouts
      elsewhere never gain launchers;
    - legacy ``<root>\\bin``: only when that dir is on the user PATH
      (registry value, process PATH as fallback), i.e. the install opted
      into the old layout and still resolves through it.

    Writes go through a staging name + ``os.replace`` so concurrent process
    starts cannot tear a launcher. Never raises; returns the restored paths.

    *windows* and *user_path_entries* are injectable for tests, same pattern
    as ``hermes_constants.venv_bin_dir``.
    """
    if windows is None:
        windows = _is_windows()
    if not windows:
        return []
    root = Path(root)
    home = _default_hermes_root()
    if home is None:
        return []

    def _launcher_present(target: Path, name: str) -> bool:
        return (target / f"{name}.exe").exists() or (target / f"{name}.cmd").exists()

    # Launchers boot the pm STORE python with PYTHONPATH=repo;site-packages
    # — never the venv interpreter (no boot through the venv; pyvenv.cfg is
    # inert dead config). mint_launcher prefers a distlib exe trampoline
    # bound to the store python; a runtime-resolving .cmd is written when
    # the store has not materialized a python yet, and the repair upgrades
    # it (and any legacy copied-venv trampoline) to the exe once the store
    # python exists. See hermes_cli/_launchers.py.
    from hermes_cli._launchers import (
        exe_is_venv_bound,
        mint_launcher,
        resolve_store_python,
        stage_launcher,
        venv_site_packages,
    )

    from hermes_constants import project_venv_dir

    venv_dir = project_venv_dir(root)
    site_packages = venv_site_packages(venv_dir) if venv_dir else None

    store_python = resolve_store_python(root)

    def _needs_attention(target: Path, name: str) -> bool:
        """Missing, a placeholder .cmd, or a launcher that still boots the
        venv interpreter — anything the store-python launcher should replace."""
        exe = target / f"{name}.exe"
        if not exe.exists():
            return True
        return exe_is_venv_bound(exe, venv_dir)

    targets: list[Path] = []
    # Runs at every hermes_cli.main process start, so the healthy path must stay a few stat calls.
    if _normalize_windows_path(root.parent) == _normalize_windows_path(home):
        canonical = home / "bin"
        if any(
            _needs_attention(canonical, name) for name in _WINDOWS_BIN_LAUNCHERS
        ):
            targets.append(canonical)
    # Legacy target: compared as normalized literal strings — the installer wrote the long literal
    # path, and realpath'ing arbitrary PATH entries could hang on dead network shares. An entry
    # stored another way (8.3 short path, subst drive) misses the re-stage, which fails safe.
    legacy = root / "bin"
    if any(_needs_attention(legacy, name) for name in _WINDOWS_BIN_LAUNCHERS):
        if user_path_entries is None:
            user_path_entries = _windows_user_path_entries()
        configured = {_normalize_windows_path(entry) for entry in user_path_entries}
        if _normalize_windows_path(legacy) in configured:
            targets.append(legacy)
    if not targets:
        return []

    restored: list[str] = []
    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for name in _WINDOWS_BIN_LAUNCHERS:
            if not _needs_attention(target, name):
                # Already a store-python launcher (or a form this heal does
                # not understand but that does not boot the venv): leave it.
                continue
            if store_python is not None:
                final = mint_launcher(name, root, target, store_python, site_packages)
            else:
                final = stage_launcher(name, root, target)
            if final is not None:
                restored.append(str(final))
    if restored:
        # A closed/broken stderr must not turn a successful heal into a crash.
        with contextlib.suppress(OSError, ValueError):
            print("  ✓ Restored hermes launcher(s): " + ", ".join(restored), file=sys.stderr)
    return restored


def _read_user_path_raw() -> tuple[list[str], int]:
    """Raw (unexpanded) user PATH entries + registry value type (a rewrite preserves ``%VARS%``)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            raw, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return [], winreg.REG_EXPAND_SZ
    return [entry for entry in str(raw).split(";") if entry], int(kind)


def _write_user_path_raw(entries: list[str], kind: int) -> None:
    """Write the user PATH back, preserving the registry value type."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries))


def migrate_windows_bin_path(
    root, *, windows: bool | None = None, read_user_path=None, write_user_path=None,
) -> bool:
    """One-time PATH migration to the ``HERMES_HOME\\bin`` launcher layout (``hermes update`` tail).

    1. stage launchers into the managed binary dir; 2. verify both are present — otherwise STOP,
    leaving the user PATH untouched (never strip a working entry before its replacement is proven);
    3. prepend the managed binary dir to the user PATH; 4. strip the legacy ``<root>\\bin`` and
    ``<root>\\venv\\Scripts`` entries. Legacy ``<root>\\bin`` FILES stay: configs that captured
    absolute launcher paths keep working and the dir is git-ignored. Registry writes preserve the
    stored value type and raw ``%VARS%``. Never raises; True when the canonical layout is in place.
    *read_user_path*/*write_user_path* are injectable for tests.

    See #83797.
    """
    if windows is None:
        windows = _is_windows()
    if not windows:
        return False
    root = Path(root)

    from hermes_constants import venv_bin_dir

    home = _default_hermes_root()
    if home is None:
        return False
    if _normalize_windows_path(root.parent) != _normalize_windows_path(home):
        return False  # not the managed clone — nothing to migrate

    ensure_windows_bin_launchers(root, windows=windows, user_path_entries=[])
    home_bin = home / "bin"
    if any(not ((home_bin / f"{name}.exe").is_file() or (home_bin / f"{name}.cmd").is_file())
           for name in _WINDOWS_BIN_LAUNCHERS):
        return False  # staging incomplete — leave the PATH alone

    if read_user_path is None:
        read_user_path = _read_user_path_raw
    if write_user_path is None:
        write_user_path = _write_user_path_raw
    try:
        entries, kind = read_user_path()
    except (OSError, ImportError):
        return False

    legacy_keys = {
        _normalize_windows_path(root / "bin"),
        # The old installer put the venv's Scripts dir itself on PATH, always at the literal
        # `venv` layout (never `.venv`) — match what it wrote then, not where the venv lives now.
        # See #83797.
        _normalize_windows_path(venv_bin_dir(root / "venv", windows=True)),
    }
    home_bin_key = _normalize_windows_path(home_bin)

    def _entry_key(entry: str) -> str:
        return _normalize_windows_path(os.path.expandvars(entry))

    kept = [e for e in entries if _entry_key(e) not in legacy_keys]
    if not any(_entry_key(e) == home_bin_key for e in kept):
        kept = [str(home_bin)] + kept
    if kept != entries:
        try:
            write_user_path(kept, kind)
        except (OSError, ImportError):
            return False
        with contextlib.suppress(OSError, ValueError):
            print(f"  ✓ hermes launchers now resolve from {home_bin} "
                  "(legacy PATH entries removed)", file=sys.stderr)
    return True


def _load_console_script_names(root: Path) -> list[str]:
    """``[project.scripts]`` names from pyproject.toml (tomllib, 3.11+)."""
    project = _er._load_pyproject_project(root)
    try:
        scripts = (project or {}).get("scripts", {}) or {}
        return [str(name) for name in scripts if name]
    except Exception:
        return []


class ShimQuarantineError(RuntimeError):
    """A live shim could not be renamed aside — the venv is contended.

    Raised BEFORE the install command runs. Callers catch it like any install failure: the
    update-incomplete marker survives and a later launch retries once the holder exits — the
    contended venv is never mutated.

    See #87331.
    """

    def __init__(self, failed_shims: list[str]):
        self.failed_shims = list(failed_shims)
        super().__init__("could not quarantine live shim(s): " + ", ".join(self.failed_shims))


def _quarantine_running_hermes_exe(
    scripts_dir: Path, *, failed_out: list[str] | None = None
) -> list[tuple[Path, Path]]:
    """Rename live hermes*.exe shims aside so the installer can rewrite them.

    Windows blocks REPLACE on a running .exe but allows RENAME. Best-effort: silently skips anything
    that cannot be renamed (names appended to *failed_out*). Returns (original, quarantined) pairs.
    The console-script set comes from pyproject ``[project.scripts]`` (fallback: well-known trio).

    ``failed_out``: when provided, names of shims that could not be renamed are appended so the caller can
    refuse instead of mutating a contended venv (#87331 fail-closed).
    """
    if not _is_windows():
        return []
    names = set(_load_console_script_names(scripts_dir.parent.parent)) or {
        "hermes", "hermes-agent", "hermes-acp"}
    names.add("hermes-gateway")
    moved: list[tuple[Path, Path]] = []
    for name in sorted(names):
        shim = scripts_dir / f"{name}.exe"
        if not shim.exists():
            continue
        quarantined = shim.with_name(f"{name}.exe.old.{int(time.time() * 1000)}")
        try:
            os.rename(shim, quarantined)
            moved.append((shim, quarantined))
        except OSError:
            if failed_out is not None:
                failed_out.append(shim.name)
    return moved


def _restore_quarantined_exes(moved: list[tuple[Path, Path]]) -> None:
    """Put quarantined shims back when the installer did not replace them (shared retry ladder).

    Delegates to the shared helper in the stdlib-only ``_early_recovery`` module: one retry ladder and one
    recovery message for every restore site, instead of the near-identical copies that had already drifted
    (#75584). Warnings land on stderr — this module runs in the early-recovery path and ``hermes acp``
    speaks JSON-RPC on stdout.
    """
    _er.restore_quarantined_shims(moved)


def _run_install_cmd(cmd: list[str], *, env: dict | None, root: Path) -> None:
    """Run an install command with quarantine protection for venv shims.

    Fail-closed: when any live shim cannot be renamed aside, the venv is contended and the
    installer would die partway on the same locks — raise :class:`ShimQuarantineError` WITHOUT
    running it. Raises CalledProcessError on install failure (callers implement the per-extra
    fallback ladder).

    The caller's marker-keeping failure handling turns that into "retry next launch". See #87331.
    """
    scripts_dir = _venv_scripts_dir(root) if _is_windows() else None
    failed: list[str] = []
    moved = _quarantine_running_hermes_exe(scripts_dir, failed_out=failed) if scripts_dir else []
    if failed:
        _restore_quarantined_exes(moved)
        raise ShimQuarantineError(failed)
    try:
        subprocess.run(cmd, cwd=root, check=True, env=env)
    finally:
        # Restore on success AND failure: a SUCCESSFUL install can skip the entry-points step
        # entirely (uv audits an already-satisfied editable install as a no-op), which would leave
        # the shims renamed aside and `hermes` gone from PATH. Restore only renames back when the
        # installer did NOT write a fresh shim, so this is safe in both cases.
        # See #75584.
        if scripts_dir is not None:
            _restore_quarantined_exes(moved)


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
    - quarantine live ``hermes*.exe`` shims on Windows so they can be replaced
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
