"""Activation runs read-only from any cwd and restores the caller environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.platforms("windows")
def test_powershell_activation_roundtrip_selected_environment(tmp_path, monkeypatch):
    from hermes_cli.runtime_paths import runtime_facts_path
    from pm.paths import repo_root
    from pm.store import current_target
    from pm.lock import Lockfile

    root = repo_root()
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    facts = runtime_facts_path(root)
    selected = facts.parent / "environments" / "a" / "venv"
    site = selected / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (selected / "pyvenv.cfg").write_text("home = test")
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(selected)}}}))
    store = tmp_path / "tools"
    store.mkdir()
    lock = Lockfile(root / "pm" / "lock.json")
    target = current_target()
    entry = store / "python-test"
    entry.mkdir()
    (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"python": {
        "entry": entry.name, "version": lock.version("python"), "target": target,
        "artifacts": [a["sha256"] for a in lock.artifacts("python", target)],
        "env": {"HERMES_ACTIVATE_CANARY": "active"},
    }}}))
    env = dict(os.environ, HERMES_RUNTIME_DIR=str(store), PYTHONPATH="caller-original",
               PATHEXT=".COM;.EXE;.BAT;.CMD")
    script = tmp_path / "run.ps1"
    script.write_text(
        f"$ErrorActionPreference='Stop'; . '{root / 'activate.ps1'}'; "
        "[PSCustomObject]@{canary=$env:HERMES_ACTIVATE_CANARY;pythonpath=$env:PYTHONPATH}|ConvertTo-Json -Compress; "
        "deactivate; Write-Output ('restored=' + $env:PYTHONPATH); "
        "Write-Output ('canary=' + $env:HERMES_ACTIVATE_CANARY)", encoding="utf-8",
    )
    powershell = shutil.which("powershell")
    assert powershell
    run = subprocess.run([powershell,"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(script)],
                         cwd=tmp_path,env=env,capture_output=True,text=True,timeout=40)
    assert run.returncode == 0, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    record = json.loads(lines[0])
    assert record["canary"] == "active"
    assert record["pythonpath"] == str(root) + os.pathsep + str(site)
    assert lines[1:] == ["restored=caller-original", "canary="]
