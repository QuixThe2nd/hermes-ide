#!/usr/bin/env python3
"""Headless CLI entry for pr_intent_watch ticks (what the timer starts)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def main(argv: list[str] | None = None) -> int:
    _bootstrap_path()

    from hermes_constants import get_hermes_home
    from plugins.pr_intent_watch.core import run_tick

    parser = argparse.ArgumentParser(
        description=(
            "Watch the hermes-ide fork for new PRs and comment an intent "
            "review (objective, not code)."
        )
    )
    parser.add_argument(
        "--config",
        default=str(get_hermes_home() / "config.yaml"),
        help="Path to config.yaml (default: HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run reviews but post nothing and write no state.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        summary = run_tick(
            config_path=Path(args.config), dry_run=bool(args.dry_run)
        )
    except Exception as exc:  # noqa: BLE001 — exit 1 only on the truly unexpected
        print(f"pr-intent-watch: unexpected error: {exc}")
        return 1

    if summary.get("disabled"):
        print("pr-intent-watch: disabled; nothing to do")
    elif summary.get("no_token"):
        print("pr-intent-watch: no GitHub token; skipped")
    elif summary.get("baseline"):
        print(
            "pr-intent-watch: baseline recorded "
            f"new={summary.get('new', 0)} commented=0"
        )
    else:
        print(
            "pr-intent-watch: "
            f"reviewed={summary.get('reviewed', 0)} "
            f"commented={summary.get('commented', 0)} "
            f"skipped={summary.get('skipped', 0)} "
            f"new={summary.get('new', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
