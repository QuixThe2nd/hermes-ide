"""Shared-contract parity for delegation lane harness reachability.

The Go twin (``internal/lanereach``) states the reachability and omp-fallback
contract; ``tools/delegate_lane_specs.py`` is what the runtime gates lane
tools with. This test pins the two together: every shared-table record
(``internal/lanereach/testdata/lane_cases.json``) is answered by the RUNNING
Go binary (``cmd/lanereach``, built once per session — the binary-comparison
test skips when no Go toolchain) and by the Python module with the same
pinned probes, and all answers must agree exactly.

Regression guard for the always-on lanes: a missing omp steps down to
claude, then the in-process subagent; a lane pinned to claude hides only
while that wrapper is unavailable; subagent is always reachable; the
removed cursor harness is rejected like any unknown harness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.delegate_lane_specs as specs

_REPO = Path(__file__).resolve().parents[2]
_MODULE = _REPO / "internal" / "lanereach"
_TABLE = _MODULE / "testdata" / "lane_cases.json"

_PIN_KEYS = ("omp", "claude")


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _go_binary(tmp_path_factory) -> str | None:
    """Build cmd/lanereach once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("lanereach", numbered=True) / "lanereach"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/lanereach"],
        cwd=_MODULE, capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/lanereach failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture(scope="session")
def go_lanereach(tmp_path_factory):
    return _go_binary(tmp_path_factory)


def _pinned_probes(pin):
    """specs._HARNESS_PROBES stand-ins answering from the case's pin table."""
    return {
        key: (lambda omp_config=None, key=key: pin.get(key) is not None)
        for key in _PIN_KEYS
    }


def _python_answer(case):
    """The Python module's answer with the case's probes pinned."""
    pin = case["pin"]
    omp_config = {"binary": case.get("omp_binary") or ""}
    with patch.object(specs, "_HARNESS_PROBES", _pinned_probes(pin)):
        harness = case["harness"]
        if case["op"] == "reachable":
            return {
                "reachable": specs.harness_reachable(harness, omp_config),
                "harness": harness,
            }
        lanes = {"ui_dev": {"harness": harness, "models": ["p/m"]}}
        spec = specs.resolve_runnable_spec("ui_dev", lanes, omp_config)
        return {
            "harness": spec["harness"],
            "reachable": specs.lane_harness_reachable("ui_dev", lanes, omp_config),
        }


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_module_matches_the_shared_table(case):
    """The Python twin — the one the runtime gates lane tools with — answers
    every shared-table record exactly as recorded."""
    assert _python_answer(case) == case["expect"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_module_matches_the_running_go_binary(case, go_lanereach):
    """The real Go binary answers the same record: Python module == Go output."""
    if go_lanereach is None:
        pytest.skip("no Go toolchain")
    run = subprocess.run(
        [go_lanereach],
        input=json.dumps({
            "op": case["op"], "harness": case["harness"],
            "omp_binary": case.get("omp_binary", ""), "pin": case["pin"],
        }),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == _python_answer(case) == case["expect"]


def test_shared_table_pins_every_probe():
    """Every case freezes both probes, or the Go run would consult the
    real host and stop being reproducible."""
    for case in _shared_cases():
        assert set(case["pin"]) == set(_PIN_KEYS), case["name"]


def test_shared_table_covers_the_ladder_contract():
    """Every branch of the reachability + fallback ladder appears in the
    table, so neither twin can drift on an untested branch."""
    names = " ".join(case["name"] for case in _shared_cases())
    required = (
        "subagent_needs_no_binary",
        "omp_stays_when_reachable",
        "steps_to_claude",
        "land_subagent",
        "pinned_claude_missing",
        "removed_cursor",
        "unknown_harness",
    )
    for needle in required:
        assert needle in names, f"shared table lost the {needle} case"
