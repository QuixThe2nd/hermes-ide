"""Diagnostics must distinguish policy rejection from inspected job properties."""

import json
import plistlib

from tools.terminal_tool_guards import gateway_lifecycle_block


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert "regardless of the job label" in result["error"]
    assert "does not inspect" in result["error"]
    assert "KeepAlive settings" in result["error"]
    assert "separate shell outside the gateway" in result["error"]


def test_interpreter_kill_rejection_names_the_owned_process_route(tmp_path, monkeypatch):
    """A live gateway loop owns the interpreter-kill guard; inherited launch markers do not."""
    from unittest.mock import MagicMock

    import gateway.run as gateway_run

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    loop = MagicMock()
    loop.is_running.return_value = True
    loop.is_closed.return_value = False
    monkeypatch.setattr(
        gateway_run, "_gateway_runner_ref", lambda: MagicMock(_gateway_loop=loop),
    )

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-test",
        )

    blocked = json.loads(run("taskkill /F /IM python.exe 2>/dev/null | head -2"))
    assert blocked["exit_code"] == 1
    assert "proc_" in blocked["error"] and "explicit PID" in blocked["error"]
    assert "proc_" in json.loads(run("pkill -9 python3"))["error"]
    assert run("taskkill /F /IM agent-browser.exe /T") is None
    assert run("taskkill /F /PID 46544") is None
    # Inherited launch markers remain set, but without a running GatewayRunner loop this is a
    # descendant/foreground process and must not inherit the gateway's lifecycle restrictions.
    loop.is_running.return_value = False
    assert run("pkill -9 python3") is None
