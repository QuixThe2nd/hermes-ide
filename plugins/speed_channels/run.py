#!/usr/bin/env python3
"""Headless CLI entry for speed_channels ticks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def main(argv: list | None = None) -> int:
    _bootstrap_path()

    from hermes_constants import get_hermes_home
    from plugins.speed_channels.core import (
        SpeedChannelsError,
        load_speed_config,
        run_tick,
    )

    parser = argparse.ArgumentParser(
        description="Run one speed_channels Discord tick (cron-friendly)."
    )
    parser.add_argument(
        "--config",
        default=str(get_hermes_home() / "config.yaml"),
        help="Path to config.yaml (default: HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--force-poll",
        action="store_true",
        help="Bypass the poll-interval gate and poll all three downloaders.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print compact JSON status on success.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_speed_config(Path(args.config))
        result = run_tick(config, force=args.force_poll)
    except SpeedChannelsError as exc:
        print(f"speed-channels: {exc}")
        return 1

    if args.debug:
        import json

        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
