"""Idempotent reconcile behavior for the bundled claude-viewer service."""

from __future__ import annotations

from pathlib import Path

import pytest

import plugins.claude_viewer.systemd as sd
from plugins.claude_viewer.config import load_claude_viewer_config
from plugins.claude_viewer.port import FOREIGN, FREE, HEALTHY, PortState, port_in_use, probe_port_state
from plugins.claude_viewer.systemd import (
    SERVICE_NAME,
    build_exec_start_argv,
    reconcile_service,
    render_service_unit,
    service_unit_path,
)

CFG = {
    "enabled": True,
    "bind": "0.0.0.0",
    "port": 8787,
    "public_host": "",
    "extra_hosts": [],
}


class _Probe:
    """Minimal stand-in for a systemd ProbeResult."""

    def __init__(self, value: bool):
        self.outcome = "true" if value else "false"
        self.detail = ""
        self.known = True
        self.as_bool = value


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """User-scoped install rooted in tmp_path, with systemctl fully stubbed."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    scope = type(
        "Scope",
        (),
        {
            "system": False,
            "unit_dir": unit_dir,
            "systemctl_prefix": ("systemctl", "--user"),
        },
    )()

    monkeypatch.setattr(sd, "platform_supported", lambda: True)
    monkeypatch.setattr(sd, "detect_install_scope", lambda: scope)
    return scope


@pytest.fixture
def calls(monkeypatch):
    """Record every systemctl invocation, answering verbs realistically."""
    seen: list[list[str]] = []

    def runner(args):
        argv = list(args)
        seen.append(argv)
        verb = argv[2] if len(argv) > 2 else ""
        if verb == "is-enabled":
            return 0, "enabled\n", ""
        if verb == "is-active":
            return 0, "active\n", ""
        return 0, "", ""

    monkeypatch.setattr(sd, "default_systemctl_runner", runner)
    return seen


def _mutating(calls) -> list[list[str]]:
    """systemctl calls that change unit state (as opposed to state probes)."""
    verbs = {"daemon-reload", "enable", "disable", "start", "stop"}
    return [c for c in calls if any(v in verbs for v in c)]


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


@pytest.fixture
def port_free(monkeypatch):
    monkeypatch.setattr(
        sd, "probe_port_state", lambda *a, **kw: PortState(FREE, "port is free")
    )


def test_reconcile_installs_and_starts(scope, calls, hermes_home, port_free):
    result = reconcile_service(CFG, enabled=True)

    assert result.supported is True
    assert result.unit_installed is True
    assert result.enabled is True
    assert result.service_active is True
    assert service_unit_path(scope).is_file()
    unit = service_unit_path(scope).read_text(encoding="utf-8")
    assert "ExecStart=" in unit
    assert "Restart=on-failure" in unit
    assert "claude-runs" in unit
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", SERVICE_NAME] in calls


def test_reconcile_is_idempotent(scope, calls, hermes_home, port_free, monkeypatch):
    first = reconcile_service(CFG, enabled=True)
    body_after_first = service_unit_path(scope).read_text(encoding="utf-8")
    second = reconcile_service(CFG, enabled=True)

    assert first.changed is True
    assert second.changed is False
    assert service_unit_path(scope).read_text(encoding="utf-8") == body_after_first
    assert len([c for c in calls if "daemon-reload" in c]) == 1


def test_reconcile_port_already_bound_healthy_starts_nothing(
    scope, calls, hermes_home, monkeypatch
):
    """A viewer already serving the port wins; reconcile must not race it."""
    monkeypatch.setattr(
        sd, "probe_port_state", lambda *a, **kw: PortState(HEALTHY, "claude-viewer UI served")
    )

    result = reconcile_service(CFG, enabled=True)

    # No unit written, no state-changing systemctl call, and no exception.
    assert not service_unit_path(scope).exists()
    assert result.unit_installed is False
    assert result.port.healthy is True
    assert _mutating(calls) == []
    assert any("already running" in w for w in result.warnings)


def test_reconcile_port_already_bound_foreign_never_raises(
    scope, calls, hermes_home, monkeypatch
):
    """A foreign listener must stand down quietly, not fail gateway start."""
    monkeypatch.setattr(
        sd, "probe_port_state", lambda *a, **kw: PortState(FOREIGN, "http status 404")
    )

    result = reconcile_service(CFG, enabled=True)

    assert not service_unit_path(scope).exists()
    assert result.port.occupied is True
    assert result.port.healthy is False
    assert _mutating(calls) == []
    assert any("not starting a second" in w for w in result.warnings)


def test_port_in_use_true_when_bind_fails():
    """Bind a real socket, then confirm the probe reports the port occupied."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        assert port_in_use(port, bind="127.0.0.1") is True
    finally:
        sock.close()


