"""Default ``inbox-sparks`` cron job seeding for the hermes_starts plugin.

The job spec and the create/skip decision live in the Go module
``internal/sparkscron`` (``cmd/sparkscron`` binary, stdin JSON → stdout JSON)
— this module contains no copy of either. It gathers per-profile facts,
executes the verdict, and records the seeding marker, mirroring the
``internal/jevnotices`` bridge arrangement: the binary is resolved once per
process from ``$HERMES_HOME/cache/sparkscron``, built from the repo checkout
with the local Go toolchain when missing (content-hashed filename, so a tree
update can never serve a stale spec). When no binary can be produced (no Go
toolchain, build failure) seeding is skipped with one log line — an absent
default job is the safe failure; a wrong one is not.

Fires from the plugin's ``on_gateway_start`` hook (the generic startup
surface) in a daemon thread: a cold ``go build`` can take minutes and must
never block ``GatewayRunner.start()``. Everything the ensure touches is
per-profile-scoped — home override + ``use_cron_store`` — so under
``gateway.multiplex_profiles`` each served profile's own job store gets its
own seed, and a secondary profile's store is never written from another
profile's context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)

logger = logging.getLogger(__name__)

# Marker recorded after THIS seeder creates the job, so a user deletion is
# final: seeding never recreates what the user removed.
_MARKER_FILENAME = "sparks_cron_seeded"
_BINARY_SOURCES = ("spec.go", "decide.go", "cmd/sparkscron/main.go")
_BUILD_TIMEOUT_SECONDS = 180

_binary_lock = threading.Lock()
_binary_cache: Optional[str] = None


def _repo_module_dir() -> Optional[Path]:
    """The ``internal/sparkscron`` checkout this tree ships, when present."""
    module_dir = Path(__file__).resolve().parents[2] / "internal" / "sparkscron"
    if all((module_dir / name).is_file() for name in _BINARY_SOURCES):
        return module_dir
    return None


def _cached_binary_path() -> Path:
    """Cache location keyed by a hash of the Go sources (staleness-proof)."""
    module_dir = _repo_module_dir()
    digest = "built"
    if module_dir is not None:
        hasher = hashlib.sha256()
        for name in _BINARY_SOURCES:
            hasher.update((module_dir / name).read_bytes())
        digest = hasher.hexdigest()[:12]
    return get_hermes_home() / "cache" / "sparkscron" / f"sparkscron-{digest}"


def _go_binary() -> Optional[str]:
    """A usable ``cmd/sparkscron`` binary: cached, or built from the checkout.

    Never raises; every failure path returns None (seeding skipped).
    """
    global _binary_cache
    with _binary_lock:
        if _binary_cache is not None:
            return _binary_cache
        try:
            binary = _cached_binary_path()
            if binary.is_file() and os.access(binary, os.X_OK):
                _binary_cache = str(binary)
                return _binary_cache
        except Exception:
            return None
        module_dir = _repo_module_dir()
        if module_dir is None or shutil.which("go") is None:
            logger.warning(
                "inbox-sparks default cron job not seeded: no sparkscron binary "
                "and no Go toolchain to build one (retried on next gateway start)"
            )
            return None
        try:
            binary.parent.mkdir(parents=True, exist_ok=True)
            staging = binary.with_suffix(".tmp")
            build = subprocess.run(
                ["go", "build", "-o", str(staging), "./cmd/sparkscron"],
                cwd=module_dir, capture_output=True, text=True,
                timeout=_BUILD_TIMEOUT_SECONDS,
            )
            if build.returncode == 0 and staging.is_file():
                os.replace(staging, binary)  # atomic: concurrent gateways converge
                _binary_cache = str(binary)
                return _binary_cache
            logger.warning(
                "sparkscron build failed (go build exit %s); inbox-sparks default "
                "cron job not seeded this start", build.returncode,
            )
        except Exception as exc:
            logger.warning("sparkscron build failed: %s", exc)
        return None


def _decide(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One verdict from the Go decision owner, or None when unavailable."""
    binary = _go_binary()
    if binary is None:
        return None
    run = subprocess.run(
        [binary], input=json.dumps(request),
        capture_output=True, text=True, timeout=30,
    )
    if run.returncode != 0:
        logger.warning(
            "sparkscron exited %s: %s", run.returncode, run.stderr.strip()
        )
        return None
    verdict = json.loads(run.stdout)
    return verdict if isinstance(verdict, dict) else None


