"""Profile discoverability on the public CLI after the dashboard web UI
removal (800bf3abc27): ``hermes profile`` stays a top-level command and
every action its dispatcher knows parses."""

import argparse

import hermes_cli.main as main_mod
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
