"""Post-``hermes update`` dependency sync: venv preflight, editable reinstall, lazy refresh,
npm installs, self-lock deferral. Names are re-imported by ``update_cmd`` (so
``hermes_cli.update_cmd.<name>`` resolves/monkeypatches); origin helpers are imported lazily."""

import logging
from contextlib import suppress
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional
from hermes_constants import project_venv_dir, venv_python_path
from hermes_cli._subprocess_compat import bounded_probe_run

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.update_cmd")

# Files defining the editable install; a pull touching none of them cannot invalidate it.
_INSTALL_DEFINING_FILES = "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "uv.lock"


def _editable_install_is_current(git_cmd, cwd, pre_pull_sha: str | None) -> bool:
    """True when the pulled commits cannot have invalidated the editable install: ``uv pip install
    -e .`` always rewrites console-script shims (Windows: ``hermes.exe`` quarantine, ``os error 32``
    on a lost race), so skip it when only non-install files changed. Safe because the editable
    finder uses a *static* module list. Fails closed: no pre-pull SHA or failed diff -> False."""
    if not pre_pull_sha:
        return False
    try:
        result = subprocess.run(
            git_cmd + ["diff", "--name-only", f"{pre_pull_sha}..HEAD", "--"] + list(_INSTALL_DEFINING_FILES),
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return False
    return result.returncode == 0 and not result.stdout.strip()


# Modules imported on every startup. Unlike _UPDATE_CRITICAL_FILES (only parsed) these are
# *imported*, catching cross-module breakage (a name pulled from a sibling no longer exists).
_UPDATE_CRITICAL_MODULES = "hermes_cli.main", "run_agent", "model_tools", "toolsets"


def _critical_module_import_failures(
    root, *, report_runtime_errors: bool = False) -> dict[str, tuple[str, str]]:
    """Import each ``_UPDATE_CRITICAL_MODULES`` entry in a subprocess; return failures in probe order.

    Syntax validation only *parses*: a partially-updated tree (Windows ZIP copy loop) parses yet
    dies with ``ImportError: cannot import name``. The subprocess (venv interpreter when present —
    the updater may run under another Python) keeps import side effects out of our ``sys.modules``.
    Generic import-time exceptions are tolerated unless ``report_runtime_errors=True``.
    """
    from hermes_cli.update_cmd import _UPDATE_CRITICAL_MODULES, _m
    from hermes_constants import FIRST_PARTY_MODULE_ROOTS
    import secrets
    marker = f"__HERMES_IMPORT_HEALTH_{secrets.token_hex(16)}__"
    probe = (
        "import importlib, json, sys\n"
        # Importing hermes_cli.main runs the startup dotenv load, which pulls external secret
        # sources (op/bws/command helpers, up to 120s each) unless argv says ``update``. The
        # probe only checks importability, so it inherits the updater's own argv contract.
        "sys.argv = ['hermes', 'update']\n"
        "failures = []\n"
        "for name in %r:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except ModuleNotFoundError as exc:\n"
        # A missing *third-party* module means deps aren't installed, not a skewed checkout;
        # only our own packages count. Roots come from hermes_constants so the user hint can't drift.
        "        missing = (getattr(exc, 'name', '') or '').split('.')[0]\n"
        "        if missing in %r or missing.startswith('hermes_') or %r:\n"
        "            failures.append((name, type(exc).__name__, str(exc)))\n"
        "    except ImportError as exc:\n"
        "        failures.append((name, type(exc).__name__, str(exc)))\n"
        "    except Exception as exc:\n"
        "        if %r:\n"
        "            failures.append((name, type(exc).__name__, str(exc)))\n"
        "    except BaseException as exc:\n"
        "        failures.append((name, type(exc).__name__, str(exc)))\n"
        "sys.stdout.write('\\n%s' + json.dumps(failures))\n"
        % (_UPDATE_CRITICAL_MODULES, tuple(sorted(FIRST_PARTY_MODULE_ROOTS)), report_runtime_errors,
           report_runtime_errors, marker))
    try:
        interpreter = sys.executable
        with suppress(Exception):
            from hermes_cli import runtime_paths
            venv_dir = runtime_paths.selected_venv(Path(root))
            venv_python = venv_python_path(venv_dir, windows=_m()._is_windows())
            if venv_python.exists():
                interpreter = str(venv_python)
        result = bounded_probe_run(
            [interpreter, "-c", probe], timeout=120, cwd=str(root), raise_on_spawn_failure=True,
        )
    except (OSError, subprocess.SubprocessError):
        # Keep this guard advisory: a probe we could not even spawn (unreadable venv
        # interpreter, fork failure) says nothing about the checkout, so it must not
        # fail an otherwise successful update. A spawned child that hangs does.
        return {}
    if result is None:
        return _probe_failure("TimeoutExpired", "timed out before reporting import health")
    output = result.stdout or ""
    if marker not in output:
        return _probe_failure(
            "ProbeTerminated",
            f"terminated before reporting import health (exit code {result.returncode})")
    try:
        failures = json.loads(output.rsplit(marker, 1)[1])
        if not isinstance(failures, list) or any(
            not isinstance(item, list) or len(item) != 3 or not all(isinstance(v, str) for v in item)
            for item in failures):
            raise ValueError("invalid import-health payload")
        return {str(module): (str(kind), str(detail)) for module, kind, detail in failures}
    except (TypeError, ValueError):
        return _probe_failure("MalformedPayload", "reported malformed import health data")


def _probe_failure(kind: str, detail: str) -> dict[str, tuple[str, str]]:
    """Failure row for the probe itself (as opposed to a module it imported)."""
    return {"critical-module probe": (kind, detail)}


def _validate_critical_modules_import(
    root, *, report_runtime_errors: bool = False) -> tuple[bool, str | None, str | None]:
    """Return the first critical-module import failure, if any."""
    failures = _critical_module_import_failures(root, report_runtime_errors=report_runtime_errors)
    if failures:
        module = next(iter(failures))
        return False, module, failures[module][1]
    return True, None, None


def _npm_bin_exists(bin_dir: Path, name: str) -> bool:
    """True when an npm bin shim for *name* exists (POSIX or Windows)."""
    return any((bin_dir / c).exists() for c in (name, f"{name}.cmd", f"{name}.ps1", f"{name}.exe"))


class PyprojectExtrasReadError(RuntimeError):
    """On-disk ``pyproject.toml`` optional-dependencies could not be read (fail closed)."""


def _declared_pyproject_extras(project_root: Path | None = None) -> frozenset[str]:
    """Optional-dependency keys from on-disk ``pyproject.toml`` (valid ``uv sync --extra`` names)."""
    from hermes_cli.update_cmd import _m
    import tomllib

    root = project_root or _m().PROJECT_ROOT
    pyproject = root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        extras = (data.get("project") or {}).get("optional-dependencies")
        if extras is None:
            raise PyprojectExtrasReadError(
                f"{pyproject}: missing [project.optional-dependencies]")
        return frozenset(str(name) for name in extras)
    except PyprojectExtrasReadError:
        raise
    except Exception as exc:
        raise PyprojectExtrasReadError(
            f"could not read optional-dependencies from {pyproject}: {exc}") from exc


def _pm_sync_extras_for_lazy_features(
    active_lazy_features: list[str] | None,
    *,
    base_extra: str,
    include_base: bool,
) -> list[str]:
    """Extras list for ``pm.sync_venv``: base profile plus any captured IDs that ARE pyproject extras.

    Lazy backends use namespaced feature IDs (:data:`tools.lazy_deps.LAZY_DEPS`); uv only accepts
    declared optional-extra names. Non-matching IDs stay on the lazy restoration path unchanged."""
    declared = _declared_pyproject_extras()
    lazy = list(active_lazy_features or [])
    from_features = sorted({feature_id for feature_id in lazy if feature_id in declared})
    if include_base:
        return [base_extra] + from_features
    return from_features


def _capture_active_lazy_features() -> list[str]:
    """Snapshot active lazy backends before a managed runtime is replaced."""
    try:
        from pm.ensure import enabled_extras
        return enabled_extras()
    except Exception as exc:
        logger.debug("Could not snapshot active lazy features: %s", exc)
        return []


def _capture_active_tool_dependencies() -> list[str]:
    """Snapshot Python dependencies installed explicitly through ``hermes tools``."""
    try:
        from hermes_cli import tools_config
        return tools_config.active_restorable_python_tool_dependencies()
    except Exception as exc:
        logger.debug("Could not snapshot active Hermes Tools dependencies: %s", exc)
        return []




def _refresh_active_lazy_features(features: list[str] | None = None) -> bool:
    """Re-sync the venv's enabled extras against the (possibly new) uv.lock.

    Extras live in the installed-state file and uv.lock owns every pin, so
    a post-update refresh is one sync_venv() call: it re-installs exactly
    the locked versions of everything enabled. Never raises.
    """
    try:
        from pm.ensure import sync_venv

        sync_venv(features, explicit=True)
        return True
    except Exception as exc:
        print(f"  ⚠ Extra re-sync failed: {exc}")
        print("  Rerun `hermes update` (or `hermes pm install`) once resolved.")
        return False


def _refresh_active_memory_provider_dependencies() -> None:
    """Refresh pip deps for the configured external memory provider: its bridge packages live in
    ``plugin.yaml`` (not Hermes extras / ``LAZY_DEPS``), so the core reinstall can strip them;
    re-run the ACTIVE provider's install last so its writes land last. Never raises.

    Re-run the provider's declared install for the ACTIVE provider only, after the core install and lazy
    refresh, so the last write to any shared package is the one the active provider needs. See #53272,
    #70636.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.debug("Memory provider refresh skipped (config load failed): %s", exc)
        return

    provider = ""
    memory_cfg = cfg.get("memory") if isinstance(cfg, dict) else None
    if isinstance(memory_cfg, dict):
        if memory_cfg.get("enabled") is False:
            return
        provider = str(memory_cfg.get("provider") or "").strip()

    # "default"/empty is the built-in file store — no pip deps.
    if not provider or provider in {"default", "builtin", "none"}:
        return

    try:
        from hermes_cli.memory_setup import _install_dependencies
    except Exception as exc:
        logger.debug("Memory provider refresh skipped (import failed): %s", exc)
        return

    print()
    print(f"→ Refreshing active memory provider dependencies ({provider})...")

    try:
        _install_dependencies(provider, force=True)
    except Exception as exc:
        print(f"  ⚠ {provider} dependencies failed to refresh: {exc}")


def _reapply_plugin_python_dependencies() -> None:
    """Re-install every enabled user plugin's declared Python deps after the venv was rebuilt (a
    ``uv sync``/reinstall strips anything Hermes' own lock does not know). Non-memory plugins whose
    deps no longer resolve are disabled loudly, memory providers last. Never raises."""
    from hermes_cli.plugin_python_deps import reapply_all
    from hermes_cli.update_cmd import _m

    def _disable(home, name: str) -> None:
        # Write through the real config writer (comments/defaults preserved), scoped to *home*.
        from hermes_cli.config import load_config, save_config
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            config = load_config()
            plugins = config.setdefault("plugins", {})
            plugins["enabled"] = sorted(set(plugins.get("enabled") or []) - {name})
            plugins["disabled"] = sorted(set(plugins.get("disabled") or []) | {name})
            save_config(config, merge_existing=True)
        finally:
            reset_hermes_home_override(token)

    try:
        report = reapply_all(project_root=_m().PROJECT_ROOT, disable=_disable)
    except Exception as exc:  # the update must finish even if the plugin step blows up
        print(f"  ⚠ Plugin Python dependencies not re-applied: {exc}")
        return
    if report.installed:
        print(f"  ✓ Plugin Python dependencies re-applied: {', '.join(report.installed)}")
    for name, reason in report.dropped:
        print(f"  ✗ Plugin '{name}' DISABLED: {reason}. Fix the plugin's declared dependencies, "
              f"then `hermes plugins enable {name}`.")
    if report.failed:
        print(f"  ⚠ Plugin Python dependencies not re-applied: {report.failed}")
    _migrate_removed_memory_providers()


def _migrate_removed_memory_providers() -> None:
    """A configured memory provider that no longer ships in core is installed from the catalog, for
    every profile home sharing this venv (its config section, data and tool names are unchanged)."""
    try:
        from hermes_cli.memory_provider_migration import migrate_all_homes
        migrate_all_homes()
    except Exception as exc:  # the update must finish even if the migration step blows up
        print(f"  ⚠ Memory provider migration skipped: {exc}")


def _npm_manifest_paths() -> tuple[Path, ...]:
    """Manifests whose changes must defeat the update-skip. The lockfile alone isn't enough (a
    package.json can be edited without running npm); workspaces come from the root ``workspaces``
    globs so a new one can't escape the key, and every workspace counts because the
    single lockfile spans the whole graph. Root manifests only if package.json is unreadable."""
    from hermes_cli.update_cmd import _m
    root_pkg = _m().PROJECT_ROOT / "package.json"
    paths = [_m().PROJECT_ROOT / "package-lock.json", root_pkg]
    with suppress(OSError, json.JSONDecodeError, TypeError):
        workspaces = json.loads(root_pkg.read_text(encoding="utf-8-sig")).get("workspaces", [])
        if isinstance(workspaces, dict):  # legacy {"packages": [...]} form
            workspaces = workspaces.get("packages", [])
        for pattern in workspaces:
            for match in sorted(_m().PROJECT_ROOT.glob(str(pattern))):
                manifest = match / "package.json"
                if manifest.is_file():
                    paths.append(manifest)
    return tuple(paths)


def _npm_manifests_digest() -> str | None:
    """sha256 over lockfile + all workspace package.json; None when the lockfile is missing (never skip)."""
    from hermes_cli.update_cmd import _m
    if not (_m().PROJECT_ROOT / "package-lock.json").exists():
        return None
    h = hashlib.sha256()
    for p in _npm_manifest_paths():
        h.update(str(p.relative_to(_m().PROJECT_ROOT)).encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()


def _npm_lockfile_changed(hermes_root: Path) -> bool:
    from hermes_cli.update_cmd import _m
    current = _npm_manifests_digest()
    if current is None:
        return True
    # Matching hash but no node_modules: cache was recorded by another checkout.
    if not (_m().PROJECT_ROOT / "node_modules").is_dir():
        return True
    try:
        cache_file = _npm_lock_cache_file(hermes_root)
        if not cache_file.exists():
            return True
        return cache_file.read_text(encoding="utf-8-sig").strip() != current
    except OSError:
        return True


def _npm_lock_cache_file(hermes_root: Path) -> Path:
    """Per-checkout cache path: keyed by PROJECT_ROOT so parallel worktrees don't collide."""
    from hermes_cli.update_cmd import _m
    cache_key = hashlib.sha256(str(_m().PROJECT_ROOT).encode()).hexdigest()[:12]
    return hermes_root / f".npm_lock_hash_{cache_key}"


