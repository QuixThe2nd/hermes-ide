"""Shell-identity snapshot/restore at delegation boundaries.

Delegate children share the parent's terminal environment on purpose (one
shell, one cwd, one set of installed packages): ``_resolve_container_task_id``
collapses the child onto the parent key and ``seed_workspace`` registers the
container alias. The shared bash snapshot
(``tools/environments/base_session_env.py`` re-dumps ``export -p`` after every
command) then persists a child's ``export HOME=... HERMES_HOME=...`` so the
PARENT's next command sources the scratch home — GitHub CLI reads it and asks
for login although the real account is fine. The process ``os.environ`` is not
the leak; the snapshot is.

The guard refcounts the SHARED ENVIRONMENT KEY (what
``_resolve_container_task_id`` returns, never the raw child id): the first
guard captures the identity names below from the live shell registered under
that key (falling back to the process env, recording unset names as absent),
later guards join the capture, and only the LAST exit restores — by running
export/unset on the live shell so the normal per-command re-dump stores the
parent value. The snapshot file is never written by hand.

Concurrency rules:
- ``release()`` is idempotent per guard instance: the worker's with-block
  finally and the adopted-runner finally may both call it.
- An enter that arrives while a restore is in progress waits for it, then
  joins the capture that restore just re-established — it never captures
  mid-restore (it would grab half-restored values).
- Neither the registry lock nor a shared condition is held across an env
  round-trip: capture/restore run with only the phase flag set, so a second
  first-enter waits instead of double-capturing.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Identity names, exactly: tool-config locations a child's scratch home must
# not displace for the parent. Keep in sync with internal/shellidentity (twin).
IDENTITY_ENV_NAMES = ("GH_CONFIG_DIR", "HERMES_HOME", "HOME", "PYTHONPATH", "XDG_CONFIG_HOME")

# Phase markers for _SharedIdentity.phase (None = idle).
_CAPTURE = "capture"
_RESTORE = "restore"


class _SharedIdentity:
    """Refcount + capture state for ONE shared environment key."""

    __slots__ = ("cond", "refcount", "captured", "phase")

    def __init__(self) -> None:
        # Guards every slot; NEVER held across an env round-trip — the phase
        # flag marks that window instead, so waiters block instead of
        # deadlocking on a shell probe.
        self.cond = threading.Condition()
        self.refcount = 0
        self.captured: Optional[Dict[str, Optional[str]]] = None
        self.phase: Optional[str] = None


_SHARED: Dict[str, _SharedIdentity] = {}
_SHARED_LOCK = threading.Lock()


def _shared_for(key: str) -> _SharedIdentity:
    with _SHARED_LOCK:
        state = _SHARED.get(key)
        if state is None:
            state = _SHARED[key] = _SharedIdentity()
        return state


def _live_environment(key: str) -> Any:
    """The live shell registered under *key* in the terminal registry, or None."""
    from tools import terminal_tool
    with terminal_tool._env_lock:
        return terminal_tool._active_environments.get(key)


def _capture_identity(key: str) -> Dict[str, Optional[str]]:
    """Capture from the live shell when one is registered under *key*;
    otherwise from the process env. Unset names are recorded as absent
    (None), never as the empty string."""
    env = _live_environment(key)
    if env is not None:
        captured = None
        try:
            captured = env.capture_shell_identity(IDENTITY_ENV_NAMES)
        except Exception:
            logger.warning(
                "shell identity capture failed on %s; falling back to process env",
                type(env).__name__, exc_info=True)
        if isinstance(captured, dict):
            return {name: captured.get(name) for name in IDENTITY_ENV_NAMES}
    return {name: (os.environ[name] if name in os.environ else None) for name in IDENTITY_ENV_NAMES}


class ShellIdentityGuard:
    """Refcounted capture/restore of the identity names on ONE shared shell.

    ``task_id`` is the delegating child's id; the shared environment is the
    key ``tools.terminal_tool._resolve_container_task_id`` maps it to, so
    nested children and parallel siblings land on the same refcount and only
    the last exit restores. With no live shell under that key at exit, the
    restore is a no-op (nothing observed can have leaked).
    """

    def __init__(self, task_id: Optional[str] = None) -> None:
        from tools.terminal_tool import _resolve_container_task_id
        self._key = _resolve_container_task_id(task_id)
        self._shared = _shared_for(self._key)
        self._entered = False
        self._released = True

    @property
    def environment_key(self) -> str:
        """The shared environment key this guard refcounts."""
        return self._key

    def __enter__(self) -> "ShellIdentityGuard":
        shared = self._shared
        waited_out_restore = False
        capture_now = False
        with shared.cond:
            while shared.phase is not None:
                if shared.phase == _RESTORE:
                    waited_out_restore = True
                shared.cond.wait()
            if shared.refcount > 0:
                shared.refcount += 1
            elif waited_out_restore and shared.captured is not None:
                # A restore just re-established these values; join its capture
                # instead of probing the shell again mid-transition.
                shared.refcount = 1
            else:
                shared.phase = _CAPTURE
                capture_now = True
            # Set before publishing any refcount a concurrent release of THIS
            # instance could observe; release() reads them under the same cond.
            self._entered, self._released = True, False
        if capture_now:
            captured = _capture_identity(self._key)
            with shared.cond:
                shared.captured = captured
                shared.refcount = 1
                shared.phase = None
                shared.cond.notify_all()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def release(self) -> None:
        """Drop this guard's reference; the LAST exit restores the captured
        identity on the live shell. Idempotent per instance — a second call
        (worker finally + adopted-runner finally) never decrements twice."""
        shared = self._shared
        with shared.cond:
            if not self._entered or self._released:
                return
            self._released = True
            if shared.refcount <= 0:
                return
            shared.refcount -= 1
            if shared.refcount > 0:
                return
            captured = shared.captured
            shared.phase = _RESTORE
        env = _live_environment(self._key)
        if env is not None and captured is not None:
            try:
                env.restore_shell_identity(captured)
            except Exception:
                logger.warning(
                    "shell identity restore failed on %s; the shared snapshot may keep child values",
                    type(env).__name__, exc_info=True)
        with shared.cond:
            shared.phase = None
            shared.cond.notify_all()
