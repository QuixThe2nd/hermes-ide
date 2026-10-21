"""Managed Python runtime repair (checkout venv surgery).

The Python backing a CHECKOUT install is shared by every Hermes profile
because the checkout's ``venv`` is shared.  A vulnerable interpreter is
never reinstalled in place: we provision a new immutable Python generation
into the install's runtime dir and build and smoke-test a relocatable
sibling venv from it.  POSIX installs cut over with same-filesystem
directory renames — the old venv stays parked for synchronous rollback
and is swept for cleanup once it is clearly stale.  Windows installs
atomically repoint the live venv's ``pyvenv.cfg`` at the new generation
instead, because any open handle under the venv (cwd, open file, sync
client) makes Windows refuse the rename.

Sealed trees never reach this module — their interpreter is a build
artifact (pm's ``sealed()`` payloads ship a staged python and refuse
runtime installs).

uv itself is NOT this module's business: the pinned uv is realized through
``pm`` (``pm/lock.json`` + the pm store), like every other managed tool.
This module previously carried its own uv acquisition (the old managed-uv
module); that half predated the package manager and is retired — except
:func:`pip_install_hint`, which still names Hermes' own uv binary when a
managed install has one (the installer drops it in ``$HERMES_HOME/bin``
without putting that on PATH, so a bare ``uv`` would fail for
installer-only users).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from hermes_cli.sqlite_runtime import (
    SQLiteRuntimeInfo, isolated_interpreter_env, probe_sqlite_runtime)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VENV_NAME = "venv"
_ALT_VENV_NAME = ".venv"
_RUNTIME_DIR_NAME = ".hermes-runtime"
_MACOS_MANAGED_PYTHON_IDENTIFIER = "com.nousresearch.hermes.managed-python"
_REPAIR_LOCK_NAME = "runtime-repair.lock"

_Provisioned = tuple[Path, Path, SQLiteRuntimeInfo]

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def _runtime_dir(project_root: Path) -> Path:
    """The checkout-scoped scratch dir for repair artifacts.

    Deliberately NOT the pm store: the pm store is machine-wide and holds
    immutable published entries, while generations, candidate venvs, and
    the repair lock are private to one checkout and its cutover.
    """
    return Path(project_root) / _RUNTIME_DIR_NAME


def _uv_binary_path() -> Path:
    """Path of Hermes' own uv binary (``$HERMES_HOME/bin/uv[.exe]``); may not exist yet.

    Only a lookup helper for :func:`resolve_uv`/:func:`pip_install_hint` — uv
    ACQUISITION is pm's business now (see the module docstring).
    """
    return get_hermes_home() / "bin" / ("uv.exe" if platform.system() == "Windows" else "uv")


def resolve_uv() -> Optional[str]:
    """Return the managed uv path if it exists, else ``None``."""
    p = _uv_binary_path()
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


def pip_install_hint(package: str) -> str:
    """Copy-pasteable command that installs *package* into the running interpreter.

    Names Hermes' own uv when it exists: the installer drops it in ``$HERMES_HOME/bin``
    without putting that on PATH, so a bare ``uv`` would fail for installer-only users.
    """
    return f"{resolve_uv() or 'uv'} pip install --python {sys.executable} {package}"


def managed_python_install_dir(project_root: Path | None = None) -> Path:
    """Return the checkout-scoped Python store shared by all profiles."""
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    return _runtime_dir(root) / "python"


def managed_python_env(
    project_root: Path | None = None,
    *,
    install_dir: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized environment for Hermes-private uv Python commands.

    Builds on pm's ``uv_env`` sanitization (which strips every ``UV_*``
    override and active-venv leakage — the interpreter-hijack class), then
    pins uv's managed-Python behavior to the private install dir.
    """
    from pm.packages import uv_env

    target = (
        Path(install_dir)
        if install_dir is not None
        else managed_python_install_dir(project_root)
    )
    env = uv_env(dict(os.environ if base_env is None else base_env))
    for key in (
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        env.pop(key, None)
    env.update({
        "UV_MANAGED_PYTHON": "1",
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_PYTHON_INSTALL_DIR": str(target),
        "UV_PYTHON_INSTALL_REGISTRY": "0",
    })
    return env


@dataclass(frozen=True)
class RuntimeRepairResult:
    """Outcome of a managed-runtime repair attempt."""

    status: str
    detail: str = ""
    sqlite_before: str = ""
    sqlite_after: str = ""
    backup_venv: Path | None = None

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"


@dataclass(frozen=True)
class _RepairLock:
    path: Path
    fd: int


def _report_runtime_repair_failure(repair: RuntimeRepairResult) -> None:
    if repair.backup_venv is None:
        print(
            "  ℹ Managed Python runtime was not replaced; "
            f"the existing venv is unchanged ({repair.detail})."
        )
        print(
            "    Sessions stay protected meanwhile: Hermes keeps databases "
            "out of WAL mode on this SQLite build. The next `hermes update` "
            "will retry."
        )
        return
    print(f"  ✗ Managed Python runtime cutover needs manual recovery: {repair.detail}")
    print(f"    Previous venv: {repair.backup_venv}")


def _record_runtime_repair(repair: RuntimeRepairResult) -> None:
    """Put the repair outcome into the update receipt (no-op outside ``hermes update``).

    Receipts are built only from explicit ``record_step``/``record_skip`` calls, so without this
    a failed repair left ``outcome: partial`` with no step naming the reason or the SQLite
    versions. A deferred or not-applicable repair is a skip WITH its reason, not a failed step:
    every pip/non-venv install would otherwise carry a red step in every receipt.
    """
    from hermes_cli.update_receipt import record_skip, record_step

    detail = (
        f"{repair.status}: {repair.detail}" if repair.detail else repair.status
    ) + f" (sqlite {repair.sqlite_before or 'unknown'} → {repair.sqlite_after or 'unknown'})"
    if repair.status in {"skipped", "not-applicable"}:
        record_skip("sqlite_runtime_repair", detail)
    else:
        record_step("sqlite_runtime_repair", repair.status in {"safe", "repaired"}, detail)


def _macos_sign_managed_python(python: Path) -> bool:
    """Give a newly downloaded managed Python a stable macOS code identity.

    python-build-standalone binaries are ad-hoc signed, so TCC sees a cdhash-only identity that
    changes every runtime generation; an identifier-pinned designated requirement keeps it stable
    without a Developer ID. Best effort: a missing/incompatible ``codesign`` must not block repair.
    """
    if platform.system() != "Darwin":
        return False
    codesign = shutil.which("codesign")
    if not codesign:
        logger.info("macOS codesign is unavailable; using the downloaded Python signature")
        return False
    requirement = f'=designated => identifier "{_MACOS_MANAGED_PYTHON_IDENTIFIER}"'
    try:
        sign = [
            codesign, "--force", "--deep", "--sign", "-", "--timestamp=none",
            "--identifier", _MACOS_MANAGED_PYTHON_IDENTIFIER,
            "--requirements", requirement, str(python)]
        verify = [codesign, "--verify", "--deep", "--strict", str(python)]
        steps = (
            (sign, "could not stably sign managed Python %s: %s", "codesign failed"),
            (verify, "macOS signature verification failed for managed Python %s: %s",
             "verification failed"))
        for cmd, warning, fallback in steps:
            result = subprocess.run(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            if result.returncode != 0:
                logger.warning(
                    warning, python, (result.stderr or result.stdout or fallback).strip())
                return False
        return True
    except Exception as exc:
        logger.warning("could not sign managed Python %s: %s", python, exc)
        return False


# ---------------------------------------------------------------------------
# Managed Python runtime repair
# ---------------------------------------------------------------------------


def _reload_hermes_constants():
    """Re-execute ``hermes_constants`` from disk and return the fresh module.

    ``hermes update`` imports ``hermes_constants`` from the OLD checkout,
    ``git pull`` then replaces that file, and this freshly-pulled module runs
    its lazy imports against the module object Python already cached in
    ``sys.modules`` — the pre-upgrade one. A symbol added by the update is
    absent there while the file named in the resulting ``ImportError`` plainly
    contains it, which is what made this read as a contradiction:

        cannot import name 'venv_python_path' from 'hermes_constants'
        (~/.hermes/hermes-agent/hermes_constants.py)

    Reloading picks up the definitions actually on disk, so callers keep using
    the shared helper instead of hand-rolling a second copy of its logic.
    """
    import hermes_constants

    return importlib.reload(hermes_constants)


def _venv_python(venv_dir: Path) -> Path:
    windows = platform.system() == "Windows"
    try:
        from hermes_constants import venv_python_path
    except ImportError:
        venv_python_path = _reload_hermes_constants().venv_python_path
    return venv_python_path(venv_dir, windows=windows)


def _remove_tree(path: Path, *, boundary: Path) -> None:
    """Best-effort removal constrained to a known runtime boundary."""
    try:
        path.resolve().relative_to(boundary.resolve())
    except (OSError, ValueError):
        return
    shutil.rmtree(path, ignore_errors=True)


def _reject(path: Path, boundary: Path, msg: str, *args) -> None:
    """Log a rejected candidate and clean up its tree; always returns ``None``."""
    logger.warning(msg, *args)
    _remove_tree(path, boundary=boundary)
    return None


def _token() -> str:
    return f"{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _dotted(parts) -> str:
    return ".".join(str(p) for p in parts)


def _make_world_traversable(path: Path) -> None:
    """Keep root/FHS-managed runtimes executable by non-root callers."""
    with contextlib.suppress(OSError):
        path.chmod(path.stat().st_mode | 0o755)


def _runtime_request(info: SQLiteRuntimeInfo) -> str:
    """Pin the candidate to the current CPython minor line (e.g. ``3.11``): requesting the exact
    patch can never repair installs whose patch has no fixed-SQLite artifact at all."""
    return _dotted(info.python_version[:2])


# Cap on how many newer patches we'll try, newest-first, before giving up.
# Bounded because each attempt is a real download+install+probe+delete cycle;
# in practice the fix is almost always in the very next patch or two.
_MAX_PATCH_RETRIES = 5


def _list_available_patches(
    uv_bin: str, minor: str, *, cwd: Path, env: dict) -> list[tuple[int, int, int]]:
    """Known patch versions for ``minor`` (e.g. "3.11"), newest first; [] on any failure
    (network, parse), in which case callers fall back to the bare-minor request.

    Queries ``uv python list --all-versions`` rather than trusting the bare minor-line request to resolve to
    the newest patch (issue #71250: on some hosts/uv versions, the resolved candidate for a bare "3.11"
    request can be an older cached/indexed patch that still links a vulnerable SQLite, even when a newer
    non-vulnerable patch is available).
    """
    try:
        result = subprocess.run(
            [
                uv_bin, "python", "list", minor, "--all-versions", "--only-downloads",
                "--output-format", "json", "--no-config"],
            cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=15)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        versions: list[tuple[int, int, int]] = []
        for entry in json.loads(result.stdout):
            if not isinstance(entry, dict):
                continue
            # Only default/cpython builds -- skip pypy/graalpy/freethreaded variants,
            # which aren't what this repair path wants.
            if entry.get("implementation") not in (None, "cpython") or (
                entry.get("variant") not in (None, "default")):
                continue
            parts = entry.get("version_parts") or {}
            try:
                versions.append(
                    (int(parts["major"]), int(parts["minor"]), int(parts["patch"])))
            except (KeyError, TypeError, ValueError):
                continue
        # Deduplicate (list --all-versions can repeat a version across
        # platforms/arches if filtering above didn't fully narrow it) and sort
        # newest-first.
        return sorted(set(versions), reverse=True)
    except Exception:
        return []


def _attempt_install_generation(
    uv_bin: str, request: str, *, project_root: Path, python_root: Path,
    current: SQLiteRuntimeInfo, allow_minor_upgrade: bool = False,
    tried_versions: set[tuple[int, int, int]] | None = None) -> _Provisioned | None:
    """One install+probe attempt for ``request`` (bare minor "3.11" or explicit patch "3.11.15").

    Each attempt gets its own generation directory so a rejected candidate is fully cleaned up
    before the next attempt (--reinstall semantics). Returns None (and cleans up) on any failure,
    including a vulnerable or off-line candidate.

    When *tried_versions* is given, the probed candidate's version is
    recorded in it so callers looping over explicit patches can skip a
    version a bare-minor request already resolved to (and rejected) --
    retrying it explicitly would spend a full download+install+probe+delete
    cycle to reach a certain rejection.
    """
    generation = python_root / f"generation-{_token()}"
    generation.mkdir(parents=True, exist_ok=False)
    _make_world_traversable(generation)

    reject = partial(_reject, generation, python_root)
    env = managed_python_env(project_root, install_dir=generation)
    run = dict(cwd=project_root, env=env, capture_output=True, text=True, check=False)
    install = subprocess.run(
        [uv_bin, "python", "install", request, "--reinstall", "--no-bin", "--no-registry",
         "--no-config"],
        **run)
    if install.returncode != 0:
        return reject(
            "private Python install failed for %s (rc=%d): %s",
            request, install.returncode, (install.stderr or install.stdout or "").strip())
    found = subprocess.run(
        [uv_bin, "python", "find", request, "--managed-python", "--no-config"], **run)
    if found.returncode != 0 or not found.stdout.strip():
        return reject(
            "private Python lookup failed for %s (rc=%d): %s",
            request, found.returncode, (found.stderr or "").strip())
    python = Path(found.stdout.strip().splitlines()[-1])
    try:
        python.resolve().relative_to(generation.resolve())
    except (OSError, ValueError):
        return reject("uv resolved Python outside the Hermes generation: %s", python)
    # Sign before the candidate is probed or promoted so each immutable generation does not look
    # like a new TCC principal on macOS. Non-fatal: the SQLite repair proceeds regardless.
    _macos_sign_managed_python(python)
    candidate = probe_sqlite_runtime(python)
    if candidate is None:
        return reject("could not probe candidate Python runtime: %s", python)
    if tried_versions is not None:
        tried_versions.add(candidate.python_version[:3])
    if allow_minor_upgrade:
        # Falling forward to a higher minor line: only reject downgrades.
        if candidate.python_version < current.python_version:
            return reject(
                "candidate Python downgraded from %s: %s",
                _dotted(current.python_version), candidate.python_version)
    elif candidate.python_version[:2] != current.python_version[:2] or (
        candidate.python_version < current.python_version):
        return reject(
            "candidate Python drifted off the %s minor line or downgraded: %s",
            _dotted(current.python_version[:2]), candidate.python_version)
    if candidate.wal_reset_vulnerable:
        return reject(
            "candidate Python still links vulnerable SQLite %s (%s)",
            candidate.sqlite_version_string, candidate.sqlite_source_id)
    return generation, python, candidate


def _retry_explicit_patches(
    uv_bin: str, request: str, *, project_root: Path, python_root: Path,
    current: SQLiteRuntimeInfo, tried: set[tuple[int, int, int]],
    allow_minor_upgrade: bool = False, skip_at_or_below: tuple[int, int, int] | None = None,
) -> _Provisioned | None:
    """Retry ``request``'s minor line with explicit patches, newest-first, at most
    ``_MAX_PATCH_RETRIES`` attempts, skipping versions already in ``tried`` (a certain rejection
    still costs a full download+install+probe+delete cycle).

    ``skip_at_or_below`` also skips patches at or below that version: only NEWER patches can carry
    the fix and the downgrade guard rejects the rest; on a stale uv catalog the newest indexed
    patch can be the installed one, and the loop would burn every retry walking backwards
    (in #71250 the newest indexed 3.11 was 3.11.14, exactly the installed version, so without
    this skip the loop burned all five retries walking backwards before failing).
    """
    # The bare minor-line request resolved to a still-vulnerable (or otherwise rejected) candidate. Rather
    # than giving up immediately, query which patches on this minor line uv actually knows about and retry
    # with explicit newer versions, newest-first -- this handles the case where the default resolution for
    # a bare request picks an older cached/indexed patch even though a newer, non-vulnerable one is available
    # (issue #71250).
    env_for_list = managed_python_env(project_root, install_dir=python_root)
    patches = _list_available_patches(uv_bin, request, cwd=project_root, env=env_for_list)
    attempts = 0
    for version_tuple in patches:
        if attempts >= _MAX_PATCH_RETRIES:
            break
        if version_tuple in tried:
            continue
        if skip_at_or_below is not None and version_tuple <= skip_at_or_below:
            continue
        tried.add(version_tuple)
        explicit = _dotted(version_tuple)
        print(f"  → Retrying with explicit patch {explicit}...")
        attempts += 1
        result = _attempt_install_generation(
            uv_bin, explicit, project_root=project_root,
            python_root=python_root, current=current,
            allow_minor_upgrade=allow_minor_upgrade)
        if result is not None:
            return result
    return None


def _provision_line(
    uv_bin: str, request: str, *, tried: set[tuple[int, int, int]],
    allow_minor_upgrade: bool = False, skip_at_or_below: tuple[int, int, int] | None = None,
    **common) -> _Provisioned | None:
    """Try ``request`` once, then its explicit newer patches; None when the whole line fails."""
    result = _attempt_install_generation(
        uv_bin, request, tried_versions=tried, allow_minor_upgrade=allow_minor_upgrade, **common)
    if result is None:
        result = _retry_explicit_patches(
            uv_bin, request, tried=tried, allow_minor_upgrade=allow_minor_upgrade,
            skip_at_or_below=skip_at_or_below, **common)
    return result


def _install_safe_python_generation(
    uv_bin: str, *, project_root: Path, current: SQLiteRuntimeInfo) -> _Provisioned | None:
    runtime_root = _runtime_dir(project_root)
    python_root = managed_python_install_dir(project_root)
    _make_world_traversable(runtime_root)
    _make_world_traversable(python_root)
    common = dict(project_root=project_root, python_root=python_root, current=current)

    request = _runtime_request(current)
    print(f"  → Provisioning a private Python {request} runtime with fixed SQLite...")
    tried_versions = {current.python_version[:3]}
    # If the bare minor-line request resolves to a still-vulnerable (or otherwise rejected)
    # candidate, the default resolution may have picked an older cached/indexed patch even though
    # a newer, non-vulnerable one exists: retry with explicit newer patches, newest-first.
    result = _provision_line(
        uv_bin, request, tried=tried_versions, skip_at_or_below=current.python_version[:3], **common
    )
    if result is not None:
        return result
    # All patches on the current minor line are vulnerable or rejected. Fall forward to the next
    # supported minor (e.g. 3.11 → 3.12) so the user isn't stuck on every `hermes update`. The
    # requires-python window (>=3.11,<3.14) and the import smoke-test gate compatibility.
    # See #76106.
    cur_major, cur_minor = current.python_version[:2]
    fb_tried: set[tuple[int, int, int]] = set(tried_versions)
    for next_minor in range(cur_minor + 1, 14):  # up to 3.13
        next_request = f"{cur_major}.{next_minor}"
        print(
            f"  → No fixed {cur_major}.{cur_minor} build available; "
            f"trying {next_request} as fallback...")
        result = _provision_line(
            uv_bin, next_request, tried=fb_tried, allow_minor_upgrade=True, **common)
        if result is not None:
            return result
    return None


def _smoke_candidate_venv(venv_dir: Path) -> tuple[bool, str, SQLiteRuntimeInfo | None]:
    """Exercise the candidate interpreter and imports through its real path."""
    python = _venv_python(venv_dir)
    info = probe_sqlite_runtime(python)
    if info is None:
        return False, f"could not execute {python}", None
    if info.wal_reset_vulnerable:
        return False, f"candidate still links vulnerable SQLite {info.sqlite_version_string}", info
    check = (
        "import dotenv, fastapi, openai, prompt_toolkit, pydantic, rich, uvicorn, yaml\n"
        "import hermes_state\n")
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", check], cwd=venv_dir.parent, env=isolated_interpreter_env(),
            capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), info
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "core import smoke failed").strip()
        return False, detail.splitlines()[-1] if detail else "core import smoke failed", info
    return True, "", info


