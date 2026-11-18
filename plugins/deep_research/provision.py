"""On-demand worker-profile provisioning for ``delegate_research``.

The decision table — when a worker profile must be left alone, when a
missing one may be cloned (and from where), which names can never be
provisioned, and how a failed create is classified — lives in
``internal/profileprovision`` (Go). This module gathers the runtime facts
(does the profile exist, which profile is active) and pipes each decision
record through ``cmd/profileprovision`` (resolved or built exactly like the
gateway's jevnotices bridge), then executes the returned plan through the
REAL profile-creation code — ``hermes_cli.profiles.create_profile`` — so
provisioning is precisely ``hermes profile create --clone`` with none of
its own filesystem logic.

The tool's availability never depends on this module: a missing worker
profile no longer hides ``delegate_research``; it is provisioned lazily at
``start`` (and best-effort at gateway start), and a failed provisioning
reports a clear error while the tool stays visible.
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
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugins.deep_research")

_BINARY_SOURCES = ("provision.go", "cmd/profileprovision/main.go")

_resolve_lock = threading.Lock()
_resolved_binary: Optional[str] = None


class ProvisionError(Exception):
    """Provisioning failed; ``code`` is the machine-readable error id."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _repo_module_dir() -> Optional[Path]:
    """The internal/profileprovision checkout this tree ships, when present."""
    module_dir = Path(__file__).resolve().parents[2] / "internal" / "profileprovision"
    if all((module_dir / name).is_file() for name in _BINARY_SOURCES):
        return module_dir


def _cached_binary_path(module_dir: Optional[Path]) -> Path:
    """Cache location under HERMES_HOME, keyed by a hash of the Go sources."""
    from hermes_constants import get_hermes_home

    digest = "built"
    if module_dir is not None:
        hasher = hashlib.sha256()
        for name in _BINARY_SOURCES:
            hasher.update((module_dir / name).read_bytes())
        digest = hasher.hexdigest()[:12]
    return get_hermes_home() / "cache" / "profileprovision" / f"profileprovision-{digest}"


def _decision_binary() -> Optional[str]:
    """The cmd/profileprovision binary: cached, or built from the checkout.

    Never raises, and a failure is not pinned — provisioning must degrade
    (see :func:`_decide`), not crash, and a later Go toolchain install
    revives the full decision table without a restart.
    """
    global _resolved_binary
    with _resolve_lock:
        if _resolved_binary is not None:
            return _resolved_binary
        try:
            binary = _cached_binary_path(_repo_module_dir())
            if not (binary.is_file() and os.access(binary, os.X_OK)):
                module_dir = _repo_module_dir()
                if module_dir is None or shutil.which("go") is None:
                    return None
                binary.parent.mkdir(parents=True, exist_ok=True)
                staging = binary.with_suffix(".tmp")
                build = subprocess.run(
                    ["go", "build", "-o", str(staging), "./cmd/profileprovision"],
                    cwd=module_dir, capture_output=True, text=True, timeout=300,
                )
                if build.returncode != 0 or not staging.is_file():
                    logger.debug(
                        "profileprovision build failed (exit %s): %s",
                        build.returncode, (build.stderr or "").strip()[:200],
                    )
                    return None
                os.replace(staging, binary)  # atomic: concurrent builders converge
            _resolved_binary = str(binary)
            return _resolved_binary
        except Exception:  # noqa: BLE001 — resolution must never take provisioning down
            return None


def _decide(record: Dict[str, Any]) -> Dict[str, Any]:
    """One decision record through the Go table; the minimal fallback without it.

    The fallback reproduces every decision the table makes for a well-formed
    name (skip an existing profile, otherwise clone it from the active one);
    invalid names are still caught — later, inside ``create_profile``.
    """
    binary = _decision_binary()
    if binary is not None:
        try:
            run = subprocess.run(
                [binary], input=json.dumps(record), capture_output=True, text=True, timeout=30,
            )
            if run.returncode == 0:
                decision = json.loads(run.stdout)
                if isinstance(decision, dict):
                    return decision
        except Exception:  # noqa: BLE001 — degrade to the fallback decision
            pass
    if record.get("op") == "classify":
        if record.get("race"):
            return {"outcome": "provisioned"}
        return {
            "outcome": "failed",
            "code": "worker_profile_provision_failed",
            "message": f"could not provision worker profile {record.get('name')!r}: "
            f"{record.get('error')}",
        }
    if record.get("exists"):
        return {"action": "skip"}
    return {"action": "create", "clone_config": True, "clone_from": ""}


def ensure_worker_profile(name: str) -> None:
    """Make sure worker profile *name* exists; clone it from the active profile otherwise.

    Raises :class:`ProvisionError` when the profile cannot be provisioned.
    A lost creation race (another process created the same profile between
    the plan and the create) counts as success — the profile exists.
    """
    from hermes_cli.profiles import create_profile, get_active_profile_name, profile_exists

    active = get_active_profile_name()
    plan = _decide(
        {
            "op": "plan",
            "name": name,
            "exists": profile_exists(name),
            "active_profile": active,
            "active_is_named": active not in ("default", "custom"),
        }
    )
    action = plan.get("action")
    if action == "skip":
        return
    if action != "create":
        raise ProvisionError(
            plan.get("code") or "worker_profile_provision_failed",
            plan.get("reason") or f"worker profile {name!r} is not provisionable",
        )
    try:
        create_profile(
            name,
            clone_from=(plan.get("clone_from") or None),
            clone_config=bool(plan.get("clone_config", True)),
        )
    except FileExistsError:
        _raise_unless_provisioned(name, race=True, error="")
    except Exception as exc:  # noqa: BLE001 — classified below, reported as ProvisionError
        _raise_unless_provisioned(name, race=False, error=str(exc), cause=exc)


def _raise_unless_provisioned(name: str, *, race: bool, error: str, cause: Any = None) -> None:
    """Turn a failed create into the Go-classified verdict (or raise it)."""
    verdict = _decide({"op": "classify", "name": name, "race": race, "error": error})
    if verdict.get("outcome") == "provisioned":
        return  # the profile exists — the goal of provisioning is met
    raise ProvisionError(
        verdict.get("code") or "worker_profile_provision_failed",
        verdict.get("message") or f"could not provision worker profile {name!r}: {error}",
    ) from cause


__all__ = ["ProvisionError", "ensure_worker_profile"]
