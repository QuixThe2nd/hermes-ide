"""Profile discoverability on the public CLI after the dashboard web UI
removal (800bf3abc27): ``hermes profile`` stays a top-level command and
every action its dispatcher knows parses."""


import argparse
from pathlib import Path

import pytest

import hermes_cli.main as main_mod
from hermes_cli import profile_cmd
from hermes_cli.profile_cmd import PROFILE_ACTIONS


def _top_level_subparsers(parser) -> argparse._SubParsersAction:
    return next(
        a for a in parser._actions
        if isinstance(a, argparse._SubParsersAction) and a.dest == "command"
    )


def _profile_subparsers(parser) -> argparse._SubParsersAction:
    profile = _top_level_subparsers(parser).choices["profile"]
    return next(a for a in profile._actions if isinstance(a, argparse._SubParsersAction))


def test_profile_is_a_top_level_command():
    parser = main_mod._build_cli_parser()[0]
    assert "profile" in _top_level_subparsers(parser).choices


def test_profile_parser_covers_every_dispatch_action():
    parser = main_mod._build_cli_parser()[0]
    parseable = {None} | set(_profile_subparsers(parser).choices)
    assert set(PROFILE_ACTIONS) <= parseable


def test_profile_list_parses_and_dispatches():
    parser = main_mod._build_cli_parser()[0]
    args = parser.parse_args(["profile", "list"])
    assert args.profile_action == "list"
    assert args.func is main_mod.cmd_profile


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolated home: Path.home() and HERMES_HOME agree on tmp_path/.hermes."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def test_profile_list_output_covers_live_profiles_only(profile_env, capsys):
    """``hermes profile list`` prints one row per live profile — the default
    home plus identity-marked named profiles — marks the active one, and never
    lists a marker-less ghost shell (#95188)."""
    profiles_root = profile_env / ".hermes" / "profiles"
    coder = profiles_root / "coder"
    coder.mkdir(parents=True)
    (coder / "config.yaml").write_text("model:\n  name: test-model\n", encoding="utf-8")
    ghost = profiles_root / "logs-shell"  # valid id, no identity marker
    ghost.mkdir(parents=True)

    profile_cmd._profile_list(argparse.Namespace(profile_action="list"))

    out = capsys.readouterr().out
    assert "Profile" in out  # header row names the columns
    assert "Gateway" in out
    assert "default" in out
    assert "coder" in out
    assert "logs-shell" not in out
    assert "◆" in out  # active-profile marker (default home is active here)
    assert "stopped" in out  # no gateway runs against an isolated home


def test_profile_list_empty_state(tmp_path, monkeypatch, capsys):
    """A home with no profiles yet says so instead of printing a bare table."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))

    profile_cmd._profile_list(argparse.Namespace(profile_action="list"))

    assert "No profiles found." in capsys.readouterr().out