def _record_npm_lockfile_hash(hermes_root: Path) -> None:
    digest = _npm_manifests_digest()
    if digest is None:
        return
    try:
        _npm_lock_cache_file(hermes_root).write_text(digest, encoding="utf-8")
    except OSError:
        logger.debug("Could not write npm lockfile hash cache")


def _repair_node_deps_on_current_checkout(
    print_completion,
    *,
    assume_yes: bool = False,
    gateway_mode: bool = False,
    pre_update_snapshot_id: str | None = None,
    completion_message: str = "✓ Already up to date!") -> bool:
    """Repair Node deps on the ``commit_count == 0`` path: a failed npm install says "re-run hermes
    update" but the early return used to skip the refresh. ``_update_node_dependencies`` self-gates
    on the hash recorded only after a SUCCESSFUL install, so this is a cheap no-op when healthy.

    See #77211.
    """
    from hermes_cli.update_cmd import _check_and_apply_config_migration, _m, _update_node_dependencies
    node_failures = _update_node_dependencies()
    if node_failures:
        print(f"  ⚠ Node.js refresh failed for: {', '.join(node_failures)}")
        print("    Fix npm and re-run `hermes update`.")
        print_completion("⚠ Checkout is current, but Node.js dependencies could not be repaired.")
        return False
    _check_and_apply_config_migration(
        assume_yes=assume_yes, gateway_mode=gateway_mode, pre_update_snapshot_id=pre_update_snapshot_id)
    return bool(print_completion(completion_message))


