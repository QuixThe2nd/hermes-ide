"""Contracts between CI dependency installation and generated asset manifests."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import generate_icons

_REPO = Path(__file__).resolve().parents[2]


def test_workflow_uv_sync_extras_are_declared() -> None:
    """Every requested extra must resolve during setup, before tests can start."""
    pyproject = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8-sig"))
    declared = set(pyproject["project"]["optional-dependencies"])
    requested: dict[str, set[str]] = {}

    workflow_paths = sorted((_REPO / ".github" / "workflows").glob("*.y*ml"))
    assert workflow_paths
    for path in workflow_paths:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        for job in (workflow.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                commands = [step.get("run", ""), (step.get("with") or {}).get("command", "")]
                for command in commands:
                    if "uv sync" not in str(command):
                        continue
                    extras = set(re.findall(r"--extra\s+([\w-]+)", str(command)))
                    requested.setdefault(path.name, set()).update(extras)

    assert requested
    missing = {
        f"{workflow}: {', '.join(sorted(extras - declared))}"
        for workflow, extras in requested.items()
        if extras - declared
    }
    assert not missing, "CI requests undeclared extras:\n" + "\n".join(missing.values())


def test_generated_icons_do_not_target_removed_website() -> None:
    """Do not regenerate assets into the intentionally removed website tree."""
    assert all(not target[0].startswith("website/") for target in generate_icons.TARGETS)
