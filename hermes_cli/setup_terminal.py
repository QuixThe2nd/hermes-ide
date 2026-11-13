"""Terminal-backend setup wizard (local/ssh/plugin).
setup.py names are resolved through the module object so test patches on ``hermes_cli.setup.<name>``
take effect; setup.py re-exports the public entry points."""

import logging
import os
from pathlib import Path

logger = logging.getLogger("hermes_cli.setup")


def _setup_backend_local(config: dict) -> None:
    _setup.print_success("Terminal backend: Local")
    _setup.print_info("Commands run directly on this machine.")
    # Gateway cwd defaults to home; sudo stays off. Both configurable via `hermes setup terminal`.
    config["terminal"].setdefault("cwd", str(Path.home()))


def _setup_backend_ssh(config: dict) -> None:
    _setup.print_success("Terminal backend: SSH")
    _setup.print_info("Run commands on a remote machine via SSH.")
    # (label, env var, fallback default when .env is empty); the port is only saved when not 22.
    fields = (
        ("  SSH host (hostname or IP)", "TERMINAL_SSH_HOST", ""),
        ("  SSH user", "TERMINAL_SSH_USER", os.getenv("USER", "")),
        ("  SSH port", "TERMINAL_SSH_PORT", "22"),
        ("  SSH private key path", "TERMINAL_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa")))
    values = []
    for label, env_var, default in fields:
        value = _setup.prompt(label, _setup.get_env_value(env_var) or default)
        values.append(value)
        if value and (env_var != "TERMINAL_SSH_PORT" or value != "22"):
            _setup.save_env_value(env_var, value)
        elif env_var == "TERMINAL_SSH_PORT" and value == "22":
            # Answering the default must undo a previously saved non-default port:
            # skipping the save alone would leave the stale value in .env.
            _setup.remove_env_value(env_var)
    host, user, port, ssh_key = values
    if host and _setup.prompt_yes_no("  Test SSH connection?", True):
        _setup.print_info("  Testing connection...")
        import subprocess
        ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", *(["-i", ssh_key] if ssh_key else []),
                   *(["-p", port] if port and port != "22" else []), f"{user}@{host}" if user else host, "echo ok"]
        result = subprocess.run(ssh_cmd, timeout=10, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode == 0:
            _setup.print_success("  SSH connection successful!")
        else:
            _setup.print_warning(f"  SSH connection failed: {result.stderr.strip()}")
            _setup.print_info("  Check your SSH key and host settings.")


def _setup_backend_plugin(config: dict, backend: str) -> None:
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        from agent.terminal_env_registry import get_provider
        provider = get_provider(backend)
        if provider is not None:
            provider.setup(config)
    except Exception as exc:
        _setup.print_warning(f"Backend plugin setup hook failed: {exc}")


_BUILTIN_TERMINAL_BACKENDS = [
    ("local", "Local - run directly on this machine (default)"),
    ("ssh", "SSH - run on a remote machine")]
_TERMINAL_BACKEND_SETUP = {
    "local": _setup_backend_local,
    "ssh": _setup_backend_ssh}


def setup_terminal_backend(config: dict):
    """Configure the terminal execution backend."""
    _setup.print_header("Terminal Backend")
    _setup._info("Choose where Hermes runs shell commands and code.",
                 "This affects tool execution, file access, and isolation.",
                 f"   Guide: {_setup._DOCS_BASE}/user-guide/configuration#terminal-backend-configuration", None)
    current_backend = _setup.cfg_get(config, "terminal", "backend", default="local")
    backends = list(_BUILTIN_TERMINAL_BACKENDS)
    # Plugin-registered backends (~/.hermes/plugins/). Fail-soft: a broken plugin must not take
    # the wizard down.
    plugin_backend_names = []
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent — plugin state may not be loaded yet
        from agent.terminal_env_registry import list_providers
        for provider in list_providers():
            pname = provider.name.strip().lower()
            backends.append((pname, f"{provider.display_name} - {provider.description}"))
            plugin_backend_names.append(pname)
    except Exception:
        pass
    terminal_choices = [label for _, label in backends] + [f"Keep current ({current_backend})"]
    terminal_idx = _setup.prompt_choice("Select terminal backend:", terminal_choices, len(backends))
    if terminal_idx == len(backends):
        _setup.print_info(f"Keeping current backend: {current_backend}")
        return
    # prompt_choice returns a valid index (cancel keeps the default = "Keep current").
    selected_backend = backends[terminal_idx][0]
    config.setdefault("terminal", {})["backend"] = selected_backend
    # Plugin names shadow only the ssh built-in (dispatch order of the original chain).
    handler = _TERMINAL_BACKEND_SETUP.get(selected_backend)
    if handler is not None and (selected_backend != "ssh" or selected_backend not in plugin_backend_names):
        handler(config)
    elif selected_backend in plugin_backend_names:
        _setup_backend_plugin(config, selected_backend)
    _setup.save_env_value("TERMINAL_ENV", selected_backend)
    _setup.save_config(config)
    print()
    _setup.print_success(f"Terminal backend set to: {selected_backend}")


import hermes_cli.setup as _setup  # noqa: E402  (bottom: hermes_cli.setup imports this module)
