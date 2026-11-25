"""An external-process (ACP) provider can ship from outside this tree.

The known-provider gate, the binary, the argv and the env var names used to be
spelled out for one vendor, so a profile registered from a plugin died with
"Unknown provider" before any client was built. This registers a provider the
way a standalone package does — before importing ``hermes_cli`` — and walks the
real resolution path.

``copilot-acp`` is the counterweight. The bundled first-party profile was
stripped (#719, db4ffa25327) and the static registry row that survives is
identity residue, not a launch spec: with no profile registered, the retired
launch env vars are unread, the executable is never resolved and credentials
fail closed — no subprocess is spawned. Registering an out-of-tree profile is
the only seam that brings the provider back (381dfb190bf).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import providers
from providers import register_provider
from providers.base import ProviderProfile


class _AcmeACPProfile(ProviderProfile):
    def create_client(self, **kwargs):
        return ("acme-client", kwargs)

    def fetch_models(self, **kwargs):
        return None


register_provider(
    _AcmeACPProfile(
        name="acme-acp",
        aliases=("acme",),
        display_name="Acme ACP",
        base_url="acp://acme",
        auth_type="external_process",
        process_command="acme-cli",
        process_args=("--acp",),
        process_command_env_vars=("ACME_CLI_PATH",),
        process_args_env_var="ACME_ACP_ARGS",
    )
)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("acme-cli", "copilot", "custom-acme"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


@pytest.fixture
def registered(fake_cli):
    """Register profiles for one test and restore the registry afterwards.

    ``register_provider`` is last-writer-wins with no unregister, and the
    process-global registry is shared by every test in this file — the
    fail-closed tests below must not see what a neighbour registered.
    """
    providers._discover_providers()
    snapshot = (dict(providers._REGISTRY), dict(providers._ALIASES), providers._PROVIDER_LIST_CACHE)
    yield register_provider
    providers._REGISTRY.clear()
    providers._REGISTRY.update(snapshot[0])
    providers._ALIASES.clear()
    providers._ALIASES.update(snapshot[1])
    providers._PROVIDER_LIST_CACHE = snapshot[2]


def test_an_out_of_tree_external_process_provider_resolves_end_to_end(fake_cli, monkeypatch):
    from hermes_cli.auth import PROVIDER_REGISTRY, resolve_external_process_provider_credentials, resolve_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    assert PROVIDER_REGISTRY["acme"] is PROVIDER_REGISTRY["acme-acp"]
    assert PROVIDER_REGISTRY["acme-acp"].auth_type == "external_process"
    assert resolve_provider("acme") == "acme-acp"

    creds = resolve_external_process_provider_credentials("acme-acp")
    assert Path(creds["command"]) == fake_cli / "acme-cli"
    assert (creds["args"], creds["api_key"]) == (["--acp"], "acme-acp")

    monkeypatch.setenv("ACME_CLI_PATH", str(fake_cli / "custom-acme"))
    monkeypatch.setenv("ACME_ACP_ARGS", "--acp=true --verbose")
    creds = resolve_external_process_provider_credentials("acme-acp")
    assert (creds["command"], creds["args"]) == (str(fake_cli / "custom-acme"), ["--acp=true", "--verbose"])

    runtime = resolve_runtime_provider(requested="acme", target_model="acme")
    assert (runtime["provider"], runtime["base_url"], runtime["source"]) == ("acme-acp", "acp://acme", "process")


# Env var names carried by the retired first-party CopilotACPProfile. Core no longer reads them;
# they are here to prove that residue alone does not configure anything.
_RETIRED_LAUNCH_ENV = {
    "HERMES_COPILOT_ACP_COMMAND": "copilot",
    "COPILOT_CLI_PATH": "copilot",
    "HERMES_COPILOT_ACP_ARGS": "--acp --stdio",
}


def test_unregistered_copilot_acp_fails_closed_despite_cli_and_env_residue(fake_cli, monkeypatch):
    """Binary on PATH + retired launch env vars + no registered profile → unconfigured.

    #719 ships only ``custom`` first-party and requires unknown providers to
    fail closed; the auth-table row is identity residue, not a launch spec, so
    nothing auto-launches or authenticates."""
    from hermes_cli.auth import AuthError
    from hermes_cli.auth import get_external_process_provider_status, resolve_external_process_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    for var, value in _RETIRED_LAUNCH_ENV.items():
        monkeypatch.setenv(var, value)

    status = get_external_process_provider_status("copilot-acp")
    assert status.get("configured") is False
    assert not status.get("resolved_command"), "no executable may be resolved without a registered profile"

    with pytest.raises(AuthError) as excinfo:
        resolve_external_process_provider_credentials("copilot-acp")
    assert excinfo.value.code == "missing_external_process_cli"

    with pytest.raises(AuthError):
        resolve_runtime_provider(requested="copilot-acp", target_model="x")


def test_registered_copilot_acp_describes_its_own_binary(registered, fake_cli, monkeypatch):
    """The same slug resolves end to end once an out-of-tree profile declares the launch spec —
    including the env var NAMES, which belong to the profile, not to core."""
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    registered(
        ProviderProfile(
            name="copilot-acp",
            display_name="Copilot ACP (user plugin)",
            base_url="acp://copilot",
            auth_type="external_process",
            process_command="copilot",
            process_args=("--acp", "--stdio"),
            process_command_env_vars=("TEST_COPILOT_ACP_BIN",),
            process_args_env_var="TEST_COPILOT_ACP_ARGS",
        )
    )

    creds = resolve_external_process_provider_credentials("copilot-acp")
    assert Path(creds["command"]) == fake_cli / "copilot"
    assert (creds["args"], creds["api_key"], creds["base_url"]) == (["--acp", "--stdio"], "copilot-acp", "acp://copilot")

    monkeypatch.setenv("TEST_COPILOT_ACP_BIN", str(fake_cli / "custom-acme"))
    monkeypatch.setenv("TEST_COPILOT_ACP_ARGS", "--acp=true --verbose")
    creds = resolve_external_process_provider_credentials("copilot-acp")
    assert (creds["command"], creds["args"]) == (str(fake_cli / "custom-acme"), ["--acp=true", "--verbose"])

    # The retired first-party names stay dead even next to a registered profile:
    # they are not read unless the profile declares them.
    monkeypatch.delenv("TEST_COPILOT_ACP_BIN")
    monkeypatch.setenv("COPILOT_CLI_PATH", str(fake_cli / "custom-acme"))
    creds = resolve_external_process_provider_credentials("copilot-acp")
    assert Path(creds["command"]) == fake_cli / "copilot"

    runtime = resolve_runtime_provider(requested="copilot-acp", target_model="x")
    assert (runtime["provider"], runtime["base_url"], runtime["source"]) == ("copilot-acp", "acp://copilot", "process")
