"""The open-sidebar command and the toolbar action toggle the side panel per window."""

import shutil
import subprocess
from pathlib import Path

import pytest

BACKGROUND_JS = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "platforms"
    / "browser_sidebar"
    / "extension"
    / "background.js"
)

TOGGLE_HARNESS = Path(__file__).resolve().parent / "fixtures" / "sidebar_toggle_harness.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the background service worker",
)


@requires_node
def test_open_sidebar_command_toggles_side_panel_per_window():
    run = subprocess.run(
        ["node", str(TOGGLE_HARNESS), str(BACKGROUND_JS)],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert run.returncode == 0, f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    assert "OK" in run.stdout
