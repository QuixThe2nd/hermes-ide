"""Spawn an overlap successor in the calling process's supervisor cgroup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def spawn_gateway_successor(*, home: str, predecessor_pid: int, preserve_supervisor: bool = False):
    """Start a successor in the caller's cgroup using the hub child environment.

    The Discord hub and a systemd predecessor share this launcher. Only the
    predecessor-local call preserves the gateway unit's cgroup and supervisor
    identity; a hub child must not inherit the hub unit's restart/notify state.
    """
    from gateway.overlap_restart import OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV
    from gateway.restart import detached_restart_spawn_blocked, spawn_replacement_gateway

    blocked = detached_restart_spawn_blocked()
    if blocked:
        raise RuntimeError("test isolation forbids a detached gateway spawn")
    project_root = str(Path(__file__).resolve().parents[3])

    def _popen(argv: list[str], **kwargs: Any):
        import subprocess
        from tools.environments.local import served_profile_child_env

        # Pin the checkout interpreter and site-packages even when the hub and
        # gateway were launched from different virtual environments.
        venv_bin_dir = kwargs.pop("checkout_venv_bin_dir", None)
        env = served_profile_child_env(target_home=home, inherit_credentials=True)
        if venv_bin_dir:
            env["VIRTUAL_ENV"] = str(Path(venv_bin_dir).parent)
            env["PATH"] = venv_bin_dir + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = project_root
        for marker in ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE"):
            env.pop(marker, None)
        if not preserve_supervisor:
            for marker in (
                "INVOCATION_ID", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID",
                "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD",
                "HERMES_GATEWAY_EXTERNAL_SUPERVISOR", "HERMES_SUPERVISED_CHILD",
            ):
                env.pop(marker, None)
        env[OVERLAP_SUCCESSOR_ENV] = "1"
        env[OVERLAP_PREDECESSOR_PID_ENV] = str(predecessor_pid)
        kwargs["env"] = env
        return subprocess.Popen(argv, **kwargs)

    return spawn_replacement_gateway(project_root, popen=_popen)
