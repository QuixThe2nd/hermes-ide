"""Shared-contract parity for steer-driven force-backgrounding.

``internal/forcebg`` (Go, canonical contract statement) and
``tools/delegate_tool_forcebg`` (the module the runtime executes) must decide
identically: registration gating, steer-before-adopt ordering, once-only
adoption, refusal fallback, and every user/model-visible string. The twin is
pinned against the RUNNING Go implementation, not merely the shared table:
when a Go toolchain is available the real ``cmd/forcebg`` binary replays every
shared-table case (testdata/forcebg_cases.json) and the Python transcript must
match step for step. Production Python never shells out to the binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import delegate_tool_forcebg as forcebg

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "forcebg" / "testdata" / "forcebg_cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


class _CaseAgent:
    def __init__(self, key: str, *, has_steer: bool, steer_refused: bool, depth: int):
        self.key = key
        self._delegate_depth = depth
        if has_steer:
            self._refuse = steer_refused

    def steer(self, text, *, on_accept=None):
        if self._refuse or not text or not text.strip():
            return False
        if on_accept is not None:
            on_accept("steer")
        return True


class _CaseRun:
    def __init__(self, parent, adopt_id):
        self.parent_agent = parent
        self._adopt = adopt_id

    def _adopt_running_worker(self, future):
        return self._adopt or None


def _replay(case) -> list:
    """Execute one shared-table case through the Python module; the transcript
    shape mirrors internal/forcebg/cmd/forcebg exactly."""
    agents = {
        a["key"]: _CaseAgent(
            a["key"],
            has_steer=a.get("has_steer", False),
            steer_refused=a.get("steer_refused", False),
            depth=a.get("delegate_depth", 0),
        )
        for a in case.get("agents", [])
    }
    runs = [_CaseRun(agents[r["parent"]], r.get("adopt_id", "")) for r in case.get("runs", [])]
    steps = []
    waits: dict = {}

    from tools.delegate_tool_config import _cfg as _real_cfg

    with ExitStack() as stack:
        if case.get("steer_force_background") is False:
            merged = {**dict(_real_cfg()), "steer_force_background": False}
            stack.enter_context(
                patch("tools.delegate_tool_config._cfg", lambda: merged)
            )
        if case.get("delivery_supported") is False:
            stack.enter_context(
                patch(
                    "tools.async_delegation._omitted_default_delivery_supported",
                    lambda: False,
                )
            )
        forcebg._reset_for_tests()
        try:
            for op in case["ops"]:
                kind = op["op"]
                step = {"op": kind}
                if kind == "register":
                    idx = op["run"]
                    future = Future()
                    waits[idx] = forcebg.register_blocking_wait(runs[idx], future, Event())
                    step["registered"] = waits[idx] is not None
                elif kind == "live":
                    step["key"] = op["key"]
                    step["count"] = forcebg.registered_wait_count(agents[op["key"]])
                elif kind == "has_eligible":
                    step["key"] = op["key"]
                    step["value"] = forcebg.has_eligible_blocking_waits(agents[op["key"]])
                elif kind == "steer":
                    agent = agents[op["key"]]
                    accepted, ids = forcebg.steer_forcing_background(agent, op["text"])
                    step.update(
                        key=op["key"], accepted=accepted, ids=list(ids),
                        summary=forcebg.summary(accepted, ids),
                    )
                elif kind == "adopt":
                    step["id"] = forcebg.adopt(waits[op["run"]]) or ""
                elif kind == "released":
                    step["value"] = waits[op["run"]].released_by_steer()
                elif kind == "complete":
                    # Mark the worker settled the way the runtime does: the future
                    # finishes (a live future is what keeps a wait releasable).
                    waits[op["run"]].future.set_result(None)
                elif kind == "notice":
                    step["text"] = forcebg.entry_notice(op["id"])
                elif kind == "receipt":
                    step["text"] = forcebg.busy_receipt(tuple(op["ids"]), op["injected"])
                elif kind == "steered_receipt":
                    step["text"] = forcebg.steered_queue_receipt(op.get("status_detail", ""))
                else:
                    raise AssertionError(f"unknown op {kind}")
                steps.append(step)
        finally:
            forcebg._reset_for_tests()
    return steps


@pytest.fixture(scope="session")
def go_forcebg(tmp_path_factory):
    """Build cmd/forcebg once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("forcebg", numbered=True) / "forcebg"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/forcebg"],
        cwd=_REPO / "internal" / "forcebg",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/forcebg failed:\n{build.stderr}")
    return str(binary)


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_python_transcript_matches_the_shared_table(case):
    assert _replay(case) == case["expect"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_go_binary_transcript_matches_the_shared_table(case, go_forcebg):
    if go_forcebg is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_forcebg],
        input=json.dumps(case),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    answer = json.loads(run.stdout)
    assert answer["steps"] == case["expect"]


def test_go_binary_rejects_unknown_op(go_forcebg):
    if go_forcebg is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_forcebg],
        input=json.dumps({"name": "bad", "agents": [], "runs": [], "ops": [{"op": "explode"}]}),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 2
    assert "unknown op" in run.stderr


def test_shared_table_covers_the_gate_matrix():
    """Every gate outcome has a case: kill switch, depth, delivery, refusal,
    refused steer, blank text, batch, idempotent adopt, phantom-free completion."""
    names = {case["name"] for case in _shared_cases()}
    for required in (
        "core_release_and_adopt", "kill_switch_off_never_registers",
        "child_depth_parent_not_eligible", "delivery_incapable_session_not_eligible",
        "rail_refusal_degrades_to_plain_steer", "steer_refused_adopts_nothing",
        "blank_text_refused", "batch_two_runs_one_steer",
        "adopt_idempotent_two_callers", "completed_wait_leaves_no_phantom",
    ):
        assert required in names, f"shared table lost the {required} contract case"