# A failed ``uv sync`` prints its diagnosis last, so the tail is the actionable part. Kept
# short: the reason travels into a one-line log entry, the failure report and the receipt step.
_SYNC_TAIL_LINES = 6
_SYNC_REASON_CHARS = 600


def _sync_reason(tail: deque[str]) -> str:
    """The actionable part of a failed sync: uv's ``error:`` line and whatever follows it.

    uv prints progress ("Resolving…", "Resolved 259 packages") before the diagnosis, so the raw
    tail leads with noise; the ``error:``/``hint:`` pair is the part a user can act on.
    """
    parts = [line for line in tail if line.strip()]
    for index, line in enumerate(parts):
        if line.lower().startswith(("error:", "error ")):
            parts = parts[index:]
            break
    else:
        parts = parts[-2:]
    return " | ".join(parts).strip()[:_SYNC_REASON_CHARS]


def _stream_sync(argv: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run the candidate's locked sync, forwarding output live; return ``(rc, reason)``.

    Streaming is load-bearing, not cosmetic: older desktop update hand-offs drain only the
    child's stdout while it runs, so a full stderr pipe blocks uv forever — stderr is merged
    into stdout and forwarded line by line instead of being captured and reprinted at the end.

    The tail is kept anyway: with inherited stdout the child's diagnosis survived in console
    scrollback only, and the rejection carried a bare exit code — "hermes update says the SQLite
    repair failed and never says why".
    """
    proc = subprocess.Popen(
        list(argv), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail: deque[str] = deque(maxlen=_SYNC_TAIL_LINES)
    stream = proc.stdout
    if stream is not None:
        for line in stream:
            tail.append(line.rstrip())
            sys.stdout.write(line)
            sys.stdout.flush()
    status = proc.wait()
    return status, _sync_reason(tail)


class _CandidateStageError(Exception):
    """A rejected candidate, already cleaned up, with its diagnostic reason."""


def _stage_candidate_venv(
    uv_bin: str, *, project_root: Path, generation: Path, python: Path) -> Path:
    runtime_root = _runtime_dir(project_root)
    candidate = runtime_root / f"venv-candidate-{_token()}"
    env = managed_python_env(project_root, install_dir=generation)
    env.update({
        "UV_PROJECT_ENVIRONMENT": str(candidate), "UV_PYTHON": str(python),
        "UV_PYTHON_DOWNLOADS": "never", "VIRTUAL_ENV": str(candidate)})

    def reject(message: str, *args) -> None:
        _reject(candidate, runtime_root, message, *args)
        raise _CandidateStageError(message % args if args else message)
    print("  → Building a relocatable replacement environment...")
    created = subprocess.run(
        [
            uv_bin, "venv", str(candidate), "--python", str(python),
            "--managed-python", "--no-python-downloads", "--relocatable", "--no-config"],
        cwd=project_root, env=env, capture_output=True, text=True, check=False)
    if created.returncode != 0:
        return reject(
            "candidate venv creation failed (rc=%d): %s",
            created.returncode, (created.stderr or created.stdout or "").strip())
    if not (project_root / "uv.lock").is_file():
        return reject("candidate dependency sync refused: uv.lock is missing")
    # Locked sync must see project [tool.uv] exclude-newer; --no-config / UV_NO_CONFIG drops it
    # and uv 0.12+ refuses --locked.
    sync_env = dict(env)
    sync_env.pop("UV_NO_CONFIG", None)
    # stderr=STDOUT: uv writes progress to stderr. Legacy desktop
    # hand-offs (pre scripts/desktop-update/windows.ps1, which drains
    # both pipes) only drain the child's stdout while the child runs; a
    # full stderr pipe (~64KB) blocks uv forever. Merging into stdout
    # keeps the output streaming through the pipe old hand-offs DO
    # drain. This module is imported lazily by update_cmd AFTER the git
    # reset, so even an update running from an old base executes THIS
    # copy — unlike the heartbeat helper (main_install_repair.py), which
    # is imported at startup and only protects bases that ship its twin.
    status, reason = _stream_sync(
        [uv_bin, "sync", "--extra", "all", "--locked", "--python", str(_venv_python(candidate))],
        cwd=project_root, env=sync_env)
    if status != 0:
        # The reason travels with the rejection into RuntimeRepairResult.detail, which the
        # failure report prints and the update receipt records.
        return reject("candidate dependency sync failed (rc=%d): %s", status, reason)
    healthy, detail, _ = _smoke_candidate_venv(candidate)
    if not healthy:
        return reject("candidate venv smoke failed: %s", detail)
    return candidate


def _rename_with_retry(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for delay in (0.0, 0.1, 0.25, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        try:
            source.rename(destination)
            return
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def _cut_over_candidate(
    candidate: Path, *, project_root: Path, live: Path | None = None
) -> tuple[bool, Path | None, SQLiteRuntimeInfo | None, str]:
    live = live if live is not None else project_root / _VENV_NAME
    runtime_root = _runtime_dir(project_root)
    token = _token()
    backup = live.with_name(f"{live.name}.stale.runtime-{token}")
    rejected = runtime_root / f"venv-rejected-{token}"

    try:
        try:
            _rename_with_retry(live, backup)
        except OSError as exc:
            return False, None, None, f"could not park the existing venv: {exc}"

        try:
            _rename_with_retry(candidate, live)
        except OSError as promote_error:
            try:
                _rename_with_retry(backup, live)
            except OSError as rollback_error:
                return (
                    False,
                    backup,
                    None,
                    "could not promote the replacement venv "
                    f"({promote_error}); rollback failed ({rollback_error})",
                )
            return (
                False,
                None,
                None,
                f"could not promote the replacement venv: {promote_error}",
            )

        try:
            healthy, detail, info = _smoke_candidate_venv(live)
        except Exception as exc:
            healthy, detail, info = False, f"candidate smoke raised: {exc}", None
        if healthy:
            return True, backup, info, ""

        try:
            _rename_with_retry(live, rejected)
            _rename_with_retry(backup, live)
        except OSError as exc:
            return (
                False,
                backup,
                info,
                "post-cutover smoke failed "
                f"({detail}); rollback failed ({exc}); rejected venv: {rejected}",
            )
        _remove_tree(rejected, boundary=runtime_root)
        return False, None, info, f"post-cutover smoke failed: {detail}"
    except BaseException:
        if not live.exists() and backup.exists():
            try:
                _rename_with_retry(backup, live)
            except OSError as exc:
                logger.error(
                    "interrupted runtime cutover could not restore %s from %s: %s",
                    live,
                    backup,
                    exc,
                )
        raise


def _replace_file_atomically(path: Path, data: bytes) -> None:
    """Replace *path* from a same-directory temporary file."""
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    temporary = path.with_name(f".{path.name}.runtime-{token}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _cut_over_windows_runtime_config(
    candidate: Path,
    *,
    live: Path,
    current: SQLiteRuntimeInfo,
    candidate_info: SQLiteRuntimeInfo,
) -> tuple[bool, bool, SQLiteRuntimeInfo | None, str]:
    """Repoint a live Windows venv instead of renaming its directory.

    Windows refuses to rename a directory while any handle is open inside it: a process whose
    cwd is under the venv, an open file, an Explorer window, a sync client (OneDrive) or a
    scanner. The updater cannot enumerate those holders, and the park rename in
    ``_cut_over_candidate`` failed with ``WinError 5`` in the field on every retry (#93032).
    Mapped executable images do NOT block the rename (proven live on windows-latest), so the
    updater running from the venv was never the problem.

    ``venv\\Scripts\\python.exe`` is a launcher that reads ``home`` from ``pyvenv.cfg`` on every
    start, so atomically replacing that one file redirects every fresh process to the candidate
    generation with no directory rename at all.

    The live venv keeps its own ``site-packages``, so the candidate must stay on the same
    ``major.minor`` line: compiled extensions built for one minor do not import under the next.

    The second return value reports whether the live config still references the candidate
    generation. Callers must preserve that generation if a failed smoke test could not restore
    the original config.
    """
    if current.python_version[:2] != candidate_info.python_version[:2]:
        return False, False, None, (
            f"a Python {_dotted(candidate_info.python_version[:2])} runtime cannot be repointed "
            f"under a {_dotted(current.python_version[:2])} venv's site-packages")
    live_config = live / "pyvenv.cfg"
    candidate_config = candidate / "pyvenv.cfg"
    try:
        original = live_config.read_bytes()
        replacement = candidate_config.read_bytes()
    except OSError as exc:
        return False, False, None, f"could not read venv runtime config: {exc}"

    try:
        _replace_file_atomically(live_config, replacement)
    except OSError as exc:
        return False, False, None, f"could not repoint the existing venv: {exc}"

    try:
        healthy, detail, info = _smoke_candidate_venv(live)
    except Exception as exc:
        healthy, detail, info = False, f"candidate smoke raised: {exc}", None
    if healthy:
        return True, True, info, ""

    try:
        _replace_file_atomically(live_config, original)
    except OSError as rollback_error:
        return (
            False,
            True,
            info,
            "post-cutover smoke failed "
            f"({detail}); runtime-config rollback failed ({rollback_error})",
        )
    return False, False, info, f"post-cutover smoke failed: {detail}"


def _acquire_repair_lock(runtime_root: Path) -> _RepairLock | None:
    """Acquire an OS-held install lock that is released on process exit."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    _make_world_traversable(runtime_root)
    path = runtime_root / _REPAIR_LOCK_NAME
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        _flock(fd, acquire=True)
    except (ImportError, OSError):
        os.close(fd)
        return None
    return _RepairLock(path=path, fd=fd)


def _flock(fd: int, *, acquire: bool) -> None:
    """Non-blocking exclusive lock (or unlock) on *fd*, portable across msvcrt/fcntl."""
    if os.name == "nt":
        import msvcrt
        if acquire and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


def _release_repair_lock(lock: _RepairLock) -> None:
    try:
        with contextlib.suppress(ImportError, OSError):
            _flock(lock.fd, acquire=False)
    finally:
        with contextlib.suppress(OSError):
            os.close(lock.fd)


def _default_live_venv(root: Path) -> Path:
    """Return the venv that runtime repair should target for *root*.

    Managed installs create ``<checkout>/venv``, but uv-default and dev
    checkouts use ``<checkout>/.venv``.  Historically only ``venv`` was
    probed, so a ``.venv`` install linking a vulnerable SQLite returned
    ``not-applicable`` on every ``hermes update`` and stayed on
    journal_mode=DELETE forever — even though the WAL fallback warning
    promises that ``hermes update`` repairs the runtime (issue class:
    2,600x slower ``state.db`` appends under DELETE).

    ``venv`` wins when it holds an interpreter (managed layout takes
    precedence); otherwise fall back to ``.venv`` when that one does.
    When neither has an interpreter, return the ``venv`` path so the
    caller's existing ``not-applicable`` handling fires unchanged.
    """
    primary = root / _VENV_NAME
    if _venv_python(primary).is_file():
        return primary
    fallback = root / _ALT_VENV_NAME
    if _venv_python(fallback).is_file():
        return fallback
    return primary


def _sweep_stale_runtime_backups(
    live: Path,
    *,
    root: Path,
    keep: Path | None = None,
    min_age_seconds: float = 3600.0,
) -> None:
    """Remove leftover ``venv.stale.runtime-*`` backups next to *live*.

    A successful runtime repair parks the previous venv as
    ``<live>.stale.runtime-<token>``; historically nothing ever reclaimed
    those, so each repair leaked a full venv (~1 GB) at the project root
    forever (issue #73109).  On POSIX, deleting the tree is safe even while
    an older process still maps files from it — open FDs and mmaps keep
    their inodes alive; the directory entry is what goes away.

    ``min_age_seconds`` guards against racing a concurrent repair in
    another process: a backup parked seconds ago may still be that
    repair's rollback path, so only clearly-old markers are swept.
    ``keep`` exempts the backup the current repair just created.
    Best-effort: never raises.
    """
    try:
        candidates = list(live.parent.glob(f"{live.name}.stale.runtime-*"))
    except OSError:
        return
    now = time.time()
    for candidate in candidates:
        if keep is not None and candidate == keep:
            continue
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue
        if age < min_age_seconds:
            continue
        _remove_tree(candidate, boundary=root)


def repair_vulnerable_runtime(
    *,
    project_root: Path | None = None,
    venv_dir: Path | None = None,
) -> RuntimeRepairResult:
    """Replace a vulnerable install venv without mutating it in place.

    Every failure before cutover leaves the live venv untouched. POSIX cuts over with
    directory renames and restores the parked venv synchronously on failure; Windows repoints
    the live venv's ``pyvenv.cfg`` instead (any open handle under the venv makes a directory
    rename fail there — #93032) and restores the original config on a failed smoke.

    uv is resolved internally — the pinned binary via ``pm`` (its version
    lives in ``pm/lock.json``; the bytes live in the pm store).  There is
    no foreign-uv path: the lockfile names exactly one uv, and a repair
    that ran on someone else's uv would provision an interpreter outside
    the catalog the pin promises.  A stale python-build-standalone catalog
    is fixed by bumping the uv pin — ``hermes update`` pulls the new
    lockfile before repair runs, so pm has already realized the bumped
    binary by the time this module asks for it.
    """
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    live = Path(venv_dir) if venv_dir is not None else _default_live_venv(root)
    live_python = _venv_python(live)
    if not (root / "pyproject.toml").is_file() or not live_python.is_file():
        return RuntimeRepairResult("not-applicable")

    from pm.ensure import uv as pm_uv

    uv_bin, _uv_env = pm_uv()
    if not uv_bin:
        return RuntimeRepairResult(
            "skipped", "pinned uv unavailable (pm could not realize it)"
        )

    current = probe_sqlite_runtime(live_python)
    if current is None:
        return RuntimeRepairResult(
            "skipped",
            f"could not probe live interpreter {live_python}",
        )
    if not current.wal_reset_vulnerable:
        # The runtime is already fixed — any venv.stale.runtime-* markers
        # next to the live venv are leftovers from a past repair (or from
        # a build predating the post-repair cleanup) and will never be
        # rolled back to. Sweep them so they don't leak ~1 GB each
        # forever (issue #73109). Age-gated to avoid racing an in-flight
        # repair in a sibling process.
        _sweep_stale_runtime_backups(live, root=root)
        return RuntimeRepairResult(
            "safe",
            sqlite_before=current.sqlite_version_string,
            sqlite_after=current.sqlite_version_string,
        )

    runtime_root = _runtime_dir(root)
    lock = _acquire_repair_lock(runtime_root)
    if lock is None:
        detail = "another runtime repair is already in progress"
        print(f"  ⚠ SQLite runtime repair deferred: {detail}")
        return RuntimeRepairResult(
            "skipped",
            detail,
            sqlite_before=current.sqlite_version_string,
        )

    generation: Path | None = None
    candidate: Path | None = None
    try:
        # Re-probe under the install-scoped lock: another updater may have
        # completed the repair while this process was entering the path.
        current = probe_sqlite_runtime(live_python)
        if current is None:
            return RuntimeRepairResult("skipped", "live interpreter probe failed")
        if not current.wal_reset_vulnerable:
            return RuntimeRepairResult(
                "safe",
                sqlite_before=current.sqlite_version_string,
                sqlite_after=current.sqlite_version_string,
            )

        print(
            "  ⚠ Hermes venv links SQLite "
            f"{current.sqlite_version_string}, which has the WAL-reset bug."
        )
        provisioned = _install_safe_python_generation(
            uv_bin,
            project_root=root,
            current=current,
        )
        if provisioned is None:
            return RuntimeRepairResult(
                "failed",
                "could not provision a fixed private Python runtime",
                sqlite_before=current.sqlite_version_string,
            )
        generation, python, candidate_info = provisioned

        try:
            candidate = _stage_candidate_venv(
                uv_bin,
                project_root=root,
                generation=generation,
                python=python,
            )
        except _CandidateStageError as exc:
            # A rejected candidate raises with the child's own diagnosis
            # already cleaned up (#111417/#111497: the reason travels into
            # the result detail, the failure report and the receipt).
            _remove_tree(generation, boundary=managed_python_install_dir(root))
            return RuntimeRepairResult(
                "failed",
                str(exc),
                sqlite_before=current.sqlite_version_string,
                sqlite_after=candidate_info.sqlite_version_string,
            )
        if candidate is None:
            # Legacy/patched staging contract: None means rejected (and
            # already cleaned up) without a reason payload.
            _remove_tree(generation, boundary=managed_python_install_dir(root))
            return RuntimeRepairResult(
                "failed",
                "replacement environment did not pass dependency and import smoke tests",
                sqlite_before=current.sqlite_version_string,
                sqlite_after=candidate_info.sqlite_version_string,
            )

        backup: Path | None = None
        generation_in_use = False
        if platform.system() == "Windows":
            cut_over, generation_in_use, final_info, cutover_detail = (
                _cut_over_windows_runtime_config(
                    candidate, live=live, current=current, candidate_info=candidate_info)
            )
        else:
            cut_over, backup, final_info, cutover_detail = _cut_over_candidate(
                candidate,
                project_root=root,
                live=live,
            )
        if not cut_over:
            if backup is None:
                _remove_tree(candidate, boundary=runtime_root)
                if not generation_in_use:
                    _remove_tree(generation, boundary=managed_python_install_dir(root))
            return RuntimeRepairResult(
                "failed",
                cutover_detail,
                sqlite_before=current.sqlite_version_string,
                sqlite_after=(
                    final_info.sqlite_version_string if final_info is not None else ""
                ),
                backup_venv=backup,
            )

        final_version = (
            final_info.sqlite_version_string
            if final_info is not None
            else candidate_info.sqlite_version_string
        )
        print(
            "  ✓ Managed Python runtime repaired "
            f"(SQLite {current.sqlite_version_string} → {final_version})"
        )
        if backup is not None and backup.exists():
            _remove_tree(backup, boundary=root)
        elif backup is None:
            # Windows: the live venv now points at the generation; the
            # staging venv is spent.
            _remove_tree(candidate, boundary=runtime_root)
        return RuntimeRepairResult(
            "repaired",
            sqlite_before=current.sqlite_version_string,
            sqlite_after=final_version,
            backup_venv=backup,
        )
    finally:
        _release_repair_lock(lock)


# ---------------------------------------------------------------------------
# Legacy stub
# ---------------------------------------------------------------------------


def rebuild_venv(uv_bin: str, venv_dir: Path, python_version: str = "3.11") -> bool:
    return True  # dont remove me. ask ethernet
