"""Sidepanel per-tab session logic: transcripts, routing, new-chat, cleanup.

Runs the real ``sidepanel.js`` (plus the shared ``tabs.js`` helper) inside a
Node harness that fakes the DOM and the chrome APIs. The harness's own
assertions cover the contract; here we only prove it ran and passed.
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
HARNESS = Path(__file__).resolve().parent / "fixtures" / "sidepanel_tabs_harness.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the sidepanel page",
)


@requires_node
def test_sidepanel_keeps_one_conversation_per_tab():
    run = subprocess.run(
        ["node", str(HARNESS), str(EXTENSION_DIR / "sidepanel.js")],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert run.returncode == 0, f"sidepanel tabs harness failed:\n{run.stdout}\n{run.stderr}"
    assert "OK" in run.stdout
