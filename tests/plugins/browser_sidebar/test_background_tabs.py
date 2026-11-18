"""Service-worker per-tab wiring: tab-tagged chat frames and tool targeting.

Runs the real ``background.js`` inside a Node harness with a fake chrome
(``tabs.get``/``tabs.query``/``scripting.executeScript``) and a fake
WebSocket, so the frame shapes the worker actually puts on the wire are what
gets asserted.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION_DIR = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "platforms"
    / "browser_sidebar"
    / "extension"
)
HARNESS = Path(__file__).resolve().parent / "fixtures" / "background_tabs_harness.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the background service worker",
)


@requires_node
def test_background_tags_chats_and_targets_run_tab_js_per_tab():
    run = subprocess.run(
        ["node", str(HARNESS), str(EXTENSION_DIR / "background.js")],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 0, f"background tabs harness failed:\n{run.stdout}\n{run.stderr}"
    assert "OK" in run.stdout