def _update_node_dependencies() -> list[str]:
    """Refresh Node deps for ui-tui. Returns labels whose npm install failed (empty on
    success) so the caller reports a partial update instead of ``Update complete!``.

    See #30271.
    """
    from hermes_cli.update_cmd import _m
    if not (_m().PROJECT_ROOT / "package.json").exists():
        return []

    npm = _m()._resolve_node_runtime_npm()
    if not npm:
        # Only a Windows npm reachable from WSL: flag loudly — skipping silently leaves
        # deps stale, running it would corrupt the tree.
        from hermes_constants import is_wsl
        path_npm = shutil.which("npm")
        if is_wsl() and path_npm and _m()._is_windows_npm_path(path_npm):
            # Name only the workspace the CLI/TUI build actually requires.
            print("→ Updating Node.js dependencies...")
            print("  ⚠ Skipped: only a Windows npm is reachable from this WSL shell.")
            print("    Install Node.js inside the WSL distro (nvm, or your distro's")
            print("    package manager), then re-run `hermes update`.")
            has_workspace = any(
                (_m().PROJECT_ROOT / ws / "package.json").exists() for ws in ("ui-tui",))
            return ["ui-tui workspace"] if has_workspace else []
        return []

    from hermes_constants import get_default_hermes_root
    # node_modules is shared by every profile on this checkout: one per-checkout cache.
    shared_hermes_root = get_default_hermes_root()

    # Best-effort npx cache warm before the lockfile-unchanged early return. Can block
    # ~11s on a cold cache — print first so it doesn't look like a hang.
    # Runs before the lockfile-unchanged early return below since that's the common `hermes update` case.
    # See #43564.
    print("→ Warming npx cache for agent-browser...")
    with suppress(Exception):
        from tools.browser_tool_install import warm_agent_browser_npx_cache
        warm_agent_browser_npx_cache()

    if not _m()._npm_lockfile_changed(shared_hermes_root):
        logger.info("npm lockfile unchanged, skipping npm install")
        return []

    # Root package.json has no deps of its own, so a workspace-scoped install prunes nothing
    # root-only.
    print("→ Updating Node.js dependencies...")
    install_args = [
        "--no-fund", "--no-audit", "--prefer-offline", "--progress=false",
        "--workspace", "ui-tui",
        # Root devDependencies (shared ESLint config) would otherwise be pruned by the
        # scoped install.
        "--include-workspace-root"]

    from hermes_constants import with_hermes_node_path
    nixos_env = with_hermes_node_path(_m()._nixos_build_env())

    # capture_output=False is deliberate: postinstall scripts print download progress and
    # capturing makes a long download look hung.
    result = _m()._run_npm_install_deterministic(
        npm, _m().PROJECT_ROOT, extra_args=tuple(install_args), capture_output=False, env=nixos_env)
    if result.returncode == 0:
        _record_npm_lockfile_hash(shared_hermes_root)
        print("  ✓ ui-tui workspace installed")
        return []
    print("  ⚠ npm install failed")
    stderr = (result.stderr or "").strip()
    if stderr:
        print(f"    {stderr.splitlines()[-1]}")
    print()
    print("  ⚠ Node.js dependency refresh did not complete cleanly; the")
    print("    installation may be in a mixed state (updated code, stale Node")
    print("    deps). Fix npm and re-run `hermes update`.")
    return ["ui-tui workspace"]


