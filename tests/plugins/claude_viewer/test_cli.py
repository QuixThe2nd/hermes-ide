"""CLI surface: ``hermes claude_viewer {status,enable,disable,reconcile}``."""

from __future__ import annotations

import pytest
import yaml

import plugins.claude_viewer.cli as cli
from plugins.claude_viewer.port import FOREIGN, FREE, HEALTHY, PortState




@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


@pytest.fixture
def user_scope(tmp_path, monkeypatch):
    scope = type(
        "Scope",
        (),
        {
            "system": False,
            "unit_dir": tmp_path / "units",
            "systemctl_prefix": ("systemctl", "--user"),
        },
    )()
    monkeypatch.setattr(cli, "detect_install_scope", lambda: scope)
    monkeypatch.setattr(cli, "platform_supported", lambda: True)
    return scope


def _write_config(home, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _probe(value: bool):
    return lambda *a, **kw: type(
        "P", (), {"outcome": "true" if value else "false", "detail": "", "known": True, "as_bool": value}
    )()


def test_status_prints_bind_public_url_unit_and_port(
    hermes_home, user_scope, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "probe_service_is_active", _probe(True))
    monkeypatch.setattr(cli, "probe_service_is_enabled", _probe(True))
    monkeypatch.setattr(
        cli, "probe_port_state", lambda *a, **kw: PortState(FREE, "port is free")
    )

    assert cli.cmd_status() == 0
    out = capsys.readouterr().out
    assert "hermes-claude-viewer.service" in out
    assert "Bind: 0.0.0.0:8787" in out
    assert "Public URL: http://" in out
    assert "Port: free" in out
    assert "Service active:" in out


def test_status_reports_coexisting_viewer_without_failing(
    hermes_home, user_scope, monkeypatch, capsys
):
    _write_config(
        hermes_home, {"delegation": {"claude_viewer": {"port": 9999}}}
    )
    monkeypatch.setattr(cli, "probe_service_is_active", _probe(False))
    monkeypatch.setattr(cli, "probe_service_is_enabled", _probe(False))
    monkeypatch.setattr(
        cli, "probe_port_state", lambda *a, **kw: PortState(FOREIGN, "http status 404")
    )

    assert cli.cmd_status() == 0
    out = capsys.readouterr().out
    assert "Bind: 0.0.0.0:9999" in out
    assert "Port: foreign" in out


def test_reconcile_succeeds_when_viewer_already_running_on_port(
    hermes_home, user_scope, monkeypatch, capsys
):
    """The coexistence rule: an already-running viewer is not an error."""
    monkeypatch.setattr(
        cli,
        "reconcile_viewer_on_load",
        lambda **kw: type(
            "R",
            (),
            {
                "supported": True,
                "scope": user_scope,
                "changed": False,
                "enabled": False,
                "service_active": False,
                "unit_installed": False,
                "port": PortState(
                    FOREIGN,
                    "claude-viewer already running on this port; leaving it in charge",
                ),
                "warnings": (
                    "claude-viewer already running on this port; leaving it in charge",
                ),
                "enabled_known": True,
                "service_active_known": True,
            },
        )(),
    )

    assert cli.cmd_reconcile() == 0
    out = capsys.readouterr().out
    assert "already running" in out


def test_reconcile_exit_zero_when_healthy_viewer_coexists(
    hermes_home, user_scope, monkeypatch, capsys
):
    """Healthy coexistence is a success: no second start, non-failing exit."""
    monkeypatch.setattr(
        cli,
        "reconcile_viewer_on_load",
        lambda **kw: type(
            "R",
            (),
            {
                "supported": True,
                "scope": user_scope,
                "changed": False,
                "enabled": False,
                "service_active": False,
                "unit_installed": False,
                "port": PortState(HEALTHY, "claude-viewer UI served"),
                "warnings": (
                    "claude-viewer already running on this port; leaving it in"
                    " charge (claude-viewer UI served)",
                ),
                "enabled_known": True,
                "service_active_known": True,
            },
        )(),
    )

    assert cli.cmd_reconcile() == 0


def test_reconcile_fails_when_port_free_but_unit_wont_start(
    hermes_home, user_scope, monkeypatch
):
    """A free port with a unit that never comes up IS a failure."""
    monkeypatch.setattr(
        cli,
        "reconcile_viewer_on_load",
        lambda **kw: type(
            "R",
            (),
            {
                "supported": True,
                "scope": user_scope,
                "changed": False,
                "enabled": True,
                "service_active": False,
                "unit_installed": True,
                "port": PortState(FREE, "port is free"),
                "warnings": ("viewer unit enabled but not active",),
                "enabled_known": True,
                "service_active_known": True,
            },
        )(),
    )

    assert cli.cmd_reconcile() == 1


def test_register_cli_wires_all_four_subcommands():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="claude_viewer_command")
    cli.register_cli(sub.add_parser("claude_viewer"))
    args = parser.parse_args(["claude_viewer", "status"])
    assert args.claude_viewer_command == "status"
    for verb in ("enable", "disable", "reconcile"):
        assert (
            parser.parse_args(["claude_viewer", verb]).claude_viewer_command
            == verb
        )


def test_command_handler_dispatch_and_usage():
    import argparse

    ns = argparse.Namespace(claude_viewer_command=None)
    assert cli.claude_viewer_command(ns) == 2
