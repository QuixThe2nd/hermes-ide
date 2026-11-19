"""Install validated, index-only Python requirements for plugin manifests.

Unlike :mod:`pm.extras`, these requirements are data supplied by a plugin rather
than a Hermes-owned extra. Validation, install policy, constraints, durable
-target routing, and result reporting therefore stay explicit at this boundary.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_NAME_RE = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"
_SAFE_SPEC = re.compile(
    rf"^{_NAME_RE}(?:\[[A-Za-z0-9_,\-]+\])?"
    r"(?:[<>=!~]=?[A-Za-z0-9_.\-+,*<>=!~]+)?$"
)
_LAZY_TARGET_ENV = "HERMES_LAZY_INSTALL_TARGET"
_TARGET_STAMP_NAME = ".python-abi"
_SUBPROCESS_KW = dict(
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    stdin=subprocess.DEVNULL,
)


def spec_is_safe(spec: str) -> bool:
    """Accept only ``name[extras]specifier``; reject URLs, paths, and flags."""
    return bool(
        spec
        and len(spec) <= 200
        and not any(ch in spec for ch in (";", "|", "&", "`", "$", "\n", "\r", "\t", "\\"))
        and not spec.startswith(("-", "/", "."))
        and "://" not in spec
        and "@" not in spec
        and _SAFE_SPEC.match(spec)
    )


@dataclass(frozen=True)
class _InstallResult:
    success: bool
    stdout: str
    stderr: str


@dataclass
class InstallSpecsResult:
    """Outcome of one requirement batch; ``blocked`` means nothing executed."""

    ok: bool
    blocked: bool = False
    reason: str = ""
    command: str = ""
    stdout: str = ""
    stderr: str = ""


def _python_abi_tag() -> str:
    return (
        f"{sys.version_info.major}.{sys.version_info.minor}:"
        f"{sysconfig.get_config_var('EXT_SUFFIX') or ''}"
    )


def _lazy_install_target() -> Optional[Path]:
    raw = os.environ.get(_LAZY_TARGET_ENV, "").strip()
    return Path(raw) if raw else None


def _ensure_target_ready(target: Path) -> Optional[str]:
    """Prepare an ABI-specific durable target, removing stale binary packages."""
    stamp = target / _TARGET_STAMP_NAME
    want = _python_abi_tag()
    try:
        if target.exists():
            have = ""
            try:
                have = stamp.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if have and have != want:
                logger.info("Discarding plugin install target from ABI %r (running %r)", have, want)
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        with contextlib.suppress(OSError):
                            child.unlink()
        target.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want, encoding="utf-8")
    except OSError as exc:
        return f"lazy install target {target} is not writable: {exc}"
    return None


def _invalidate_import_caches() -> None:
    with contextlib.suppress(Exception):
        import importlib
        import importlib.metadata as metadata

        importlib.invalidate_caches()
        if hasattr(metadata, "_cache_clear"):
            metadata._cache_clear()  # type: ignore[attr-defined]


def _activate_target_on_syspath(target: Path) -> None:
    """Append the target so core site-packages always wins name collisions."""
    target_str = str(target)
    before = list(sys.path)
    if target_str not in before:
        site.addsitedir(target_str)
    new_entries = [path for path in sys.path if path not in before]
    if new_entries:
        sys.path[:] = [path for path in sys.path if path not in new_entries] + new_entries
    _invalidate_import_caches()


def _config_allows_installs() -> bool:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return True
    return bool((config.get("security") or {}).get("allow_lazy_installs", True))


def _installs_allowed(target: Optional[Path]) -> bool:
    if not _config_allows_installs():
        return False
    return not (os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1" and target is None)


def _core_constraints_file() -> Optional[Path]:
    """Pin core distributions for durable targets so shadow copies cannot be installed."""
    try:
        import tempfile
        from importlib.metadata import distributions

        pins: dict[str, str] = {}
        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            if name and dist.version and name.lower() not in pins:
                pins[name.lower()] = f"{name}=={dist.version}"
        if not pins:
            return None
        fd, path = tempfile.mkstemp(prefix="hermes-core-constraints-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(sorted(pins.values())) + "\n")
        return Path(path)
    except Exception as exc:
        logger.debug("Could not build core constraints file: %s", exc)
        return None


def _write_constraints_file(lines: tuple[str, ...]) -> Path:
    import tempfile

    fd, path = tempfile.mkstemp(prefix="hermes-plugin-constraints-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return Path(path)


def _package_name(spec: str) -> str:
    match = re.match(rf"^({_NAME_RE})", spec)
    return match.group(1) if match else spec


def _installed_dist_roots(spec: str, target: Optional[Path]) -> set[Path]:
    roots: set[Path] = set()
    try:
        import importlib.metadata as metadata

        name = _package_name(spec)
        dist = (
            next(iter(metadata.distributions(name=name, path=[str(target)])), None)
            if target is not None
            else metadata.distribution(name)
        )
        for entry in dist.files or () if dist is not None else ():
            top = entry.parts[0] if entry.parts else ""
            if not top or top.startswith(".") or top == "__pycache__" or top.endswith(
                (".dist-info", ".egg-info")
            ):
                continue
            root = Path(dist.locate_file(top))
            if root.is_dir():
                roots.add(root)
    except Exception:
        return set()
    return roots


def _warm_installed_bytecode(specs: tuple[str, ...], target: Optional[Path]) -> None:
    if sys.dont_write_bytecode:
        return
    try:
        import compileall
    except Exception:
        return
    for spec in specs:
        try:
            for root in _installed_dist_roots(spec, target):
                try:
                    compileall.compile_dir(str(root), quiet=2, force=False, workers=1)
                except Exception as exc:
                    logger.debug("Bytecode warm skipped for %s: %s", root, exc)
        except Exception as exc:
            logger.debug("Bytecode warm skipped for %s: %s", spec, exc)


def _run_installer(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        **_SUBPROCESS_KW,
        **kwargs,
    )


def _uv_binary() -> Optional[str]:
    try:
        from hermes_cli.runtime_repair import resolve_uv

        return resolve_uv() or shutil.which("uv")
    except Exception:
        return shutil.which("uv")


def _venv_pip_install(
    specs: tuple[str, ...],
    *,
    timeout: int = 300,
    constraint_lines: tuple[str, ...] = (),
    dry_run: bool = False,
) -> _InstallResult:
    """Resolve/install through uv, with pip and ensurepip as the fallback ladder."""
    if not specs:
        return _InstallResult(True, "", "")
    target = _lazy_install_target()
    constraints: Optional[Path] = None
    extra_args = ["--dry-run"] if dry_run else []
    if target is not None:
        if error := _ensure_target_ready(target):
            return _InstallResult(False, "", error)
        constraints = _core_constraints_file()
        extra_args += ["--target", str(target)]
    elif constraint_lines:
        constraints = _write_constraints_file(constraint_lines)
    if constraints is not None:
        extra_args += ["--constraint", str(constraints)]

    def finish(result: subprocess.CompletedProcess) -> _InstallResult:
        if result.returncode == 0 and not dry_run:
            if target is not None:
                _activate_target_on_syspath(target)
            _warm_installed_bytecode(specs, target)
        return _InstallResult(result.returncode == 0, result.stdout or "", result.stderr or "")

    try:
        from tools.environments.local import hermes_subprocess_env

        env = hermes_subprocess_env(inherit_credentials=False)
        env["VIRTUAL_ENV"] = str(Path(sys.executable).parent.parent)
        if uv_bin := _uv_binary():
            try:
                result = _run_installer(
                    [uv_bin, "pip", "install", "--compile-bytecode", *extra_args, *specs],
                    timeout=timeout,
                    env=env,
                )
                if result.returncode != 0:
                    logger.debug("uv pip install failed: %s", result.stderr)
                # A uv resolution failure is authoritative; pip would bypass uv policy.
                return finish(result)
            except subprocess.TimeoutExpired as exc:
                return _InstallResult(False, "", f"uv pip install timed out: {exc}")
            except FileNotFoundError as exc:
                logger.debug("uv invocation failed: %s", exc)
        pip_command = [sys.executable, "-m", "pip"]
        try:
            if _run_installer(pip_command + ["--version"], timeout=15).returncode != 0:
                raise FileNotFoundError("pip not in venv")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            try:
                _run_installer(
                    [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                    timeout=120,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return _InstallResult(False, "", f"pip not available and ensurepip failed: {exc}")
        try:
            return finish(
                _run_installer(pip_command + ["install", *extra_args, *specs], timeout=timeout)
            )
        except subprocess.TimeoutExpired as exc:
            return _InstallResult(False, "", f"pip install timed out: {exc}")
        except Exception as exc:
            return _InstallResult(False, "", f"pip install failed: {exc}")
    finally:
        if constraints is not None:
            with contextlib.suppress(OSError):
                constraints.unlink()


def install_specs(
    specs: list[str] | tuple[str, ...],
    *,
    timeout: int = 300,
    constraints: list[str] | tuple[str, ...] = (),
    dry_run: bool = False,
) -> InstallSpecsResult:
    """Install arbitrary validated requirements without raising installer errors."""
    cleaned = tuple(str(spec).strip() for spec in specs if str(spec).strip())
    if not cleaned:
        return InstallSpecsResult(ok=True, command="")
    for spec in cleaned:
        if not spec_is_safe(spec):
            return InstallSpecsResult(
                ok=False,
                blocked=True,
                reason=f"refusing to install unsafe spec {spec!r}",
            )
    target = _lazy_install_target()
    if not _installs_allowed(target):
        sealed = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1" and target is None
        reason = (
            "runtime installs are disabled on this deployment: the agent environment is immutable "
            "and no writable install target is configured (HERMES_LAZY_INSTALL_TARGET)"
            if sealed
            else "runtime installs disabled (security.allow_lazy_installs=false)"
        )
        return InstallSpecsResult(ok=False, blocked=True, reason=reason)
    display = (
        "uv pip install "
        + (f"--target {target} " if target is not None else "")
        + " ".join(cleaned)
    )
    logger.info(
        "%s pip specs %s (target=%s)",
        "Resolving" if dry_run else "Installing",
        " ".join(cleaned),
        target or "venv",
    )
    try:
        result = _venv_pip_install(
            cleaned,
            timeout=timeout,
            constraint_lines=tuple(constraints),
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.warning("install_specs failed unexpectedly: %s", exc)
        return InstallSpecsResult(ok=False, command=display, stderr=f"install failed: {exc}")
    _invalidate_import_caches()
    return InstallSpecsResult(
        ok=result.success,
        command=display,
        stdout=result.stdout,
        stderr=result.stderr,
    )
