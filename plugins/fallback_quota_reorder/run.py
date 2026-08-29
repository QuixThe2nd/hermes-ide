#!/usr/bin/env python3
"""Headless CLI entry for fallback_quota_reorder ticks."""

from __future__ import annotations

import argparse
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
    from plugins.fallback_quota_reorder.core import (
        FallbackQuotaReorderError,
        format_entry_label,
        format_readings_line,
        run_reorder,
    )

    parser = argparse.ArgumentParser(
        description="Reorder fallback_providers from Discord quota channel names (cron-friendly)."
    )
    parser.add_argument(
        "--config",
        default=str(get_hermes_home() / "config.yaml"),
        help="Path to config.yaml (default: HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--force-quota",
        action="store_true",
        help="Bypass the staleness freeze and allow config writes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print readings and order comparison without writing config.",
    )
    args = parser.parse_args(argv)

    try:
        result = run_reorder(
            config_path=Path(args.config),
            force_quota=args.force_quota,
            dry_run=args.dry_run,
        )
    except FallbackQuotaReorderError as exc:
        print(f"fallback-quota-reorder: {exc}")
        return 1

    if args.dry_run:
        readings = result["readings"]
        current_entries = result["current_entries"]
        desired_entries = result["desired_entries"]
        print(
            f"READINGS: {format_readings_line(readings, result.get('reliability'), result.get('scores'))}"
        )
        if readings:
            current_primary = result.get("primary_current")
            desired_primary = result.get("primary_desired")
            if desired_primary is not None:
                old_label = (
                    f"{current_primary.provider}/{current_primary.model}"
                    if current_primary is not None
                    else "unset"
                )
                print(
                    f"PRIMARY: {old_label}"
                    f" -> {desired_primary.provider}/{desired_primary.model}"
                )
            elif current_primary is not None:
                print(
                    f"PRIMARY: unchanged "
                    f"{current_primary.provider}/{current_primary.model}"
                )
        print(
            "CURRENT: "
            + ", ".join(format_entry_label(entry) for entry in current_entries)
        )
        print(
            "DESIRED: "
            + ", ".join(format_entry_label(entry) for entry in desired_entries)
        )
        if result["frozen"]:
            print("CHANGE: no (staleness freeze active)")
        else:
            print(f"CHANGE: {'yes' if result['would_change'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
