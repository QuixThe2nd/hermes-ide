"""Invariant: the wind-down/park restart machinery stays purged.

The unified overlap-handover restart pipeline removed the old
"ask active agents to park" wind-down choreography entirely. This pins that no
reference creeps back into the gateway, CLI, or plugin trees (the unrelated
"Evening wind-down" user cron blueprint is excluded), and that the new
config surface it was replaced by is present.
"""

import subprocess
from pathlib import Path

from gateway.restart import restart_platforms_enabled
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


def test_restart_platforms_default_true():
    assert restart_platforms_enabled(None) is True
    assert restart_platforms_enabled({}) is True


def test_default_config_carries_restart_platforms_and_drain_timeout():
    gateway = DEFAULT_CONFIG["gateway"]
    # Relationship asserts: the knobs the unified pipeline reads exist and the
    # restart-platforms default is the bounce-connections posture the pipeline
    # assumes when nothing is configured.
    assert gateway["restart_platforms"] is True
    assert isinstance(gateway["drain_timeout"], (int, float))
    assert gateway["drain_timeout"] > 0
