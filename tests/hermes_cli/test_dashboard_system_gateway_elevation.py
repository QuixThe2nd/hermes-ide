"""Dashboard gateway lifecycle actions must elevate on a system-scope install.

Regression for #110820: the Restart Gateway button spawned ``hermes gateway restart`` as the
dashboard's own user, and the CLI refuses system-scope verbs below root, so the button could
never work on a systemd *system* install — the refusal only reached the action log while the
endpoint reported a started action.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def unprivileged(monkeypatch):
    """Run the predicate as a non-root user (CI containers can be root)."""
    monkeypatch.setattr("os.geteuid", lambda: 1000)


@pytest.fixture
def system_scope_install(monkeypatch, tmp_path):
    """Only the SYSTEM unit is installed, the shape that needs root."""
    system_unit = tmp_path / "etc" / "hermes-gateway.service"
    system_unit.parent.mkdir(parents=True)
    system_unit.write_text("[Service]\n", encoding="utf-8")
    user_unit = tmp_path / "user" / "hermes-gateway.service"
    monkeypatch.setattr(
        "hermes_cli.gateway.get_systemd_unit_path",
        lambda system=False: system_unit if system else user_unit,
    )
    return system_unit, user_unit


def _spawn(tmp_path, subcommand, *, sudo_ok: bool):
    """Run ``_spawn_hermes_action(subcommand)`` with Popen (and the ``sudo -n true`` probe) faked;
    the elevation decision runs through the production helpers. Returns the attempted argv."""
    from hermes_cli import web_server_gateway

    probe = subprocess.CompletedProcess(["sudo", "-n", "true"], 0 if sudo_ok else 1)
    child = MagicMock(spec=subprocess.Popen, pid=4242)  # spec'd before Popen is patched
    web_server_gateway._ACTION_LOG_FILES.setdefault("probe", "probe.log")
    with patch.object(web_server_gateway, "_ACTION_LOG_DIR", tmp_path / "logs"), patch.object(
        web_server_gateway.subprocess, "run", return_value=probe
    ), patch.object(web_server_gateway.subprocess, "Popen", return_value=child) as popen, patch.object(
        web_server_gateway, "_dashboard_spawn_executable", return_value="/venv/bin/python"
    ), patch("hermes_cli.web_server.PROJECT_ROOT", tmp_path):
        web_server_gateway._spawn_hermes_action(subcommand, "probe")
    return popen.call_args.args[0]


@pytest.mark.parametrize(
    "subcommand, both_units, elevated",
    [
        (["gateway", "restart"], False, True),
        (["-p", "default", "gateway", "start"], False, True),
        (["gateway", "status"], False, False),   # no root gate on status
        (["gateway", "restart"], True, False),    # both units installed -> the CLI picks user scope
    ],
)
def test_only_system_scope_lifecycle_verbs_are_spawned_under_sudo(
    unprivileged, system_scope_install, monkeypatch, tmp_path, subcommand, both_units, elevated
):
    _system_unit, user_unit = system_scope_install
    if both_units:
        user_unit.parent.mkdir(parents=True)
        user_unit.write_text("[Service]\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.web_server_profiles._resolve_profile_dir", lambda name: tmp_path / name)
    argv = _spawn(tmp_path, subcommand, sudo_ok=True)
    plain = ["/venv/bin/python", "-m", "hermes_cli.main", *subcommand]
    assert argv == (["sudo", "-n", *plain] if elevated else plain)


def test_restart_without_passwordless_sudo_fails_the_request(unprivileged, system_scope_install, tmp_path):
    # The endpoint reports Popen success; a child that can only refuse must fail the REQUEST instead.
    with pytest.raises(RuntimeError, match="passwordless sudo is unavailable"):
        _spawn(tmp_path, ["gateway", "restart"], sudo_ok=False)
