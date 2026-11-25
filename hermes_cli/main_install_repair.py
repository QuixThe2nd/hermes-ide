"""Install/update recovery: interrupted-install markers and lazy-refresh repair.

Split out of ``hermes_cli/main.py``. Names that still live in main (``PROJECT_ROOT``, ...)
are imported lazily inside the functions that use them (avoids an import cycle).
"""

import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time as _time

from pathlib import Path
from hermes_cli import _early_recovery as _early_recovery_mod

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.main")


def _pyproject_project(debug_fmt: str | None = None) -> dict | None:
    """``[project]`` table of pyproject.toml, or ``None`` when absent/unreadable."""
    from hermes_cli.main import PROJECT_ROOT
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
        with pyproject.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except Exception as exc:
        if debug_fmt:
            logger.debug(debug_fmt, exc)
        return None
    return project if isinstance(project, dict) else None


def _naive_requirement(spec: str) -> tuple[str, str]:
    """``(name, head)`` of a ``name OP version ; marker`` spec without ``packaging``."""
    head = spec.split(";", 1)[0].strip()
    bare = head
    for op in ("==", ">=", "<=", "~=", ">", "<", "!="):
        if op in bare:
            bare = bare.split(op, 1)[0]
            break
    return bare.strip().split("[", 1)[0].strip(), head


def _parse_requirements(raw_deps: list[str]) -> list[tuple[str, "object | None", str]]:
    """``(name, marker, head)`` per dep spec — ``packaging`` when importable, else a naive split."""
    parsed: list[tuple[str, "object | None", str]] = []
    try:
        from packaging.requirements import Requirement  # type: ignore
        for spec in raw_deps:
            try:
                req = Requirement(spec)
            except Exception:
                continue
            parsed.append((req.name, req.marker, spec.split(";", 1)[0].strip()))
    except Exception:
        for spec in raw_deps:
            name, head = _naive_requirement(spec)
            if name:
                parsed.append((name, None, head))
    return parsed


def _load_installable_optional_extras(group: str = "all") -> list[str]:
    """Return optional extras referenced by a dependency group (``all`` or ``termux-all``)."""
    optional_deps = (_pyproject_project() or {}).get("optional-dependencies", {})
    if not isinstance(optional_deps, dict):
        return []
    referenced: list[str] = []
    for ref in optional_deps.get(group, []):
        if "[" in ref and "]" in ref:
            name = ref.split("[", 1)[1].split("]", 1)[0]
            if name in optional_deps:
                referenced.append(name)
    return referenced


# Install-scoped breadcrumbs live next to the venv (not under $HERMES_HOME)
# because the venv is shared across profiles.
#   ``.update-incomplete``       — generic core ``.[all]`` install was interrupted;
#     cleared only after a confirmed full dependency reinstall/recovery.
#   ``.lazy-refresh-incomplete`` — lazy-backend refresh may have corrupted packages;
#     cleared only after import-probe repair confirms healthy (never on indeterminate).
# Narrow lazy probes must NEVER clear the generic core marker.
# See #58004.
def _update_marker_path() -> Path:
    from hermes_cli.main import PROJECT_ROOT
    return PROJECT_ROOT / ".update-incomplete"


def _lazy_refresh_marker_path() -> Path:
    from hermes_cli.main import PROJECT_ROOT
    return PROJECT_ROOT / ".lazy-refresh-incomplete"


def _pytest_owns_live_checkout(root: Path) -> bool:
    """True under pytest when ``root`` is this checkout: unsandboxed update/recovery tests must
    neither litter the live repo root with breadcrumbs (false-arming the developer's next launch)
    nor run a real reinstall against the executing venv (cf. ``managed_scope._under_pytest``)."""
    return "PYTEST_CURRENT_TEST" in os.environ and root == Path(__file__).resolve().parent.parent


