"""Contract: the Python readiness twin answers exactly like the Go core.

``internal/readygates`` (Go) is the canonical statement of which boot
subsystems gate ``READY=1``; ``gateway/readygates.py`` is the twin the
runtime calls. This test runs every shared-table case against the Python
twin and, when a Go toolchain exists, pipes the same case through the
RUNNING Go binary (``cmd/readygates``) and asserts identical verdicts, so
the twins cannot drift.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.readygates import PlatformState, decide, ReadyInput

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CASES = _REPO_ROOT / "internal" / "readygates" / "testdata" / "cases.json"
_GO_DIR = _REPO_ROOT / "internal" / "readygates"


def _load_cases():
    doc = json.loads(_CASES.read_text())
    cases = doc.get("cases") or []
    assert cases, "shared readygates table is empty"
    return cases


def _python_answers(case):
    inp = ReadyInput(
        platforms=[PlatformState(enabled=bool(p.get("enabled")),
                                 connected=bool(p.get("connected")))
                  for p in (case.get("platforms") or [])],
        cron_started=bool(case.get("cron_started", False)),
        housekeeping_started=bool(case.get("housekeeping_started", False)),
        mcp_discovery_state=str(case.get("mcp_discovery_state", "pending")),
        boot_path_sends_state=str(case.get("boot_path_sends_state", "pending")),
    )
    res = decide(inp)
    return {"ready": res.ready, "blocked_by": res.blocked_by}


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_twin_matches_shared_table(case):
    expect = case["expect"]
    answers = _python_answers(case)
    assert answers["ready"] == expect["ready"], "ready"
    assert answers["blocked_by"] == expect["blocked_by"], "blocked_by"


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain")
def test_twin_matches_running_go_core():
    for case in _load_cases():
        request = {
            "platforms": [
                {"enabled": bool(p.get("enabled")), "connected": bool(p.get("connected"))}
                for p in (case.get("platforms") or [])
            ],
            "cron_started": bool(case.get("cron_started", False)),
            "housekeeping_started": bool(case.get("housekeeping_started", False)),
            "mcp_discovery_state": str(case.get("mcp_discovery_state", "pending")),
            "boot_path_sends_state": str(case.get("boot_path_sends_state", "pending")),
        }
        proc = subprocess.run(
            ["go", "run", "./cmd/readygates"],
            cwd=_GO_DIR,
            input=json.dumps(request),
            capture_output=True, text=True, check=True,
        )
        go_answers = json.loads(proc.stdout)
        py_answers = _python_answers(case)
        assert py_answers == go_answers, (
            f"{case['name']}: python={py_answers!r} go={go_answers!r}")
