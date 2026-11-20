"""A hard-exit backstop must never kill a live overlap successor.

2026-11-20 incident: a draining overlap predecessor wedged inside stop()
(an unbounded hub-RPC await); the thread shutdown watchdog fired after 60s
and ``os._exit(1)``-ed. Under ``Restart=always`` + ``KillMode=mixed``
systemd read exit 1 as failure, killed the whole unit cgroup — including
the live, authoritative successor serving turns — and restarted the unit.
These tests pin the invariant from the other direction: whenever a
verified-live successor (or a newer live generation) carries the restart
line, every watchdog hard exit routes through the overlap handover path
(exit 76 after a MAINPID transfer, or parking), never a code the
supervisor treats as "restart the unit".
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.overlap_restart import (
    HARD_EXIT_OVERLAP,
    HARD_EXIT_PARK,
    HARD_EXIT_REQUESTED,
    resolve_hard_exit_action,
)
from gateway.restart import GATEWAY_OVERLAP_RESTART_EXIT_CODE
from gateway.shutdown_watchdog import arm_shutdown_watchdog, start_loop_liveness_watchdog
from tests.gateway.restart_test_helpers import make_restart_runner
# The shared table's owner: internal/restartplan/testdata/restart_cases.json.
from tests.gateway.test_restart_plan_contract import _shared_cases


def _overlap_runner(
    *, started: bool = True, pid: int = 5150, managed: bool = False,
    identity: str = "live", newer_live: bool = False, probe_fails: bool = False,
) -> tuple[SimpleNamespace, list]:
    """A draining predecessor double plus the status patches pinning its
    successor's identity (the resolver reads /proc and the registry mirror
    synchronously — exactly what a watchdog thread may do)."""
    runner = SimpleNamespace(
        _draining=True,
        _restart_requested=True,
        _overlap_restart_successor_started=started,
        _overlap_restart_successor_pid=pid if started else None,
        _overlap_successor_systemd_managed=managed,
        _overlap_restart_successor_start_time=4242,
    )

    def _newer_probe():
        if probe_fails:
            raise OSError("registry mirror unreadable")
        return 777 if newer_live else None

    patches = [
        patch("gateway.status._pid_exists", return_value=identity != "dead"),
        patch("gateway.status.get_process_start_time", return_value=4242),
        patch("gateway.overlap_restart.newest_live_other_pid", side_effect=_newer_probe),
    ]
    if identity == "unknown":
        # A live PID whose start time cannot be fingerprinted.
        patches[1] = patch("gateway.status.get_process_start_time", return_value=None)
    return runner, patches


@contextmanager
def _successor_evidence(patches):
    """Hold the /proc and mirror patches that pin the successor's identity."""
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


# ── resolver against runner state (sync, watchdog-thread safe) ─────────────
#
# The runner-resolved half of the contract is driven by the shared table's
# op "hard_exit_runner" rows — the same rows the Go twin answers through
# ResolveRunnerHardExit and the built binary answers through its
# "hard_exit_runner" op. The pure decision table (op "hard_exit") is pinned
# by test_restart_plan_contract.py; the watchdog-executed half below drives
# the table's op "hard_exit_code" rows through the real armed thread.


@pytest.mark.parametrize(
    "case",
    [c for c in _shared_cases() if c["op"] == "hard_exit_runner"],
    ids=lambda c: c["name"],
)
def test_resolver_answers_the_shared_runner_rows(case):
    identity = case["successor_identity"]
    runner, patches = _overlap_runner(
        identity=identity, started=case["successor_started"],
        newer_live=case.get("newer_live_generation", False),
        managed=case.get("systemd_managed", False),
    )
    handover = MagicMock(return_value=case.get("mainpid_transfer_succeeds", False))
    with _successor_evidence(patches), \
            patch("gateway.systemd_notify.handover_main_pid", handover):
        assert resolve_hard_exit_action(runner) == case["expect_action"]


def test_resolver_performs_mainpid_handover_for_managed_live_successor():
    runner, patches = _overlap_runner(identity="live", managed=True)
    handover = MagicMock(return_value=True)
    with _successor_evidence(patches), \
            patch("gateway.systemd_notify.handover_main_pid", handover):
        assert resolve_hard_exit_action(runner) == HARD_EXIT_OVERLAP
    handover.assert_called_once_with(runner._overlap_restart_successor_pid)


def test_resolver_probes_newer_generation_through_the_mirror():
    """The successor-less predecessor is protected by the registry mirror read."""
    runner, patches = _overlap_runner(started=False, newer_live=True)
    mirror = patches.pop()  # the mirror probe is patched last
    with _successor_evidence(patches), mirror as probe:
        assert resolve_hard_exit_action(runner) == HARD_EXIT_OVERLAP
    probe.assert_called_once()


def test_resolver_treats_failed_newer_probe_as_absent():
    """A raising mirror probe must not guess a successor away or into being."""
    live, live_patches = _overlap_runner(identity="live", probe_fails=True)
    with _successor_evidence(live_patches):
        # Own-successor evidence still decides; the unreadable mirror adds nothing.
        assert resolve_hard_exit_action(live) == HARD_EXIT_OVERLAP
    dead, dead_patches = _overlap_runner(identity="dead", probe_fails=True)
    with _successor_evidence(dead_patches):
        # No provable successor and no provable newer line: the armed code stands.
        assert resolve_hard_exit_action(dead) == HARD_EXIT_REQUESTED


