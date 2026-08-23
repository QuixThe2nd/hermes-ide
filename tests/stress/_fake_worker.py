#!/usr/bin/env python3
"""Fake worker process that exercises the real subprocess contract.

Reads HERMES_KANBAN_TASK from env, heartbeats periodically, does short
work, completes via the CLI. Designed to be spawned by the dispatcher
exactly the way `hermes chat -q` would be, minus the LLM cost.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Make the worktree importable even when run manually without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.kanban_db import require_disposable_board


def main():
    tid = os.environ["HERMES_KANBAN_TASK"]
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "")

    # Fresh-process guard (pc_fa9ca887ebc61dc8): fail closed before any
    # mutation unless this process resolves a disposable board. A missing
    # HERMES_HOME/HERMES_KANBAN_DB override would otherwise silently write
    # heartbeats/completions onto the live board.
    require_disposable_board(expect_under=os.environ.get("HERMES_HOME") or None)

    # Announce via CLI (goes through real argparse + init_db + etc)
    subprocess.run(
        ["hermes", "kanban", "heartbeat", tid, "--note", "started"],
        check=True, capture_output=True,
    )

    # Simulate work with periodic heartbeats
    for i in range(3):
        time.sleep(0.3)
        subprocess.run(
            ["hermes", "kanban", "heartbeat", tid, "--note", f"progress {i+1}/3"],
            check=True, capture_output=True,
        )

    # Complete with structured handoff
    subprocess.run(
        [
            "hermes", "kanban", "complete", tid,
            "--summary", f"real-subprocess worker finished {tid}",
            "--metadata", json.dumps({
                "workspace": workspace,
                "worker_pid": os.getpid(),
                "iterations": 3,
            }),
        ],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    main()
