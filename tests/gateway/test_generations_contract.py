"""Shared-contract parity for the gateway restart generation array.

``internal/generations`` (Go, canonical contract statement) and
``gateway/generations.py`` (Python twin, what the runtime uses) must answer
every generation-array question identically: who is the newest live
generation, who receives a forwarded restart, who may claim the runtime
lock / PID file, when shared-credential adapters may start, and which PID
file holders may be displaced.

The twin is pinned against the RUNNING Go implementation, not merely the
shared table: when a Go toolchain is available the real ``cmd/generations``
binary evaluates every shared-table record
(internal/generations/testdata/cases.json) and the Python answers must
match exactly. The Go tests run the same table as a second cross-check, so
a decision change on either side fails BOTH suites unless the table — and
therefore both twins — moves.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.generations import (
    Generation, GenerationArray, takeover_allowed,
)

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "generations" / "testdata" / "cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _go_binary(tmp_path_factory) -> str | None:
    """Build cmd/generations once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("generations", numbered=True) / "generations"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/generations"],
        cwd=_REPO / "internal" / "generations",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/generations failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture(scope="session")
def go_generations(tmp_path_factory) -> str | None:
    return _go_binary(tmp_path_factory)


def _python_answers(case: dict) -> dict:
    array = GenerationArray(
        Generation(
            pid=int(row["pid"]),
            id=str(row.get("id") or ""),
            start_time=int(row.get("start_time") or 0),
            predecessor=int(row.get("predecessor") or 0),
            spawn_seq=int(row.get("spawn_seq") or 0),
            attached=bool(row.get("attached")),
            draining=bool(row.get("draining")),
            live=bool(row.get("live")),
        )
        for row in case["generations"]
    )
    me = int(case["me"])
    ancestors = array.ancestors_of(me)
    answers = {
        "newest_live": (array.newest_live() or Generation(pid=0)).pid,
        "restart_target": (array.newest_live_other(me) or Generation(pid=0)).pid,
        "may_claim": array.may_claim_authority(me),
        "may_start_shared": array.may_start_shared_adapters(me),
        "ancestors": ancestors,
        "describe": array.describe(),
        "takeover": None,
    }
    pid_file = case.get("pid_file")
    if pid_file is not None:
        answers["takeover"] = takeover_allowed(
            int(pid_file["pid"]), bool(pid_file["live"]), ancestors)
    return answers


def _go_answers(binary: str, case: dict) -> dict:
    run = subprocess.run(
        [binary], input=json.dumps(case), capture_output=True, text=True, timeout=60,
    )
    if run.returncode != 0:
        pytest.fail(f"cmd/generations rejected case {case.get('name')}: {run.stderr}")
    return json.loads(run.stdout)


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["name"])
def test_generation_answers_match_go_binary(case, go_generations):
    expected = case["expect"]
    answers = _python_answers(case)
    # The table is the contract; assert against it directly so the suite is
    # meaningful even on hosts with no Go toolchain.
    assert answers["newest_live"] == expected["newest_live"]
    assert answers["restart_target"] == expected["restart_target"]
    assert answers["may_claim"] == expected["may_claim"]
    assert answers["may_start_shared"] == expected["may_start_shared"]
    assert answers["ancestors"] == expected["ancestors"]
    assert answers["describe"] == expected["describe"]
    if "takeover" in expected:
        assert answers["takeover"] == expected["takeover"]
    else:
        assert answers["takeover"] is None
    if go_generations is None:
        pytest.skip("no Go toolchain: table-only parity")
    assert _go_answers(go_generations, case) == answers, (
        "Python twin drifted from the running Go implementation")


def test_original_asking_with_live_successor_is_not_newest():
    """The hub registry records only spawned successors; the original
    gateway asking while its successor serves must forward, not restart."""
    from gateway.generations import array_from_registry
    rows = [{"pid": 102, "predecessor_pid": 101, "start_time": 5,
             "state": "attached"}]
    array = array_from_registry(rows, me_pid=101, me_start_time=4,
                                me_predecessor=0, me_draining=True)
    assert array.newest_live_other(101) is not None
    assert array.newest_live_other(101).pid == 102
    assert array.may_claim_authority(101) is False
    assert array.describe() == ["102: serving", "101: draining"]


def test_registry_glue_derives_draining_from_successor_links():
    from gateway.generations import array_from_registry
    rows = [
        {"pid": 102, "predecessor_pid": 101, "start_time": 5, "state": "attached"},
        {"pid": 103, "predecessor_pid": 102, "start_time": 6, "state": "attached"},
    ]
    array = array_from_registry(rows, me_pid=103, me_start_time=6,
                                me_predecessor=102)
    # 103 asking: 102 spawned 103, so 102 is draining; 101 synthesized live.
    assert array.describe() == ["103: serving", "102: draining", "101: draining"]
    assert array.may_start_shared_adapters(103) is False
