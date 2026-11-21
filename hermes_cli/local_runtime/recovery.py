"""Conservative recovery of the one router recorded by this managed runtime."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def read_state() -> dict:
    from hermes_cli.local_runtime.supervisor import state_path

    try:
        state = json.loads(state_path().read_text(encoding="utf-8-sig"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


_MODERN_FIELDS = ("create_time", "executable", "owner_pid", "owner_create_time")


def is_modern(state: dict) -> bool:
    return any(key in state for key in _MODERN_FIELDS)


def _valid_pid(value) -> bool:
    return type(value) is int and value > 0


def _valid_birth(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def recorded_process(state: dict):
    """Match a modern process incarnation, never just its reusable PID."""
    try:
        pid, created = state["pid"], state["create_time"]
        owner_pid, owner_created = state["owner_pid"], state["owner_create_time"]
        exe = state["executable"]
        if (not _valid_pid(pid) or not _valid_birth(created)
                or not _valid_pid(owner_pid) or not _valid_birth(owner_created)
                or owner_created > created or not isinstance(exe, str) or not exe):
            return None
        proc = psutil.Process(pid)
        if (not proc.is_running() or proc.create_time() != created
                or Path(proc.exe()) != Path(exe)):
            return None
        parent = proc.parent()
        if parent is not None:
            if parent.pid == owner_pid:
                if parent.create_time() != owner_created:
                    return None
            elif not _owner_is_dead(state):
                return None  # the recorded owner may have exited while the router lives on.
        return proc
    except (KeyError, TypeError, ValueError, OverflowError, OSError, psutil.Error):
        return None


def _owner_is_dead(state: dict) -> bool:
    try:
        pid, created = state["owner_pid"], state["owner_create_time"]
        if not _valid_pid(pid) or not _valid_birth(created):
            return False
        try:
            owner = psutil.Process(pid)
            # A newer incarnation proves the recorded owner has exited.
            return owner.create_time() > created or not owner.is_running()
        except psutil.NoSuchProcess:
            return True
    except (KeyError, TypeError, ValueError, OverflowError, OSError, psutil.Error):
        return False



def stop_recorded_orphan() -> bool:
    """Explicit user stop only. Refuse uncertain identity or a living recorded owner."""
    from hermes_cli.local_runtime.supervisor import LlamaServerSupervisor, state_path

    try:
        state = json.loads(state_path().read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return True  # already stopped and no record
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict) or not _valid_pid(state.get("pid")):
        return False
    try:
        if not psutil.pid_exists(state["pid"]):
            return True  # keep the dead record; endpoint resolution rejects it
        if not is_modern(state):
            return False  # legacy records without birth times are never identity-verifiable
        proc = recorded_process(state)
        if proc is None or not _owner_is_dead(state):
            return False
        if read_state() != state or not proc.is_running():
            return False
        # Retain the psutil incarnation object: destructive methods guard PID reuse.
        LlamaServerSupervisor._terminate_tree(proc, verified_root=True)
        proc.wait(timeout=5)
        logger.info("stopped orphaned managed llama-server pid=%s", proc.pid)
        return True
    except (OSError, psutil.Error):
        logger.warning("could not verify or stop recorded managed llama-server")
        return False