def _clear_marker_file(path: Path, *, label: str) -> None:
    """Remove an update-recovery breadcrumb. Never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not clear %s marker: %s", label, exc)


def _clear_update_incomplete_marker() -> None:
    """Remove the interrupted core-install breadcrumb. Never raises."""
    _clear_marker_file(_update_marker_path(), label="update-incomplete")


def _clear_lazy_refresh_incomplete_marker() -> None:
    """Remove the interrupted lazy-refresh breadcrumb. Never raises."""
    _clear_marker_file(_lazy_refresh_marker_path(), label="lazy-refresh-incomplete")


def _claim_recovery_lock(lock_path: Path) -> bool:
    """Atomically claim the single-flight recovery lock; False when another process holds it.

    A crashed holder's stale lock is broken after an hour (well past any realistic install).
    Failing to CREATE the lock (read-only fs, perms) proceeds unlocked — the install itself
    will surface the real problem."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
    except FileExistsError:
        try:
            if _time.time() - lock_path.stat().st_mtime > 3600:
                lock_path.unlink()
        except OSError:
            pass
        return False
    except OSError as exc:
        logger.debug("Could not create install-recovery lock: %s", exc)
    return True


@contextlib.contextmanager
def _stdout_to_stderr():
    """Route Python prints AND the fd 1 that pip/uv inherit to stderr: launches whose stdout is
    a protocol stream (``hermes acp`` speaks JSON-RPC on stdout) must never get install noise."""
    saved_stdout_fd = None
    saved_sys_stdout = sys.stdout
    try:
        saved_stdout_fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        saved_stdout_fd = None
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved_sys_stdout
        if saved_stdout_fd is not None:
            try:
                os.dup2(saved_stdout_fd, 1)
                os.close(saved_stdout_fd)
            except OSError:
                pass


def _recover_from_interrupted_install() -> None:
    """Finish update work left half-done by a prior ``hermes update``.

    ``.update-incomplete`` recovers via full reinstall; ``.lazy-refresh-incomplete``
    via package-only import probes (cleared only when probes confirm healthy/repaired). Never
    raises: on failure it prints the manual command and leaves the marker for the next launch.
    Concurrent launches race on the shared venv, so an ``O_EXCL`` lockfile lets one process
    recover while the others skip.
    """
    from hermes_cli.main import PROJECT_ROOT
    if _pytest_owns_live_checkout(PROJECT_ROOT):
        return
    lazy_marker = _lazy_refresh_marker_path().exists()
    if not lazy_marker and not _update_marker_path().exists():
        return
    # Managed/Docker installs and git-less PyPI installs never run the source-tree
    # update path, so a stray marker is not ours to act on. Just clear it.
    if not (PROJECT_ROOT / "pyproject.toml").is_file():
        _clear_update_incomplete_marker()
        _clear_lazy_refresh_incomplete_marker()
        return
    lock_path = PROJECT_ROOT / ".update-incomplete.lock"
    if not _claim_recovery_lock(lock_path):
        return
    try:
        with _stdout_to_stderr():
            if lazy_marker:
                _recover_lazy_refresh_marker_locked()
            if _update_marker_path().exists():
                _recover_core_update_marker_locked()
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _recover_lazy_refresh_marker_locked() -> None:
    """Heal ``.lazy-refresh-incomplete`` via confirmed import-probe repair."""
    print(
        "⚠ A previous lazy-backend refresh may have left the venv unhealthy — "
        "running import-based package repair...")
    install_prefix, install_env = _default_venv_install_target()
    status = _repair_venv_via_import_probes(install_prefix, env=install_env)
    if status in ("healthy", "repaired"):
        _clear_lazy_refresh_incomplete_marker()
        print("✓ Lazy-refresh venv recovery confirmed — install is healthy again.")
        return
    indeterminate = status == "indeterminate"
    problem = (
        "Import probes unavailable — cannot confirm venv health." if indeterminate
        else "Lazy-refresh package repair incomplete.")
    print(f"  ⚠ {problem} Leaving `.lazy-refresh-incomplete` for the next launch.")
    if indeterminate:
        return
    print("  Recover manually with:")
    all_specs = _lazy_refresh_repair_specs(sorted(set(_LAZY_REFRESH_REPAIR_PACKAGES.values())))
    print(
        f"    {' '.join(install_prefix)} install --force-reinstall "
        + " ".join(shlex.quote(s) for s in all_specs))


