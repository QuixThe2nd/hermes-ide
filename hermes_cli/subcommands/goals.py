"""``hermes goals`` subcommand parser and thin lazy handler."""

from __future__ import annotations

import argparse


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _cmd_goals(args) -> int:
    from hermes_cli.goal_intent import cmd_goals

    return cmd_goals(args)


def build_goals_parser(subparsers) -> None:
    """Attach the ``goals`` group. Handler stays lazy so ``--help`` is cheap."""
    parser = subparsers.add_parser(
        "goals",
        help="Inspect standing goals and shadow intent classifications",
        description="Goal tooling, including the Phase 1 shadow intent report.",
    )
    parser.set_defaults(func=_cmd_goals, goals_parser=parser, goals_command=None)
    subcommands = parser.add_subparsers(dest="goals_command")
    report = subcommands.add_parser(
        "intent-report",
        help="Count shadow classifications and show recent examples",
        description=(
            "Read the local, log-only goal-intent shadow JSONL. No LLM is called "
            "and no reply-path behavior changes."
        ),
    )
    report.add_argument(
        "--since",
        default="7d",
        metavar="DURATION",
        help="Include records newer than this duration, e.g. 30m, 24h, 7d (default: 7d).",
    )
    report.add_argument(
        "--examples", "--limit",
        dest="examples",
        type=_positive_int,
        default=3,
        metavar="N",
        help="Show the newest N examples per label (default: 3).",
    )
