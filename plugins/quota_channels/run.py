#!/usr/bin/env python3
"""Headless CLI entry for quota_channels ticks."""

from __future__ import annotations

import argparse
import json
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
    from plugins.quota_channels.core import QuotaChannelsError, load_quota_config, run_tick

    parser = argparse.ArgumentParser(
        description="Run one quota_channels Discord tick (cron-friendly)."
    )
    parser.add_argument(
        "--config",
        default=str(get_hermes_home() / "config.yaml"),
        help="Path to config.yaml (default: HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--force-quota",
        action="store_true",
        help="Bypass the quota-interval gate and fetch all providers.",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help=(
            "Collection-only tick: refresh provider quotas and Z.AI wallets "
            "into the collector snapshot without any Discord credentials, "
            "channel IDs or Discord requests."
        ),
    )
    parser.add_argument(
        "--print-snapshot",
        action="store_true",
        help=(
            "Print the current collector snapshot JSON and exit; reads state "
            "only, never collects and never touches Discord."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print compact JSON status on success.",
    )
    args = parser.parse_args(argv)

    if args.print_snapshot:
        from plugins.quota_channels import collector

        try:
            config = load_quota_config(Path(args.config), require_discord=False)
        except QuotaChannelsError:
            # a snapshot is still useful without a valid config section
            config = None
        print(json.dumps(collector.read_snapshot(config), separators=(",", ":")))
        return 0

    try:
        if args.collect_only:
            from plugins.quota_channels import collector

            config = load_quota_config(Path(args.config), require_discord=False)
            result = collector.run_collect(config, force=args.force_quota)
        else:
            config = load_quota_config(Path(args.config))
            result = run_tick(config, force=args.force_quota)
    except QuotaChannelsError as exc:
        print(f"quota-channels: {exc}")
        return 1

    if args.debug:
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
