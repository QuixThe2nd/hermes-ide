"""Terminal lifecycle protection follows the running gateway, not its PID file."""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block


def _run_guard(command: str, tmp_path) -> str | None:
    return gateway_lifecycle_block(
        command=command,
        env=None,
        env_type="local",
        cwd=str(tmp_path),
        workdir=None,
        session_key="terminal-lifecycle-test",
    )


@pytest.fixture
def running_gateway_loop(monkeypatch):
    """Simulate an overlap successor serving before it owns ``gateway.pid``."""
    import gateway.run as gateway_run

    loop = MagicMock()
    loop.is_running.return_value = True
    loop.is_closed.return_value = False
    runner = SimpleNamespace(_gateway_loop=loop)
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kwargs: 999999)
    return loop


@pytest.mark.parametrize(
    "command",
    [
        "systemd-run --unit=x --collect /opt/venv/bin/hermes gateway restart",
        "systemd-run --working-directory /tmp /opt/venv/bin/hermes gateway stop",
        "nohup /opt/venv/bin/hermes gateway restart",
        "setsid /opt/venv/bin/hermes gateway stop",
        "sudo -n /opt/venv/bin/hermes gateway restart",
        "env HERMES_PROFILE=default /opt/venv/bin/hermes gateway stop",
        "timeout 30s /opt/venv/bin/hermes gateway restart",
        "nohup /opt/venv/bin/hermes gateway restart >/tmp/restart.log 2>&1 &",
    ],
)
def test_wrapped_gateway_lifecycle_is_refused_in_any_running_gateway(
    command, tmp_path, running_gateway_loop
):
    blocked = _run_guard(command, tmp_path)

    assert blocked is not None, command
    assert json.loads(blocked)["exit_code"] == 1
    assert "Blocked" in json.loads(blocked)["error"]


def test_running_loop_arms_guard_without_environment_or_pid_ownership(
    tmp_path, running_gateway_loop
):
    blocked = _run_guard("/opt/venv/bin/hermes gateway restart", tmp_path)

    assert blocked is not None
    assert json.loads(blocked)["exit_code"] == 1


def test_inherited_gateway_environment_does_not_arm_guard(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("INVOCATION_ID", "inherited-by-terminal-child")
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    # Even PID-file ownership cannot make an inherited environment marker proof
    # that this process is running GatewayRunner's loop.
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kwargs: os.getpid())

    assert _run_guard("/opt/venv/bin/hermes gateway restart", tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat /opt/runbooks/hermes-gateway-restart.md",
        "printf '%s\\n' 'See /opt/venv/bin/hermes gateway restart-notes.md'",
        "/opt/venv/bin/hermes gateway status",
    ],
)
def test_read_only_mentions_remain_allowed(command, tmp_path, running_gateway_loop):
    assert _run_guard(command, tmp_path) is None
