"""Shared-contract parity for the always-spawn restart policy.

The same table (internal/restartsuccessor/testdata/restart_cases.json)
drives the Go twin's tests and this one: every case is replayed against
the REAL ``gateway.overlap_restart.spawn_successor`` decision and against
the running Go binary (cmd/restartsuccessor), and both must match the
record's expectation. The contract: every restart spawns exactly one
overlap successor through the first overlap-capable adapter, and no
config key disables the spawn — a stale ``gateway.restart_platforms:
false`` left in a user config.yaml is inert (#825).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.overlap_restart as overlap_restart

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "restartsuccessor" / "testdata" / "restart_cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _go_binary(tmp_path_factory) -> str | None:
    """Build cmd/restartsuccessor once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("restartsuccessor", numbered=True) / "restartsuccessor"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/restartsuccessor"],
        cwd=_REPO / "internal" / "restartsuccessor",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/restartsuccessor failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture(scope="session")
def go_restartsuccessor(tmp_path_factory) -> str | None:
    return _go_binary(tmp_path_factory)


class _TableAdapter:
    """Adapter stand-in whose observable behavior matches one table spec."""

    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.supports_overlap_restart = spec.get("supports") is True
        self._spec = spec
        self.spawn_calls = 0
        self.waited = False
        self.marked = False
        # Latch mirrors the real hub adapters: a fixed start fingerprint so
        # the decision path never touches /proc for table pids.
        self._last_overlap_successor_start_time = 2007821.0
        if "latched_pid" in spec:
            self._last_overlap_successor_pid = spec["latched_pid"]
        self._last_overlap_successor_service_managed = bool(spec.get("service_managed"))
        if spec.get("has_spawn"):
            self.spawn_overlap_successor = self._spawn_overlap_successor
        if spec.get("has_mark_ready"):
            self.wait_for_overlap_successor = self._wait_for_overlap_successor
            self.mark_overlap_successor_ready = self._mark_overlap_successor_ready

    async def _spawn_overlap_successor(self, *, via_service, predecessor_pid):
        self.spawn_calls += 1
        self.via_service_seen = via_service
        self.predecessor_pid_seen = predecessor_pid
        if self._spec.get("spawn_error"):
            raise RuntimeError("successor spawned but readiness failed")
        return self._spec.get("spawn_pid")

    async def _wait_for_overlap_successor(self):
        self.waited = True

    def _mark_overlap_successor_ready(self):
        self.marked = True


def _table_runner(case: dict):
    spec_input = case["input"]
    adapters = {s["name"]: _TableAdapter(s) for s in spec_input.get("adapters", [])}
    # The stale config rides on the runner exactly as it would on the real
    # one; spawn_successor must never consult it.
    runner = SimpleNamespace(
        adapters=adapters,
        _profile_adapters={},
        _overlap_restart_successor_started=bool(spec_input.get("started")),
        _overlap_restart_successor_pid=spec_input.get("started_pid"),
        config=spec_input.get("config") or {},
    )
    return runner, adapters


async def _python_decision(case: dict) -> dict:
    runner, adapters = _table_runner(case)
    try:
        pid = await overlap_restart.spawn_successor(
            runner, via_service=bool(case["input"].get("via_service"))
        )
    except RuntimeError:
        pid = "error"
    if pid == "error":
        result = "error"
    elif pid is None:
        result = "legacy-drain"
    elif case["input"].get("started"):
        result = "already-started"
    elif any(a._spec.get("spawn_error") and a.spawn_calls for a in adapters.values()):
        result = "readiness-failed-retained"
    else:
        result = "spawned"
    return {
        "result": result,
        "pid": None if pid == "error" else pid,
        "started": bool(getattr(runner, "_overlap_restart_successor_started", False)),
        "spawn_attempted": [n for n, a in adapters.items() if a.spawn_calls],
        "readiness_marked": [n for n, a in adapters.items() if a.marked],
        "service_managed": bool(getattr(runner, "_overlap_successor_systemd_managed", False)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
async def test_python_spawn_successor_matches_the_shared_table(case):
    assert await _python_decision(case) == case["expect"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_running_go_binary_matches_the_shared_table(case, go_restartsuccessor):
    """The real Go twin decides the same record: Python decision == Go decision."""
    if go_restartsuccessor is None:
        pytest.skip("no Go toolchain available")
    table = json.dumps({"cases": _shared_cases()}, separators=(",", ":"))
    run = subprocess.run(
        [go_restartsuccessor], input=table, capture_output=True, text=True, timeout=60,
    )
    if run.returncode != 0:
        pytest.fail(f"cmd/restartsuccessor failed:\n{run.stderr}")
    records = {r["name"]: r for r in json.loads(run.stdout)}
    assert records[case["name"]] == dict({"name": case["name"]}, **case["expect"])