def _marker_path(home: Path) -> Path:
    return home / "hermes_starts" / _MARKER_FILENAME


def _write_marker(home: Path) -> None:
    path = _marker_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text("", encoding="utf-8")
    os.replace(staging, path)
    os.chmod(path, 0o600)


def _sparks_cron_enabled() -> bool:
    """``plugins.entries.hermes_starts.settings.sparks_cron`` (default true),
    read for the home currently scoped by the caller."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return True
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("hermes_starts") or {}
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return True
    value = settings.get("sparks_cron")
    return True if value is None else bool(value)


def _seed_homes() -> List[Path]:
    """Homes to seed: the launch home, plus every served profile under
    ``gateway.multiplex_profiles`` (mirroring the cron ticker's set)."""
    try:
        from hermes_cli.profiles import profiles_to_serve

        gateway_cfg: Dict[str, Any] = {}
        try:
            from hermes_cli.config import load_config_readonly

            gateway_cfg = (load_config_readonly() or {}).get("gateway") or {}
        except Exception:
            pass
        launch = get_hermes_home()
        if not gateway_cfg.get("multiplex_profiles"):
            return [launch]
        homes = [home for _name, home in profiles_to_serve(
            multiplex=True,
            profile_allowlist=gateway_cfg.get("multiplex_profile_allowlist"),
        )]
        resolved = {home.resolve() for home in homes}
        if launch.resolve() not in resolved:
            homes.append(launch)
        return homes
    except Exception:
        logger.warning("sparkscron profile enumeration failed", exc_info=True)
        return [get_hermes_home()]


def _ensure_for_home(home: Path, prereq: Callable[[], bool]) -> str:
    """Seed ``home``'s own store; returns the outcome for logging."""
    from cron.jobs import create_job, list_jobs, use_cron_store

    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            request = {
                "job_names": [str(job.get("name") or "") for job in list_jobs(include_disabled=True)],
                "seeded": _marker_path(home).exists(),
                "sparks_cron": _sparks_cron_enabled(),
                "prereq_ok": bool(prereq()),
            }
            verdict = _decide(request)
            if verdict is None or verdict.get("action") != "create":
                return str((verdict or {}).get("reason") or "bridge-unavailable")
            spec = verdict.get("job") or {}
            create_job(
                prompt=str(spec.get("prompt") or ""),
                schedule=str(spec.get("schedule") or ""),
                name=str(spec.get("name") or ""),
                deliver=str(spec.get("deliver") or ""),
                skills=list(spec.get("skills") or []),
                enabled_toolsets=list(spec.get("enabled_toolsets") or []),
            )
            _write_marker(home)
            return "created"
    finally:
        reset_hermes_home_override(token)


def ensure_sparks_cron_jobs(prereq: Callable[[], bool]) -> None:
    """Run one idempotent seeding pass over every home this process serves.

    ``prereq`` answers, for the currently scoped home, whether the plugin's
    prerequisite holds (a usable Discord bot token). Never raises: a failed
    pass logs and leaves the stores untouched.
    """
    for home in _seed_homes():
        try:
            outcome = _ensure_for_home(home, prereq)
        except Exception:
            logger.warning(
                "inbox-sparks seeding failed for %s", home, exc_info=True
            )
            continue
        if outcome == "created":
            logger.info("Seeded default inbox-sparks cron job in %s", home)


def start_ensure_thread(prereq: Callable[[], bool]) -> None:
    """Kick the seeding pass off the gateway startup path."""

    def _run() -> None:
        try:
            ensure_sparks_cron_jobs(prereq)
        except Exception:
            logger.warning("inbox-sparks seeding thread failed", exc_info=True)

    threading.Thread(
        target=_run, daemon=True, name="hermes-starts-sparks-cron"
    ).start()