# ── watchdog honors the plan ────────────────────────────────────────────────


def _armed_exit_code(overlap_exit_plan, *, exit_code=1, delay=0.05,
                     done_after_park=None) -> tuple[list[int], threading.Event]:
    """Arm the shutdown watchdog against a fake os._exit; return (codes, done)."""
    done = threading.Event()
    codes: list[int] = []
    fired = threading.Event()

    def fake_exit(code):
        codes.append(code)
        fired.set()

    plan_calls = {"n": 0}

    def plan():
        plan_calls["n"] += 1
        action = overlap_exit_plan()
        if action == HARD_EXIT_PARK and done_after_park is not None and plan_calls["n"] >= 2:
            done.set()  # teardown completes while parked: the backstop stands down
            done_after_park()
        return action

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit), \
            patch("gateway.shutdown_watchdog._write_watchdog_dump"), \
            patch("gateway.shutdown_watchdog._mark_exited_quietly"):
        arm_shutdown_watchdog(
            delay, done_event=done, snapshot_fn=lambda: {},
            exit_code=exit_code, dump_path=None, overlap_exit_plan=plan,
        )
        # Exit cases fire within the delay; the park case unblocks via done.
        deadline = time.monotonic() + 10.0
        while not fired.is_set() and not done.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
    return codes, done


@pytest.mark.parametrize(
    "case",
    [c for c in _shared_cases() if c["op"] == "hard_exit_code"],
    ids=lambda c: c["name"],
)
def test_armed_watchdog_executes_the_shared_exit_rows(case):
    """The firing watchdog maps the resolved action to the table's exit row:
    overlap overrides the armed code with the handover exit, requested keeps
    the armed code, and park refuses to exit while teardown is pending."""
    teardown_done = threading.Event()
    codes, _ = _armed_exit_code(
        lambda: case["action"], exit_code=case["armed_exit_code"],
        done_after_park=None if case["expect_exited"] else teardown_done.set,
    )
    if case["expect_exited"]:
        assert codes == [case["expect_code"]]
    else:
        assert codes == []
        assert teardown_done.is_set()  # parked through a re-plan until teardown stood it down


@pytest.mark.parametrize(
    "plan_answer, expected_code",
    [
        (HARD_EXIT_OVERLAP, GATEWAY_OVERLAP_RESTART_EXIT_CODE),
        (HARD_EXIT_REQUESTED, 75),
    ],
    ids=["exits_overlap_with_live_successor", "keeps_its_code_without_successor"],
)
def test_loop_liveness_watchdog_honors_the_plan(plan_answer, expected_code):
    """The loop-freeze backstop defaults to exit 75 — a cgroup-killing code
    under RestartForceExitStatus — so it must take the overlap handover too."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: None  # probe never lands
    fired = threading.Event()
    codes: list[int] = []

    def fake_exit(code):
        codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit), \
            patch("gateway.shutdown_watchdog._mark_exited_quietly"), \
            patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.02, probe_timeout=0.02,
            max_strikes=1, overlap_exit_plan=lambda: plan_answer,
        )
        assert handle is not None
        assert fired.wait(timeout=10.0), "loop-liveness watchdog did not fire"
        handle.stop()

    assert codes == [expected_code]


# ── wiring: stop() arms the watchdog with the runner-resolved plan ─────────


@pytest.mark.asyncio
async def test_stop_arms_watchdog_with_overlap_plan_bound_to_runner(monkeypatch):
    """The live stop() path must pass a plan that resolves from THIS runner.
    The fake arm replaces the real watchdog, so clearing the pytest guard is safe."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.0
    runner._overlap_restart_successor_started = True
    runner._overlap_restart_successor_pid = 5150
    runner._overlap_successor_systemd_managed = False
    runner._overlap_restart_successor_start_time = 4242

    captured: dict = {}

    def fake_arm(delay_s, **kwargs):
        captured.update(kwargs, delay_s=delay_s)
        # Resolve at arm time: a completed stop() legitimately clears successor
        # state, and the plan is only consulted in reality while teardown is
        # wedged — i.e. before any of that clearing ran.
        captured["plan_answer"] = kwargs["overlap_exit_plan"]()

    with patch("gateway.run.arm_shutdown_watchdog", side_effect=fake_arm), \
            patch("gateway.status._pid_exists", return_value=True), \
            patch("gateway.status.get_process_start_time", return_value=4242), \
            patch("gateway.overlap_restart.newest_live_other_pid", return_value=None), \
            patch("gateway.status.remove_pid_file"), \
            patch("gateway.status.write_runtime_status"):
        await runner.stop()

    plan = captured.get("overlap_exit_plan")
    assert callable(plan), "stop() must arm the shutdown watchdog with an overlap exit plan"
    # The plan resolves from the runner it was armed with: a verified-live
    # unmanaged successor answers the overlap action.
    assert captured["plan_answer"] == HARD_EXIT_OVERLAP
