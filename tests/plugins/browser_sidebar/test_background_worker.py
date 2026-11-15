"""Background service-worker startup compatibility with Chrome alarms APIs."""

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

STARTUP_HARNESS = r"""
const assert = require('node:assert/strict');

const alarmApi = process.argv[1];
const backgroundScript = process.argv[2];
const event = { addListener() {} };
let alarmCount = 0;

global.chrome = {
  action: { onClicked: event },
  alarms: {
    onAlarm: event,
    create(name, options) {
      alarmCount += 1;
      assert.equal(name, 'bs-keepalive');
      assert.deepEqual(options, { delayInMinutes: 1, periodInMinutes: 1 });
      if (alarmApi === 'promise') {
        return Promise.reject(new Error('alarm creation rejected'));
      }
      return undefined;
    },
  },
  commands: { onCommand: event },
  runtime: {
    onMessage: event,
    sendMessage() { return Promise.resolve(); },
  },
  storage: {
    onChanged: event,
    sync: { async get() { return {}; } },
  },
  windows: { onFocusChange: event },
};

require(backgroundScript);
assert.equal(alarmCount, 1);
"""

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="requires node on PATH to execute the background service worker",
)


@requires_node
@pytest.mark.parametrize("alarm_api", ["callback", "promise"])
def test_background_worker_starts_with_supported_alarm_api(alarm_api):
    run = subprocess.run(
        ["node", "-e", STARTUP_HARNESS, alarm_api, str(BACKGROUND_JS)],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert run.returncode == 0, (
        f"background worker startup failed with {alarm_api} alarms API:\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