def _recover_core_update_marker_locked() -> None:
    """Heal ``.update-incomplete`` via full ``.[all]`` reinstall only.

    Narrow lazy-refresh import probes are not proof that a generic interrupted core
    install finished — a missing dep outside that probe set would look healthy and
    clear the breadcrumb too early.
    """
    from hermes_cli.main import PROJECT_ROOT
    print(
        "⚠ A previous `hermes update` was interrupted mid-install — "
        "finishing dependency installation now...")

    # Package-only import repair may help as first aid but must NEVER clear this core
    # marker on its own (#58004 review) — only the full reinstall below can.
    try:
        from hermes_cli import _install_repair as _ir

        # The early stdlib-only pass cannot restore a missing uv; PM can.
        from pm.ensure import uv as pm_uv

        pm_uv(realize=True)
        # Shared stdlib executor: late path and pre-import early pass run exactly the same
        # reinstall. Its own stdout→stderr redirect nests harmlessly inside ours.
        _ir.run_core_install(PROJECT_ROOT)
        _clear_update_incomplete_marker()
        print("✓ Dependency installation recovered — your install is healthy again.")
    except Exception as exc:
        # Leave the marker so the next launch retries; give the exact manual command.
        logger.debug("Interrupted-install recovery failed: %s", exc)
        print("✗ Could not auto-recover the interrupted install.")
        for line in (
            "  Recover manually with:",
            f"    cd {PROJECT_ROOT}",
            f"    {sys.executable} -m ensurepip --upgrade",
            f"    {sys.executable} -m pip install -e '.[all]'",
        ):
            print(line)




def _default_venv_install_target() -> tuple[list[str], dict[str, str] | None]:
    """Return ``(install_cmd_prefix, env)`` for the project venv when possible."""
    from hermes_cli.main import PROJECT_ROOT
    try:
        from pm.ensure import uv as pm_uv

        uv_bin, _ = pm_uv(realize=True)
    except Exception:
        uv_bin = None
    if uv_bin:
        from hermes_constants import project_venv_dir
        venv_dir = project_venv_dir(PROJECT_ROOT) or PROJECT_ROOT / "venv"
        env = {**os.environ, "VIRTUAL_ENV": str(venv_dir)}
        if _is_termux_env(env):
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
        return [uv_bin, "pip"], env
    return [sys.executable, "-m", "pip"], None


def _run_install_with_heartbeat(
    cmd: list[str], *, env: dict[str, str] | None = None, heartbeat_interval_seconds: int = 30
) -> None:
    """Run a dependency install, printing an elapsed-time heartbeat while pip/uv is silent.

    Resolvers/build backends compiling Rust/C extensions can stay quiet for minutes.
    """
    from hermes_cli.main import PROJECT_ROOT
    done = threading.Event()
    start = _time.time()

    def _heartbeat() -> None:
        # Wait first, then print, so short installs don't emit noise.
        while not done.wait(heartbeat_interval_seconds):
            elapsed = int(_time.time() - start)
            print(
                f"  … still installing dependencies ({elapsed}s elapsed)"
                " — compiling Rust/C extensions can take several minutes",
                flush=True)

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        # stderr=STDOUT: uv/pip write progress to stderr. Legacy desktop
        # hand-offs only drain the child's stdout, so a full stderr
        # pipe (~64KB) blocks the installer forever. Merged into stdout,
        # the output rides the pipe old hand-offs DO drain. This module
        # is imported when `hermes update` starts, so an update running
        # from an old base executes the old copy regardless of the git
        # reset — this protects updates initiated from bases that ship
        # it. (managed_uv.py gets the same fix AND is imported lazily
        # after the reset, so its sync is protected even on old bases.)
        subprocess.run(cmd, cwd=PROJECT_ROOT, check=True, env=env, stderr=subprocess.STDOUT)
    finally:
        done.set()
        t.join(timeout=0.2)


