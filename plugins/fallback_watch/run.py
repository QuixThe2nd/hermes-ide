#!/usr/bin/env python3
"""Long-running fallback_watch service entry (``python -m plugins.fallback_watch.run``)."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def main(argv: list[str] | None = None) -> int:
    _bootstrap_path()

    from hermes_constants import get_hermes_home
    from plugins.fallback_watch.core import (
        FallbackWatchError,
        discord_token,
        follow_from_eof,
        load_config,
        load_state,
        log_path,
        send_discord_alert,
        watch_lines,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Watch HERMES_HOME/logs/agent.log for primary-model fallback"
            " activations and alert a Discord channel (runs until SIGTERM)."
        )
    )
    parser.add_argument(
        "--config",
        default=str(get_hermes_home() / "config.yaml"),
        help="Path to config.yaml (default: HERMES_HOME/config.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config and bot token, then exit without watching.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except FallbackWatchError as exc:
        print(f"fallback-watch: {exc}", file=sys.stderr)
        return 1

    if not config.enabled:
        print("fallback-watch: disabled (fallback_watch.enabled is not true)")
        return 0

    try:
        discord_token()
    except FallbackWatchError as exc:
        # fail fast rather than retrying every event against a missing token
        print(f"fallback-watch: {exc}", file=sys.stderr)
        return 2 if args.check else 1

    if args.check:
        print("ok")
        return 0

    stop_event = threading.Event()

    def _request_stop(signum, frame) -> None:
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _request_stop)

    def _sleep(seconds: float) -> None:
        # wait() over sleep() so SIGTERM interrupts the backoff too
        stop_event.wait(seconds)

    state = load_state()
    try:
        watch_lines(
            follow_from_eof(
                log_path(),
                poll_seconds=config.poll_seconds,
                stop_event=stop_event,
            ),
            config,
            state,
            send=lambda message: send_discord_alert(message, config.chat_id),
            sleep_fn=_sleep,
            on_alert=lambda message: print(message, flush=True),
        )
    except FallbackWatchError as exc:
        print(f"fallback-watch: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
