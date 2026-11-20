"""Shared-contract parity for the restart-transport decision.

``internal/restartplan`` (Go, canonical contract statement) and
``gateway/restart.py::restart_transport_plan`` (the function the runtime
executes) must decide identically: the transport flags of a restart derive
from live host conditions only — never from which door (user vs update
signal) triggered it. The twin is pinned against the RUNNING Go
implementation, not merely the shared table: when a Go toolchain is
available the real ``cmd/restartplan`` binary decides every shared-table
record (testdata/restart_cases.json) and the Python output must match.
Production Python never shells out to the binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.restart import (
    planned_restart_exit_code,
    restart_transport_plan,
)

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "restartplan" / "testdata" / "restart_cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _decide(case) -> dict:
    return restart_transport_plan(
        case["trigger"],
        supervised=case["supervised"], container=case["container"],
    )


def _exit_code(case):
    return planned_restart_exit_code(
        restart_requested=case["restart_requested"],
        via_service=case["via_service"],
        successor_started=case["successor_started"],
        supervised=case["supervised"],
        newer_live_generation=case.get("newer_live_generation", False),
    )

@pytest.fixture(scope="session")
def go_restartplan(tmp_path_factory) -> str | None:
    """Build cmd/restartplan once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("restartplan", numbered=True) / "restartplan"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/restartplan"],
        cwd=_REPO / "internal" / "restartplan",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/restartplan failed:\n{build.stderr}")
    return str(binary)


def _python_answer(case):
    if case["op"] == "decide":
        return _decide(case)
    return _exit_code(case)

def _expected(case):
    return case["expect"] if case["op"] == "decide" else case["expect_exit_code"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_python_decides_the_shared_table(case):
    assert _python_answer(case) == _expected(case)


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_go_binary_decides_the_shared_table(case, go_restartplan):
    if go_restartplan is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_restartplan],
        input=json.dumps(case),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    answer = json.loads(run.stdout)
    if case["op"] == "exit_code":
        # {"code": N, "special": bool} ↔ the Python None-or-int answer.
        want = _expected(case)
        assert answer["special"] == (want is not None)
        assert (answer["code"] if answer["special"] else None) == want
    else:
        assert answer == _expected(case)


def test_trigger_never_changes_the_transport():
    """The invariant the update path regressed on: same host, same plan."""
    for supervised in (False, True):
        for container in (False, True):
            user = restart_transport_plan(
                "user", supervised=supervised, container=container
            )
            signal = restart_transport_plan(
                "update_signal", supervised=supervised, container=container
            )
            assert user == signal


def test_unknown_trigger_is_a_caller_bug():
    for bad in ("", "pause_for_update", "cron", "USER"):
        with pytest.raises(ValueError, match="unknown restart trigger"):
            restart_transport_plan(bad, supervised=False, container=False)


def test_go_binary_rejects_unknown_trigger(go_restartplan):
    if go_restartplan is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_restartplan],
        input=json.dumps({"trigger": "pause_for_update",
                          "supervised": False, "container": False}),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 2
    assert "unknown trigger" in run.stderr