def test_port_in_use_false_when_free():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert port_in_use(port, bind="127.0.0.1") is False


def test_probe_port_state_classifies_foreign_listener(monkeypatch):
    monkeypatch.setattr(
        "plugins.claude_viewer.port.port_in_use", lambda port, bind=None: True
    )
    monkeypatch.setattr(
        "plugins.claude_viewer.port.probe_viewer_health",
        lambda port, **kw: (False, "http status 500"),
    )
    state = probe_port_state(8787)
    assert state.status == FOREIGN
    assert state.occupied and not state.healthy


def test_disable_stops_and_disables_without_enabling(
    scope, calls, hermes_home, monkeypatch
):
    monkeypatch.setattr(
        sd, "probe_port_state", lambda *a, **kw: PortState(FREE, "port is free")
    )
    _false = lambda *a, **kw: _Probe(False)
    monkeypatch.setattr(sd, "probe_service_is_enabled", _false)
    monkeypatch.setattr(sd, "probe_service_is_active", _false)

    result = reconcile_service(CFG, enabled=False)

    assert result.enabled is False
    assert result.service_active is False
    verbs = [c[2] for c in calls if len(c) >= 3]
    assert "stop" in verbs
    assert "disable" in verbs
    assert not any("enable" in c for c in calls)


def test_reconcile_unsupported_platform_is_reported_not_raised(
    scope, hermes_home, monkeypatch
):
    monkeypatch.setattr(sd, "platform_supported", lambda: False)
    result = reconcile_service(CFG, enabled=True)
    assert result.supported is False
    assert result.warnings == ()


def test_exec_start_uses_bundled_server_and_hermes_log_dir(hermes_home):
    argv = build_exec_start_argv(CFG, hermes_home=hermes_home)
    assert argv[1].endswith("viewer/server.py")
    assert Path(argv[1]).is_file()
    assert argv[argv.index("--bind") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8787"
    assert argv[argv.index("--log-dir") + 1] == str(hermes_home / "claude-runs")


def test_unit_documented_coexistence_note():
    unit = render_service_unit(
        hermes_home="/tmp/hermes",
        exec_start=["/usr/bin/python3", "/x/server.py"],
    )
    # The stand-down rule has to be readable from the unit itself.
    assert "Coexistence" in unit
    assert "claude-viewer.service" in unit
    assert "Restart=on-failure" in unit


def test_lifecycle_hook_returns_none_on_platform_without_systemd(monkeypatch):
    import plugins.claude_viewer.lifecycle as lc

    monkeypatch.setattr(lc, "platform_supported", lambda: False)
    assert lc.reconcile_viewer_on_load() is None


def test_lifecycle_hook_swallows_reconcile_exceptions(monkeypatch):
    import plugins.claude_viewer.lifecycle as lc

    monkeypatch.setattr(lc, "platform_supported", lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError("systemd exploded")

    monkeypatch.setattr(lc, "reconcile_service", _boom)
    assert lc.reconcile_viewer_on_load() is None


def test_load_config_reads_delegation_claude_viewer(tmp_path, monkeypatch):
    import yaml

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "delegation": {
                    "claude_viewer": {
                        "bind": "127.0.0.1",
                        "port": 9999,
                        "public_host": "viewer.lan",
                        "extra_hosts": ["a.lan"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_claude_viewer_config()
    assert cfg["bind"] == "127.0.0.1"
    assert cfg["port"] == 9999
    assert cfg["public_host"] == "viewer.lan"
    assert cfg["extra_hosts"] == ["a.lan"]
    assert cfg["enabled"] is True
