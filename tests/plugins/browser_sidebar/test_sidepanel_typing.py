"""Side panel typing-indicator logic, exercised via a Node harness with a fake DOM."""

import shutil
import subprocess
from pathlib import Path

import pytest

SIDEPANEL_JS = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "platforms"
    / "browser_sidebar"
    / "extension"
    / "sidepanel.js"
)

HARNESS = Path(__file__).resolve().parent / "fixtures" / "sidepanel_typing_harness.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the side panel script",
)


@requires_node
def test_typing_indicator_shows_hides_and_times_out():
    run = subprocess.run(
        ["node", str(HARNESS), str(SIDEPANEL_JS)],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert "OK" in run.stdout
