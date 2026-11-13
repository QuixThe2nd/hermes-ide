"""Execution-environment backends for the terminal tool: per-backend builders, config-to-kwargs
shapers, and requirement checkers, routed by dispatch/spec tables. Split out of ``tools/terminal_tool.py``;
the terminal tool imports the builders/checkers it uses from here."""

import functools
import logging
from typing import Any, Dict, Optional

from tools.environments.local import LocalEnvironment as _LocalEnvironment
from tools.environments.ssh import SSHEnvironment as _SSHEnvironment
from tools.terminal_tool_config import _get_plugin_env_provider

# Log-record parity with the origin module.
logger = logging.getLogger("tools.terminal_tool")

# Human reason for the most recent failed requirements check (None after a passing one). The CLI
# startup notice and `hermes doctor` read it through terminal_backend_unavailable_reason() so the user
# hears WHY the terminal tool is missing instead of discovering it on first use.
_last_unavailable_reason: Optional[str] = None


def _reject(reason: str) -> bool:
    """Log *reason*, remember it for the user-facing notice, and return False for the checker."""
    global _last_unavailable_reason
    _last_unavailable_reason = reason
    logger.error(reason)
    return False


def _record_unavailable_reason(reason: Optional[str]) -> None:
    """Store (or clear) the last failure reason without logging; used by check_terminal_requirements."""
    global _last_unavailable_reason
    _last_unavailable_reason = reason


def terminal_backend_unavailable_reason() -> Optional[str]:
    """Why the last terminal requirements check failed, in plain words; None when it passed."""
    return _last_unavailable_reason


_BUILTIN_BACKENDS = "local, ssh"

# Config -> kwargs shapers, driven by (out_key, config_key, default) tables.
_SSH_KEYS = (("host", "ssh_host", ""), ("user", "ssh_user", ""), ("port", "ssh_port", 22),
             ("key", "ssh_key", ""), ("persistent", "ssh_persistent", False))


def _ssh_config_from_config(config: Dict[str, Any]) -> dict:
    """``ssh_config`` for :func:`_create_environment` (shared with the lazy ``ensure_task_env``)."""
    return {out: config.get(key, default) for out, key, default in _SSH_KEYS}


# --- Environment builders. Signature: (*, image, cwd, timeout, cc, task_id, ssh_config, host_cwd)
# (env_type is only forwarded to the plugin-registry fallback, not to the built-in builders.)
def _build_local_env(*, cwd, timeout, **_):
    return _LocalEnvironment(cwd=cwd, timeout=timeout)


def _build_ssh_env(*, cwd, timeout, ssh_config, probe_only=False, **_):
    if not ssh_config or not ssh_config.get("host") or not ssh_config.get("user"):
        raise ValueError("SSH environment requires ssh_host and ssh_user to be configured")
    return _SSHEnvironment(host=ssh_config["host"], user=ssh_config["user"], port=ssh_config.get("port", 22),
                           key_path=ssh_config.get("key", ""), cwd=cwd, timeout=timeout, probe_only=probe_only)


def _build_plugin_env(*, env_type, image, cwd, timeout, cc, task_id, **_):
    provider = _get_plugin_env_provider(env_type)
    if provider is not None:
        env_obj = provider.create_environment(cwd=cwd, timeout=timeout, task_id=task_id, image=image,
                                              container_config=cc)
        # Stamp the backend name so path-resolution and progress surfaces can identify plugin
        # backends without class-name sniffing. Test doubles may reject attributes.
        try:
            env_obj._hermes_backend_name = provider.name.strip().lower()
        except AttributeError:
            pass
        return env_obj
    try:
        from agent.terminal_env_registry import plugin_backend_names
        plugin_names = plugin_backend_names()
    except Exception:
        plugin_names = []
    known = ", ".join(f"'{n}'" for n in _BUILTIN_BACKENDS.split(", ") + list(plugin_names))
    raise ValueError(f"Unknown environment type: {env_type}. Use {known}")


# Built-in backend -> builder. Anything else is looked up in the plugin registry.
_ENV_BUILDERS = {"local": _build_local_env, "ssh": _build_ssh_env}


def _create_environment(env_type: str, image: str, cwd: str, timeout: int,
                        ssh_config: dict = None, container_config: dict = None,
                        local_config: dict = None, task_id: str = "default",
                        host_cwd: Optional[str] = None, probe_only: bool = False):
    """Create an execution environment (instance with ``execute()``) for *env_type*. ``image`` and
    ``container_config`` only matter to plugin backends; ``probe_only`` asks ssh for a throwaway
    connection with no remote setup/sync (the prompt-time probe). Unknown types fall through to
    plugin backends."""
    builder = _ENV_BUILDERS.get(env_type)
    kwargs = dict(image=image, cwd=cwd, timeout=timeout, cc=container_config or {}, task_id=task_id,
                  ssh_config=ssh_config, host_cwd=host_cwd, probe_only=probe_only)
    if builder is not None:
        env = builder(**kwargs)
    else:
        env = _build_plugin_env(env_type=env_type, **kwargs)
    # Backend tag for consumers that only hold the instance (cwd sanitizers on
    # live cached envs); __slots__ plugin providers simply keep going untagged.
    try:
        env.env_type = env_type
    except Exception:
        pass
    return env


# --- Requirement checkers: spec tables; optional ``pre(config)`` returns True (satisfied) /
# False (rejected, already logged) / None (continue -> satisfied).
def _ssh_pre(config: Dict[str, Any]) -> bool:
    if config.get("ssh_host") and config.get("ssh_user"):
        return True
    return _reject("the SSH host and user are not configured (TERMINAL_SSH_HOST / TERMINAL_SSH_USER); "
                   "run `hermes setup terminal` to enter them or pick the 'local' backend")


_BACKEND_SPECS: Dict[str, Dict[str, Any]] = {
    "local": {},
    "ssh": {"pre": _ssh_pre},
}


def _check_requirements(env_type: str, config: Dict[str, Any]) -> bool:
    _record_unavailable_reason(None)
    spec = _BACKEND_SPECS[env_type]
    verdict = spec["pre"](config) if "pre" in spec else None
    return True if verdict is None else verdict


def _check_plugin_requirements(config: Dict[str, Any]) -> bool:
    _record_unavailable_reason(None)
    env_type = config["env_type"]
    provider = _get_plugin_env_provider(env_type)
    if provider is not None:
        ok = bool(provider.check_requirements(config))
        return ok or _reject(f"the {env_type!r} plugin backend reported its requirements are not met")
    return _reject(f"Unknown TERMINAL_ENV '{env_type}'. Use one of: {_BUILTIN_BACKENDS}, or a plugin-registered backend.")


# Built-in backend -> requirements checker; unknown backends go to the plugin registry.
_REQUIREMENT_CHECKERS = {name: functools.partial(_check_requirements, name) for name in _BACKEND_SPECS}
