#!/usr/bin/env python3
"""Headless CLI entry for home_server sync (cron-friendly).

Runs the debounced reconcile — at most once per hour unless ``--force`` (or
unless the in-code template changed, which re-syncs immediately). Use
this from cron or systemd exactly like quota_channels' ``run.py``:

    hermes cron add \\
      --schedule "every 1h" \\
      --script "python3 /path/to/hermes-agent/plugins/home_server/run.py" \\
      --no-agent \\
      --name "home-server-sync"

Silent on success (cron stays quiet); prints ``home-server: <message>`` and
exits non-zero on failure.
"""

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

    from plugins.home_server.core import HomeServerError, reconcile, sync_if_due

    parser = argparse.ArgumentParser(
        description="Run one debounced home_server Discord reconcile."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the hourly debounce and reconcile now.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print compact JSON status on success.",
    )
    args = parser.parse_args(argv)

    try:
        if args.force:
            result = reconcile()
        else:
            result = sync_if_due()
    except HomeServerError as exc:
        print(f"home-server: {exc}")
        return 1

    if args.debug:
        import json

        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
