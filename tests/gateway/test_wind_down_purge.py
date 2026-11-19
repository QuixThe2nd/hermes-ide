"""Invariant: the wind-down/park restart machinery stays purged.

The unified overlap-handover restart pipeline removed the old
"ask active agents to park" wind-down choreography entirely. This pins that no
reference creeps back into the gateway, CLI, or plugin trees (the unrelated
"Evening wind-down" user cron blueprint is excluded), and that the drain knob
the replacement pipeline reads is present.
"""

import subprocess
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_no_wind_down_or_park_references_remain():
    completed = subprocess.run(
        [
            "grep",
            "-rn",
            "-E",
            "wind_down|wind-down|Asking.*park",
            str(PROJECT_ROOT / "gateway"),
            str(PROJECT_ROOT / "hermes_cli"),
            str(PROJECT_ROOT / "plugins"),
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    hits = [
        line
        for line in completed.stdout.splitlines()
        if line.strip()
        # Unrelated user-facing cron blueprint name.
        and "blueprint_catalog" not in line
        # Compiled artifacts of pre-purge sources.
        and "__pycache__" not in line
    ]
    assert hits == [], f"wind-down/park references re-introduced:\n" + "\n".join(hits)


def test_default_config_carries_drain_timeout():
    gateway = DEFAULT_CONFIG["gateway"]
    # Relationship assert: the drain knob the unified pipeline reads exists
    # and bounds the stop() drain.
    assert isinstance(gateway["drain_timeout"], (int, float))
    assert gateway["drain_timeout"] > 0