def _venv_core_imports_healthy() -> tuple[bool, str]:
    """Probe the SELECTED dependency environment (in ITS interpreter — the updater may run under
    another Python) for core imports, catching a half-updated environment that "Already up to
    date!" would otherwise never re-sync.

    Selection goes through ``hermes_cli.runtime_paths`` — the stdlib-only selection module — not
    through the pm manager: the probe must run before any dependency of that environment has been
    imported. The legacy ``<repo>/venv`` target is superseded: the PM model stages a fresh
    generation under ``installs/<key>/environments/<gen>`` and commits the selection in
    ``facts.json`` (``pm.packages.VenvPackage.apply``), so the environment the update must judge
    (and the one the repair below rebuilds) is the SELECTED one, never a hardcoded repo-relative
    ``venv`` directory.

    Returns ``(healthy, detail)``; never raises, unknown states report healthy."""
    from hermes_cli.update_cmd import _m
    from hermes_cli import runtime_paths
    try:
        venv_dir = runtime_paths.selected_venv(_m().PROJECT_ROOT)
    except FileNotFoundError:
        venv_dir = runtime_paths.base_venv(_m().PROJECT_ROOT)
    except RuntimeError as exc:
        # An unreadable/invalid committed selection is a KNOWN-unhealthy state (the repair
        # flow below re-syncs and re-commits); never raise.
        return False, str(exc)
    venv_python = venv_python_path(venv_dir, windows=_m()._is_windows())
    if not venv_python.exists():
        # No venv: normal for a dev checkout (healthy), but on a MANAGED install (bootstrap
        # stamp or `.update-incomplete`) the venv IS the install — absence means an interrupted repair.
        managed_markers = (_m().PROJECT_ROOT / ".hermes-bootstrap-complete", _m()._update_marker_path())
        if any(m.exists() for m in managed_markers):
            return False, f"venv python missing ({venv_python})"
        return True, ""

    # Import (not just metadata): dist-info can be intact with modules missing after an
    # interrupted uninstall/install.
    check = (
        "import importlib\n"
        "mods = ['uvicorn', 'pydantic', 'openai', 'yaml']\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try: importlib.import_module(m)\n"
        "    except Exception as e: missing.append(f'{m}: {e}')\n"
        "print('\\n'.join(missing))\n")
    try:
        result = subprocess.run(
            [str(venv_python), "-c", check], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, cwd=_m().PROJECT_ROOT)
    except Exception as exc:
        logger.debug("venv health probe failed to run: %s", exc)
        return True, ""

    missing = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0 and not missing:
        # Interpreter itself is broken — that IS unhealthy.
        detail = (result.stderr or "").strip().splitlines()
        return False, detail[0] if detail else "venv python failed to run"
    if missing:
        return False, "; ".join(missing[:4])
    return True, ""


