"""Contract: the Python generation twin answers exactly like the Go core.

``internal/generations`` (Go) is the canonical decision core for overlapping
gateway restarts; ``gateway/generations.py`` is the twin the runtime calls.
This test pipes every shared-table case (testdata/cases.json) through the
RUNNING Go binary (cmd/generations) when a Go toolchain exists and asserts
the Python twin computes identical answers — newest live, restart target,
may-claim, may-start-shared, ancestors, takeover, and the describe roles.

Without a toolchain the table still runs against the Python twin alone, so a
table edit can never silently diverge from the documented expectations.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.generations import (
    Generation,
    GenerationArray,
    array_from_registry,
    takeover_allowed,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CASES = _REPO_ROOT / "internal" / "generations" / "testdata" / "cases.json"
_CMD = _REPO_ROOT / "internal" / "generations" / "cmd" / "generations"


def _load_cases():
    doc = json.loads(_CASES.read_text())
    cases = doc.get("cases") or []
    assert cases, "shared generations table is empty"
    return cases


def _python_array(case):
    gens = [
        Generation(
            pid=int(g["pid"]),
            predecessor=int(g.get("predecessor") or 0),
            spawn_seq=int(g.get("spawn_seq") or 0),
            attached=bool(g.get("attached")),
            draining=bool(g.get("draining")),
            live=bool(g.get("live")),
        )
        for g in case["generations"]
    ]
    return GenerationArray(gens)


def _python_answers(case):
    array = _python_array(case)
    me = int(case["me"])
    newest = array.newest_live()
    target = array.newest_live_other(me)
    pid_file = case.get("pid_file")
    takeover = None
    if pid_file is not None:
        takeover = takeover_allowed(
            int(pid_file["pid"]), bool(pid_file["live"]), array.ancestors_of(me))
    return {
        "newest_live": newest.pid if newest else 0,
        "restart_target": target.pid if target else 0,
        "may_claim": array.may_claim_authority(me),
        "may_start_shared": array.may_start_shared_adapters(me),
        "ancestors": array.ancestors_of(me),
        "describe": array.describe(),
        "takeover": takeover,
    }


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_twin_matches_shared_table(case):
    expect = case["expect"]
    answers = _python_answers(case)
    assert answers["newest_live"] == expect["newest_live"], "newest_live"
    assert answers["restart_target"] == expect["restart_target"], "restart_target"
    assert answers["may_claim"] == expect["may_claim"], "may_claim"
    assert answers["may_start_shared"] == expect["may_start_shared"], "may_start_shared"
    assert answers["ancestors"] == expect["ancestors"], "ancestors"
    assert answers["describe"] == expect["describe"], "describe"
    if "takeover" in expect and expect["takeover"] is not None:
        assert answers["takeover"] == expect["takeover"], "takeover"


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain")
def test_twin_matches_running_go_core():
    """The Python twin must agree with the RUNNING Go binary on every case."""
    for case in _load_cases():
        request: dict = {
            "generations": [
                {
                    "pid": int(g["pid"]),
                    "predecessor": int(g.get("predecessor") or 0),
                    "spawn_seq": int(g.get("spawn_seq") or 0),
                    "attached": bool(g.get("attached")),
                    "draining": bool(g.get("draining")),
                    "live": bool(g.get("live")),
                }
                for g in case["generations"]
            ],
            "me": int(case["me"]),
        }
        if "pid_file" in case:
            request["pid_file"] = {
                "pid": int(case["pid_file"]["pid"]),
                "live": bool(case["pid_file"]["live"]),
            }
        proc = subprocess.run(
            ["go", "run", "./cmd/generations"],
            cwd=_REPO_ROOT / "internal" / "generations",
            input=json.dumps(request),
            capture_output=True, text=True, check=True,
        )
        go_answers = json.loads(proc.stdout)
        py_answers = _python_answers(case)
        for key in ("newest_live", "restart_target", "may_claim",
                    "may_start_shared", "ancestors", "describe", "takeover"):
            assert py_answers[key] == go_answers[key], (
                f"{case['name']}: {key}: python={py_answers[key]!r} go={go_answers[key]!r}")


def test_registry_glue_builds_the_complete_array():
    """The registry glue adds the asking generation and synthesizes
    record-less predecessors with conservative OS liveness."""
    rows = [
        {"pid": 202, "predecessor_pid": 101, "start_time": 20, "state": "attached"},
        {"pid": 303, "predecessor_pid": 202, "start_time": 30, "state": "starting"},
    ]
    array = array_from_registry(rows, me_pid=202, me_start_time=20,
                                me_predecessor=101, me_draining=True)
    # v101 (the original) appears as a synthetic record and drains.
    original = array.by_pid(101)
    assert original is not None
    assert original.draining and not original.attached
    assert original.spawn_seq == 0  # older than every recorded successor
    # v202 keeps its recorded sequence (spawn order) and reports its own drain state.
    mine = array.by_pid(202)
    assert mine is not None
    assert mine.spawn_seq == 1 and mine.draining
    # v303 is newest even while still starting.
    newest = array.newest_live()
    assert newest is not None and newest.pid == 303
    assert not array.may_claim_authority(202)
    # v303 may not start shared adapters while any ancestor lives.
    assert not array.may_start_shared_adapters(303)


def test_registry_glue_original_without_records_is_oldest():
    """An original asking with live successors must not think itself newest."""
    rows = [{"pid": 202, "predecessor_pid": 101, "start_time": 20, "state": "attached"}]
    array = array_from_registry(rows, me_pid=101, me_start_time=10)
    newest = array.newest_live()
    assert newest is not None and newest.pid == 202
    assert not array.may_claim_authority(101)
    assert array.ancestors_of(202) == [101]


def test_registry_glue_places_unrecorded_successor_after_its_predecessor():
    """A successor absent from the rows still outranks its own predecessor.

    Hub-less deployments (and lost records) build the array from the
    self-reported predecessor link alone; a PID tie-break must not decide
    which of the two is newest — the successor is, by construction.
    """
    # No rows at all: the successor claims while the predecessor drains.
    array = array_from_registry([], me_pid=202, me_predecessor=101)
    newest = array.newest_live()
    assert newest is not None and newest.pid == 202
    assert array.may_claim_authority(202)
    assert array.ancestors_of(202) == [101]

    # A recorded successor of mine still outranks me: my own spawn never
    # leapfrogs generations recorded after me.
    rows = [{"pid": 303, "predecessor_pid": 202, "start_time": 30,
             "state": "attached"}]
    array = array_from_registry(rows, me_pid=202, me_predecessor=101)
    newest = array.newest_live()
    assert newest is not None and newest.pid == 303
    assert not array.may_claim_authority(202)

    # No known predecessor: the asker is treated as the original (oldest).
    array = array_from_registry(rows, me_pid=404)
    assert not array.may_claim_authority(404)


def test_registry_glue_preserves_row_drain_links():
    """A recorded generation with a recorded successor is draining."""
    rows = [
        {"pid": 202, "predecessor_pid": 101, "start_time": 20, "state": "attached"},
        {"pid": 303, "predecessor_pid": 202, "start_time": 30, "state": "attached"},
    ]
    array = array_from_registry(rows, me_pid=101)
    recorded = array.by_pid(202)
    assert recorded is not None
    assert recorded.draining
    assert "202: draining" in array.describe()
