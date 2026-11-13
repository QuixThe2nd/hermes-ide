"""Contract for the unused-terminal-backend strip.

The owner runs ``terminal.backend: local`` (plus occasional ssh). The
docker / modal (incl. managed) / daytona / singularity / vercel_sandbox
backends, their SDK modules, and their config keys were removed. These
tests pin that removal and the compatibility promise made with it:

* the environment modules are gone (not merely unregistered);
* the built-in backend surface is exactly ``local`` + ``ssh`` (plugins
  still extend it through the registry fallback);
* ``terminal`` config defaults carry none of the removed backend keys;
* a live ``config.yaml`` that still carries those legacy keys LOADS —
  the strip must not brick an existing deployment.
"""

import importlib.util

import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from tools.terminal_tool_backends import _BACKEND_SPECS, _ENV_BUILDERS

_REMOVED_MODULES = (
    "tools.environments.docker",
    "tools.environments.docker_egress",
    "tools.environments.modal",
    "tools.environments.managed_modal",
    "tools.environments.modal_utils",
    "tools.environments.daytona",
    "tools.environments.singularity",
    "tools.environments.vercel_sandbox",
)

_REMOVED_CONFIG_KEYS = (
    "docker_image", "docker_forward_env", "docker_env", "docker_volumes",
    "docker_mount_cwd_to_workspace", "docker_network", "docker_extra_args",
    "docker_shm_size", "docker_run_as_host_user", "docker_snap_compat",
    "docker_persist_across_processes", "docker_shared_container_key",
    "docker_orphan_reaper", "singularity_image", "modal_image", "modal_mode",
    "daytona_image", "vercel_runtime", "container_cpu", "container_memory",
    "container_disk", "container_persistent",
)


@pytest.mark.parametrize("module", _REMOVED_MODULES)
def test_removed_backend_module_is_gone(module):
    assert importlib.util.find_spec(module) is None, (
        f"{module} still exists; the backend was supposed to be stripped"
    )


def test_builtin_backend_surface_is_local_and_ssh():
    assert set(_ENV_BUILDERS) == {"local", "ssh"}
    assert set(_BACKEND_SPECS) == {"local", "ssh"}


def test_terminal_defaults_carry_no_removed_backend_keys():
    terminal_defaults: dict = DEFAULT_CONFIG["terminal"]
    leaked = sorted(set(_REMOVED_CONFIG_KEYS) & set(terminal_defaults))
    assert not leaked, f"removed backend keys still defaulted: {leaked}"


def test_legacy_config_keys_still_load(tmp_path, monkeypatch):
    """A pre-strip config.yaml with removed backend keys must keep loading."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "terminal:\n"
        "  backend: local\n"
        "  docker_image: nikolaik/python-nodejs:python3.11-nodejs20\n"
        "  modal_image: nikolaik/python-nodejs:python3.11-nodejs20\n"
        "  daytona_image: nikolaik/python-nodejs:python3.11-nodejs20\n"
        "  singularity_image: docker://nikolaik/python-nodejs:python3.11-nodejs20\n"
        "  modal_mode: direct\n"
        "  container_cpu: 2\n"
        "  container_memory: 4096\n"
        "  container_disk: 10240\n"
        "  container_persistent: false\n"
        "  docker_volumes: ['/tmp/x:/x']\n"
        "  vercel_runtime: node22\n"
        "  ssh_host: example.com\n"
        "  ssh_user: me\n",
        encoding="utf-8",
    )
    from hermes_cli.config import load_config

    config = load_config()
    assert config["terminal"]["backend"] == "local"
    assert config["terminal"]["ssh_host"] == "example.com"


def test_terminal_tool_imports_and_resolves_local_backend():
    import tools.terminal_tool as terminal_tool

    assert terminal_tool._create_environment is not None
    assert tools_env_is_local_supported()


def tools_env_is_local_supported() -> bool:
    from tools.terminal_tool_backends import _REQUIREMENT_CHECKERS

    return callable(_REQUIREMENT_CHECKERS["local"])