def _venv_dependency_set_stale() -> tuple[bool, str]:
    """Whether the venv's ``hermes-agent`` distribution was installed from an OLDER checkout than
    the one on disk. The sync after a pull can be skipped (Windows hand-off child refused because
    the serve backend held the venv, or died mid-install); the next run then finds git current,
    passes the import probe — the old release imports fine — and prints "Already up to date!" over
    stale pins (#97208). Probed in the venv's own interpreter; ``(stale, detail)``, unknown = not stale."""
    from hermes_cli.update_cmd import _m, _read_project_version
    expected = _read_project_version()
    venv_dir = project_venv_dir(_m().PROJECT_ROOT) or _m().PROJECT_ROOT / "venv"
    venv_python = venv_python_path(venv_dir, windows=_m()._is_windows())
    if not expected or not venv_python.exists():
        return False, ""
    try:
        result = subprocess.run(
            [str(venv_python), "-c", "import importlib.metadata as m; print(m.version('hermes-agent'))"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            cwd=_m().PROJECT_ROOT)
    except Exception as exc:
        logger.debug("installed-version probe failed to run: %s", exc)
        return False, ""
    installed = (result.stdout or "").strip()
    if result.returncode != 0 or not installed:
        return False, ""  # not installed as a distribution: nothing to compare against
    try:
        from packaging.version import Version
        stale = Version(installed) != Version(expected)
    except Exception:
        stale = installed != expected
    return stale, f"installed hermes-agent {installed}, checkout is {expected}" if stale else ""


def _path_uid(path) -> Optional[int]:
    """Owner uid of ``path`` (``None`` when unreadable). Separate seam so tests can simulate
    root-owned files without chown. Never raises."""
    try:
        return os.stat(path, follow_symlinks=False).st_uid
    except OSError:
        return None


def _venv_foreign_owned_paths(venv_root, limit: int = 5) -> list:
    """Up to ``limit`` ``(path_str, uid)`` venv entries not owned by the current user.

    A venv touched by ``sudo pip``/``sudo hermes`` dies mid-update with ``venv/bin/hermes`` already
    deleted — never mutate a venv we can't safely mutate. Deliberately BOUNDED (venv root,
    ``venv/bin``, first site-packages top level, ``*.dist-info`` children; ~2000 stats). POSIX-only:
    ``[]`` on Windows and as root; ``[]`` on any surprise — must NEVER raise or add latency.

    See #83529.
    A later normal ``hermes update`` then dies mid-mutation inside ``uv pip install -e .`` ("Permission
    denied (os error 13)") with ``venv/bin/hermes`` already deleted — the CLI is bricked. Same philosophy as
    the contended-venv gate (#87331): a venv we cannot safely mutate is never mutated at all.
    """
    from hermes_cli.update_cmd import _path_uid
    try:
        if not hasattr(os, "geteuid"):
            return []  # windows-footgun: ok — POSIX ownership concept only
        euid = os.geteuid()  # windows-footgun: ok — guarded by hasattr above
        if euid == 0:
            return []  # root can rewrite anything; nothing to refuse

        venv_root = Path(venv_root)
        budget = 2000  # max stat() calls — hard bound on preflight cost
        foreign: list = []

        def _check(p) -> bool:
            """stat one path; True while scan should continue."""
            nonlocal budget
            if budget <= 0 or len(foreign) >= limit:
                return False
            budget -= 1
            uid = _path_uid(p)
            if uid is not None and uid != euid:
                foreign.append((str(p), uid))
            return budget > 0 and len(foreign) < limit

        def _entries(d) -> list:
            try:
                return list(os.scandir(d))
            except OSError:
                return []

        def _scan_dir(d, recurse_dist_info: bool = False) -> None:
            for entry in _entries(d):
                if not _check(entry.path):
                    return
                if recurse_dist_info and entry.name.endswith(".dist-info"):
                    for child in _entries(entry.path):
                        if not _check(child.path):
                            return

        if not _check(venv_root):
            return foreign[:limit]
        _scan_dir(venv_root / "bin")

        # First lib/python*/site-packages (POSIX venv layout).
        site_packages = next(iter(sorted(venv_root.glob("lib/python*/site-packages"))), None)
        if site_packages is not None:
            _scan_dir(site_packages, recurse_dist_info=True)

        return foreign[:limit]
    except Exception:
        # Advisory preflight: structural surprise = "no verdict", never a blocked update.
        return []


def _refuse_update_if_venv_foreign_owned(project_root) -> None:
    """Refuse-before-mutate ownership gate, run after the pull and before the first venv mutation:
    foreign-owned files would brick the install mid-mutation, so refuse with the recovery command
    while the venv is intact. No subprocess calls — tests mock ``subprocess.run`` with sequenced effects.

    See #83529.
    """
    from hermes_cli import runtime_paths
    foreign = _venv_foreign_owned_paths(runtime_paths.selected_venv(Path(project_root)))
    if not foreign:
        return
    print("\n✗ Update stopped: this install's venv contains files owned by another user.")
    print("  Updating now would fail midway (Permission denied) and leave Hermes broken.")
    print("  This usually happens after running hermes or pip with sudo. Offending paths:")
    for p, uid in foreign:
        print(f"    - {p} (owner uid {uid})")
    print("\n  Fix ownership, then re-run the update:")
    print(f"    sudo chown -R $(id -un): {project_root}")
    print("    hermes update")
    print("\n  Nothing in the venv was modified.")
    sys.exit(1)


def _sync_python_dependencies_after_pull(
    git_cmd, branch, pre_pull_sha, *, active_lazy_features, active_tool_dependencies,
    _windows_gateway_resume, deferred_defects=None):
    """Reinstall Python deps for the pulled checkout in the post-swap interpreter.
    Ownership preflight and the transactional pm sync run before the bytecode
    sweep and feature/plugin repair; deferred defects are reported by the caller."""
    from hermes_cli.update_cmd import (
        _m, _sweep_bytecode_after_update, _validate_critical_modules_import,
        _write_lazy_refresh_incomplete_marker, _write_update_incomplete_marker)
    _refuse_update_if_venv_foreign_owned(_m().PROJECT_ROOT)
    _write_update_incomplete_marker()
    print("→ Syncing Python dependencies...")
    import pm

    try:
        pm.sync_venv(["all"], explicit=True)
    except pm.InstallError as _sync_err:
        print(f"  ✗ {_sync_err}")
        print("  Re-run `hermes update` (or `hermes pm install`) once resolved.")
        raise

    # Core install finished; lazy/feature repair carries its own breadcrumb.
    _m()._clear_update_incomplete_marker()
    _sweep_bytecode_after_update(branch)

    _write_lazy_refresh_incomplete_marker()
    lazy_ok = _m()._refresh_active_lazy_features(active_lazy_features)
    if lazy_ok:
        _m()._clear_lazy_refresh_incomplete_marker()
    else:
        print(
            "  ⚠ Lazy-refresh recovery incomplete — run `hermes` again "
            "to finish import-based venv repair."
        )
        if deferred_defects is not None:
            deferred_defects.append("lazy-refresh recovery incomplete")

    # Heal the active memory provider's bridge packages last — the core
    # reinstall + lazy refresh above may have stripped or downgraded
    # plugin.yaml-declared deps that aren't in extras (#53272, #70636).
    _m()._refresh_active_memory_provider_dependencies()
    _m()._reapply_plugin_python_dependencies()

    # Everything that can legitimately produce a transient ImportError has
    # now run (bytecode sweep, dependency reinstall, lazy refresh), so a
    # module that still won't import is real breakage. Warn only — never
    # roll back here: `cannot import name X` is also the signature of the
    # stale-bytecode class (#6207, #60242), and the launch-time sweep in
    # _sweep_stale_bytecode_if_checkout_changed() self-heals that on the
    # next run. A destructive reset would undo a good update over a state
    # that fixes itself.
    import_ok, failing_module, import_error = _validate_critical_modules_import(
        _m().PROJECT_ROOT
    )
    if not import_ok:
        print()
        print(f"  ⚠ {failing_module} still fails to import after updating:")
        print(f"      {import_error}")
        if deferred_defects is not None:
            deferred_defects.append(f"critical import validation failed: {failing_module}")
        print("    Run `hermes update` again — if it persists, reinstall:")
        print("    https://hermes-agent.nousresearch.com")