def _run_repair_step(run, cmd: list[str], *, log_msg: str, fail_msg: str | None, **kwargs) -> bool:
    """``run(cmd, **kwargs)``; on ``CalledProcessError`` log + print the failure and return False."""
    try:
        run(cmd, **kwargs)
    except subprocess.CalledProcessError as e:
        logger.warning(log_msg, e)
        if fail_msg is not None:
            print(fail_msg)
        return False
    return True


def _report_still_missing(missing: list[str], hint: str, *, ok: str) -> None:
    if missing:
        print(f"  ⚠ Still missing after repair: {', '.join(missing)}. {hint}")
    else:
        print(ok)



# Import probes for venv corruption after a failed lazy ``uv pip install`` (metadata can
# look fine while ``.py`` files were removed mid-install). Canonical tables live in the
# stdlib-only ``_early_recovery`` module so the early and full recovery layers never drift.
# See #57828.
_LAZY_REFRESH_IMPORT_PROBES: tuple[tuple[str, str], ...] = (
    _early_recovery_mod.LAZY_REFRESH_IMPORT_PROBES)
_LAZY_REFRESH_REPAIR_PACKAGES: dict[str, str] = _early_recovery_mod.LAZY_REFRESH_REPAIR_PACKAGES


def _run_package_only_install(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Package-only pip/uv install — no shim quarantine: ``--force-reinstall <pkg>`` never
    rewrites the ``hermes`` launcher, and the quarantine path would rename shims uv then
    never recreates.

    See #57828.
    """
    _run_install_with_heartbeat(cmd, env=env)


def _lazy_refresh_repair_specs(packages: list[str]) -> list[str]:
    """Map repair package names to their declared pin specs in pyproject.toml."""
    project = _pyproject_project("lazy refresh repair spec lookup failed: %s")
    if project is None:
        return packages
    name_to_spec = {
        name.lower(): head
        for name, _, head in _parse_requirements(project.get("dependencies", []) or [])}
    return [name_to_spec.get(pkg.lower(), pkg) for pkg in packages]


def _venv_probe(venv_python: Path, script: str, *args: str, env: dict[str, str] | None):
    """Run ``script`` in the target venv's interpreter, capturing UTF-8 stdout."""
    return subprocess.run(
        [str(venv_python), "-c", script, *args],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
        env=env)


def _nonblank_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _detect_broken_lazy_refresh_imports(
    install_cmd_prefix: list[str], *, env: dict[str, str] | None = None) -> list[str] | None:
    """Probe lazy-refresh packages via real imports: ``[]`` all clean, ``[dist, ...]`` failures,
    ``None`` when the probe could not run (no venv Python, subprocess failure, non-zero exit)
    — *indeterminate*, not healthy."""
    venv_python = _resolve_install_target_python(install_cmd_prefix, env)
    if venv_python is None:
        return None
    probe_lines = "\n".join(
        f"    ({mod!r}, {attr!r})," for mod, attr in _LAZY_REFRESH_IMPORT_PROBES)
    check_script = (
        "import os\n"
        "import sys\n"
        "probes = [\n"
        f"{probe_lines}\n"
        "]\n"
        "broken = []\n"
        "for mod, attr in probes:\n"
        "    try:\n"
        "        imported = __import__(mod)\n"
        "        if not hasattr(imported, attr):\n"
        "            broken.append(mod)\n"
        "        elif mod == 'certifi':\n"
        "            # The module can import cleanly while cacert.pem is\n"
        "            # missing/corrupt (brew Python upgrade, interrupted venv\n"
        "            # rebuild) - every TLS call then fails (#29866).\n"
        "            bundle = imported.where()\n"
        "            if not os.path.isfile(bundle) or os.path.getsize(bundle) < 1024:\n"
        "                broken.append(mod)\n"
        "    except Exception:\n"
        "        broken.append(mod)\n"
        "print('\\n'.join(broken))\n")
    try:
        result = _venv_probe(venv_python, check_script, env=env)
    except Exception as exc:
        logger.debug("lazy refresh import probe failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.debug("lazy refresh import probe exited %s: %s",
                     result.returncode, (result.stderr or "")[:200])
        return None
    packages: list[str] = []
    for mod in _nonblank_lines(result.stdout):
        pkg = _LAZY_REFRESH_REPAIR_PACKAGES.get(mod)
        if pkg and pkg not in packages:
            packages.append(pkg)
    return packages


def _repair_broken_lazy_refresh_imports(
    install_cmd_prefix: list[str], packages: list[str], *, env: dict[str, str] | None = None
) -> bool:
    """Force-reinstall ``packages`` and re-probe imports. Never raises."""
    if not packages:
        return True
    specs = _lazy_refresh_repair_specs(packages)
    if not _run_repair_step(
        _run_package_only_install, install_cmd_prefix + ["install", "--force-reinstall", *specs],
        env=env, log_msg="lazy refresh venv repair failed: %s", fail_msg=None):
        return False
    # Indeterminate re-probe is not confirmed success.
    return _detect_broken_lazy_refresh_imports(install_cmd_prefix, env=env) == []


def _repair_venv_via_import_probes(
    install_cmd_prefix: list[str], *, env: dict[str, str] | None = None) -> str:
    """Probe imports and force-reinstall any broken lazy-refresh packages.

    Real ``import`` checks (not distribution metadata) catch a venv where METADATA remains
    but ``.py`` files were wiped mid-install. Package-only reinstall — never rewrites
    ``hermes.exe``. Never raises. Returns ``"healthy"``, ``"repaired"``, ``"failed"``
    (repair did not confirm clean) or ``"indeterminate"`` (probes could not run; NOT healthy).

    See #57828.
    """
    broken = _detect_broken_lazy_refresh_imports(install_cmd_prefix, env=env)
    if broken is None:
        print("  ⚠ Import probes unavailable — cannot confirm venv package health.")
        return "indeterminate"
    if not broken:
        return "healthy"
    print(
        "  → Detected corrupted venv packages via import probes: "
        f"{', '.join(broken)}; repairing...")
    if _repair_broken_lazy_refresh_imports(install_cmd_prefix, broken, env=env):
        print("  ✓ Venv repair succeeded")
        return "repaired"
    manual = " ".join(shlex.quote(s) for s in _lazy_refresh_repair_specs(broken))
    print("  ⚠ Venv repair incomplete. Run manually, then `hermes update`:")
    print(f"    {' '.join(install_cmd_prefix)} install --force-reinstall {manual}")
    return "failed"


def _is_uv_command(install_cmd_prefix: list[str]) -> bool:
    """True for a uv/uvx binary (bare or path) or ``python -m uv`` / ``python -m uvx``."""
    if not install_cmd_prefix:
        return False
    first = str(install_cmd_prefix[0]).lower()
    if "uv" in Path(first).name:
        return True
    return (
        len(install_cmd_prefix) >= 3
        and first.endswith("python")
        and install_cmd_prefix[1] == "-m"
        and install_cmd_prefix[2] in ("uv", "uvx"))


def _insert_python_pin(args: list[str]) -> list[str]:
    """Insert ``--python <sys.executable>`` into a uv command line; an explicit caller ``--python`` wins."""
    if "--python" in args:
        return args
    return [args[0], "--python", str(sys.executable), *args[1:]]


def _install_python_dependencies_with_optional_fallback(
    install_cmd_prefix: list[str], *, env: dict[str, str] | None = None, group: str = "all"
) -> None:
    """Install base deps plus as many optional extras as the environment supports.

    Targets ``.[all]`` by default; Termux callers pass ``group='termux-all'``. When ``env`` carries a
    ``VIRTUAL_ENV`` that does not exist (pip / site-packages install), ``uv pip`` fails with
    ``Failed to inspect Python interpreter from active virtual environment`` before doing any
    work — pin the install at the running interpreter instead.

    Pin the install at the running interpreter instead so the update/recovery path succeeds on those
    installs (#71510 fixed the ZIP path, #83335 fixed lazy-deps; this closes the shared helper for the
    remaining callers).
    """
    # Only uv needs the explicit pin; pip resolves the target from sys.executable itself.
    pin_python = bool(
        env and env.get("VIRTUAL_ENV") and not Path(env["VIRTUAL_ENV"]).is_dir()
        and install_cmd_prefix and _is_uv_command(install_cmd_prefix))
    if pin_python:
        env = {**env}
        env.pop("VIRTUAL_ENV", None)

    def _install(args: list[str]) -> None:
        if pin_python:
            args = _insert_python_pin(args)
        _run_install_with_heartbeat(install_cmd_prefix + args, env=env)

    try:
        _install(["install", "-e", f".[{group}]"])
        return
    except subprocess.CalledProcessError:
        print(
            "  ⚠ Optional extras failed, reinstalling base dependencies and retrying extras individually..."
        )

    _install(["install", "-e", "."])
    failed_extras: list[str] = []
    installed_extras: list[str] = []
    for extra in _load_installable_optional_extras(group=group):
        try:
            _install(["install", "-e", f".[{extra}]"])
            installed_extras.append(extra)
        except subprocess.CalledProcessError:
            failed_extras.append(extra)
    if installed_extras:
        print(f"  ✓ Reinstalled optional extras individually: {', '.join(installed_extras)}")
    if failed_extras:
        print(f"  ⚠ Skipped optional extras that still failed: {', '.join(failed_extras)}")
        _warn_configured_features_missing_deps(install_cmd_prefix, env=env)
    # uv's incremental resolver has left newly added base deps silently missing on a half-stale
    # venv, surfacing hours later as a downstream ModuleNotFoundError. Verify here instead.
    _verify_core_dependencies_installed(install_cmd_prefix, env=env, group=group)


_CONFIGURED_FEATURES_SCRIPT = (
    "import importlib.util\n"
    "try:\n"
    "    from gateway.config import load_gateway_config\n"
    "    from gateway.platform_registry import platform_registry\n"
    "    from hermes_cli.plugins import discover_plugins\n"
    "    discover_plugins()\n"
    "    for platform in load_gateway_config().get_connected_platforms():\n"
    "        entry = platform_registry.get(platform.value)\n"
    "        if entry is not None and not entry.check_fn():\n"
    "            print(entry.label + '\\t' + (entry.install_hint or 'reinstall the %r extra' % platform.value))\n"
    "except Exception:\n"
    "    pass\n"
    "try:\n"
    "    from hermes_cli.config import load_config\n"
    "    if (load_config().get('mcp_servers') or {}) and importlib.util.find_spec('mcp') is None:\n"
    "        print('MCP servers\\tinstall the ' + repr('mcp') + ' extra')\n"
    "except Exception:\n"
    "    pass\n"
)


def _configured_features_missing_deps(
    install_cmd_prefix: list[str], *, env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """``(feature, hint)`` for every configured platform / MCP whose optional deps are not importable
    in the TARGET venv. A running gateway masks the gap until its next restart (#10651), so the
    update has to say which configured feature will fail to load. Probed in a fresh interpreter:
    the updater's own process may be the outer Python and its import caches predate the install,
    so an in-process ``check_fn`` reports stale state."""
    venv_python = _resolve_install_target_python(install_cmd_prefix, env)
    if venv_python is None:
        return []
    try:
        result = _venv_probe(venv_python, _CONFIGURED_FEATURES_SCRIPT, env=env)
    except Exception as exc:  # the update must finish even when the probe cannot run
        logger.debug("configured-feature dependency check skipped: %s", exc)
        return []
    missing: list[tuple[str, str]] = []
    for line in _nonblank_lines(result.stdout):
        feature, _, hint = line.partition("\t")
        missing.append((feature, hint or "reinstall its optional extra"))
    return missing


def _warn_configured_features_missing_deps(install_cmd_prefix: list[str], *, env: dict[str, str] | None = None) -> None:
    missing = _configured_features_missing_deps(install_cmd_prefix, env=env)
    if not missing:
        return
    prefix = " ".join(shlex.quote(part) for part in install_cmd_prefix)
    print("  ⚠ Configured features whose dependencies are still missing — the gateway will fail to load them on restart:")
    for feature, hint in missing:
        print(f"    - {feature}: {hint}")
    print(f"    Retry with: {prefix} install -e '.[all]'")


def _applicable_dependency_names(raw_deps: list[str]) -> list[str]:
    """Declared dep names whose ``;`` markers apply here. An unevaluable marker counts as applicable."""
    applicable: list[str] = []
    for name, marker, _ in _parse_requirements(raw_deps):
        try:
            if marker is None or marker.evaluate():  # type: ignore[union-attr]
                applicable.append(name)
        except Exception:
            applicable.append(name)
    return applicable


_MISSING_DEPS_SCRIPT = (
    "import importlib.metadata as md, sys\n"
    "missing=[]\n"
    "for name in sys.argv[1:]:\n"
    "    try: md.version(name)\n"
    "    except md.PackageNotFoundError: missing.append(name)\n"
    "print('\\n'.join(missing))\n")


def _verify_core_dependencies_installed(
    install_cmd_prefix: list[str], *, env: dict[str, str] | None = None, group: str = "all"
) -> None:
    """Check that every base dep from pyproject.toml is installed in the target venv; if not, retry.

    Reads ``pyproject.toml`` directly (not the venv's stale metadata), drops deps whose ``;``
    markers don't apply here, and probes ``importlib.metadata.version()`` in the venv
    interpreter. Missing deps trigger a base-group ``--reinstall``, then a per-package force
    install. The final state is a warning, not a hard failure, so one broken-on-PyPI dep can't
    block an otherwise-successful update — but the partial install is visible where it happened.
    """
    project = _pyproject_project("dep verification: failed to read pyproject.toml: %s")
    if project is None:
        return
    raw_deps = project.get("dependencies", []) or []
    applicable = _applicable_dependency_names(raw_deps)
    if not applicable:
        return
    # Probe inside the venv Python — sys.executable may be the outer Python that drove
    # ``hermes update``; the install prefix/env encode which environment we targeted.
    venv_python = _resolve_install_target_python(install_cmd_prefix, env)
    if venv_python is None:
        return

    def _missing_deps() -> list[str]:
        try:
            result = _venv_probe(venv_python, _MISSING_DEPS_SCRIPT, *applicable, env=env)
        except Exception as e:
            logger.debug("dep verification: subprocess failed: %s", e)
            return []
        return _nonblank_lines(result.stdout)

    missing = _missing_deps()
    if not missing:
        return
    print(
        f"  ⚠ Verification: {len(missing)} declared dep(s) missing after install: "
        f"{', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}")
    print("  → Reinstalling base group with --reinstall to repair...")
    # Base group only, not ``[{group}]``: the missing dep is a *base* dep and the all-extras
    # install costs minutes.
    if not _run_repair_step(
        _run_install_with_heartbeat, install_cmd_prefix + ["install", "--reinstall", "-e", "."],
        env=env,
        log_msg="dep verification: repair install failed: %s",
        fail_msg="  ⚠ Repair install failed; check `hermes update` output above."):
        return
    still_missing = _missing_deps()
    if not still_missing:
        print("  ✓ All declared core dependencies now installed")
        return
    # Last-ditch: install each remaining missing dep with its pin directly — uv's
    # resolver can think the env is satisfied while on-disk metadata disagrees.
    name_to_spec = dict(_naive_requirement(spec) for spec in raw_deps)
    specs = [name_to_spec.get(n, n) for n in still_missing]
    print(f"  → Force-installing remaining missing dep(s): {', '.join(specs)}")
    if not _run_repair_step(
        _run_install_with_heartbeat, install_cmd_prefix + ["install", "--reinstall", *specs],
        env=env,
        log_msg="dep verification: per-package repair failed: %s",
        fail_msg=(
            f"  ⚠ Could not install: {', '.join(still_missing)}. "
            "Run `hermes update --force` after closing other hermes processes.")):
        return
    _report_still_missing(
        _missing_deps(), "Run `hermes update --force` after closing other hermes processes.",
        ok="  ✓ All declared core dependencies now installed")


def _resolve_install_target_python(
    install_cmd_prefix: list[str], env: dict[str, str] | None) -> Path | None:
    """Python interpreter the install targeted: ``VIRTUAL_ENV`` from ``env`` for the
    ``[uv, pip]`` shape, else ``install_cmd_prefix[0]`` for ``[sys.executable, -m, pip]``."""
    if env and "VIRTUAL_ENV" in env:
        from hermes_constants import venv_python_path
        candidate = venv_python_path(Path(env["VIRTUAL_ENV"]))
        if candidate.exists():
            return candidate
    if install_cmd_prefix:
        first = Path(install_cmd_prefix[0])
        if first.exists() and "uv" not in first.name.lower():
            return first
    return None


def _is_termux_env(env: dict[str, str] | None = None) -> bool:
    from hermes_cli.main import _is_termux_startup_environment
    return _is_termux_startup_environment(env)


def _is_windows_npm_path(npm_path: str) -> bool:
    """True if ``npm_path`` points at a Windows npm shim (WSL drive interop, ``.cmd``/``.exe``, UNC).

    Callers use this only on a POSIX host — on native Windows ``npm.cmd`` is correct.
    """
    low = npm_path.lower()
    mount = low.split("/", 3)[2] if low.startswith("/mnt/") else ""
    return (
        low.endswith((".exe", ".cmd", ".bat"))
        or (len(mount) == 1 and mount.isalpha())
        or "\\" in npm_path
    )


def _resolve_node_runtime_npm() -> str | None:
    """Resolve an npm executable that belongs to the host's Node runtime.

    On WSL, PATH interop can hand back a Windows npm that fails with EISDIR / symlink errors over
    ``\\\\wsl.localhost\\...`` UNC paths. Refuse it on a POSIX host and re-scan PATH minus the
    Windows drive mounts. ``None`` when no suitable npm is reachable.

    On WSL/Linux ``shutil.which("npm")`` may resolve a Windows npm exposed through PATH interop. See #30271.
    """
    from hermes_constants import find_node_executable
    npm = find_node_executable("npm")
    if not npm:
        return None
    if not _is_windows_npm_path(npm):
        return npm
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or _is_windows_npm_path(directory):
            continue
        candidate = shutil.which("npm", path=directory)
        if candidate and not _is_windows_npm_path(candidate):
            return candidate
    return None


def _resolve_update_branch(args) -> str:
    """Normalize ``args.branch`` to a non-empty name (default ``main``; blank/whitespace = default)."""
    return (getattr(args, "branch", None) or "main").strip() or "main"
