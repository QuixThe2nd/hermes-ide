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


# The pure decision table (hard_exit_action ↔ Go ResolveHardExitAction) is
# pinned over the shared table by test_restart_plan_contract.py; the tests
# here drive the runner-resolved and watchdog-executed halves.


# ── resolver against runner state (sync, watchdog-thread safe) ─────────────


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(identity="live", managed=False), HARD_EXIT_OVERLAP),
        (dict(identity="dead", newer_live=True), HARD_EXIT_OVERLAP),
        (dict(identity="dead"), HARD_EXIT_REQUESTED),
        (dict(started=False, newer_live=False), HARD_EXIT_REQUESTED),
        (dict(identity="unknown"), HARD_EXIT_PARK),
    ],
    ids=["live-unmanaged", "dead-newer-live", "dead-serial", "no-successor", "unknown"],
)
def test_resolver_maps_runner_state_to_actions(kwargs, expected):
    runner, patches = _overlap_runner(**kwargs)
    for p in patches:
        p.start()
    try:
        assert resolve_hard_exit_action(runner) == expected
    finally:
        for p in patches:
            p.stop()


def test_resolver_performs_mainpid_handover_for_managed_live_successor():
    runner, patches = _overlap_runner(identity="live", managed=True)
    for p in patches:
        p.start()
    handover = MagicMock(return_value=True)
    try:
        with patch("gateway.systemd_notify.handover_main_pid", handover):
            assert resolve_hard_exit_action(runner) == HARD_EXIT_OVERLAP
        handover.assert_called_once_with(runner._overlap_restart_successor_pid)
    finally:
        for p in patches:
            p.stop()


def test_resolver_probes_newer_generation_through_the_mirror():
    """The successor-less predecessor is protected by the registry mirror read."""
    runner, patches = _overlap_runner(started=False, newer_live=True)
    probe = None
    for p in patches:
        probe = p.start()  # the mirror probe is started last
    try:
        assert resolve_hard_exit_action(runner) == HARD_EXIT_OVERLAP
        probe.assert_called_once()
    finally:
        for p in patches:
            p.stop()


def test_resolver_treats_failed_newer_probe_as_absent():
    """A raising mirror probe must not guess a successor away or into being."""
    runner, patches = _overlap_runner(identity="live", managed=False, probe_fails=True)
    for p in patches:
        p.start()
    try:
        # Own-successor evidence still decides; the unreadable mirror adds nothing.
        assert resolve_hard_exit_action(runner) == HARD_EXIT_OVERLAP
        dead_runner, dead_patches = _overlap_runner(identity="dead", probe_fails=True)
        for p in dead_patches:
            p.start()
        try:
            # No provable successor and no provable newer line: the armed code stands.
            assert resolve_hard_exit_action(dead_runner) == HARD_EXIT_REQUESTED
        finally:
            for p in dead_patches:
                p.stop()
    finally:
        for p in patches:
            p.stop()


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


def test_watchdog_exits_overlap_when_live_successor_unmanaged():
    codes, _ = _armed_exit_code(lambda: HARD_EXIT_OVERLAP)
    assert codes == [GATEWAY_OVERLAP_RESTART_EXIT_CODE]


def test_watchdog_exits_overlap_after_managed_handover():
    codes, _ = _armed_exit_code(lambda: HARD_EXIT_OVERLAP)
    assert codes == [GATEWAY_OVERLAP_RESTART_EXIT_CODE]


def test_watchdog_keeps_armed_code_without_successor():
    codes, _ = _armed_exit_code(lambda: HARD_EXIT_REQUESTED, exit_code=1)
    assert codes == [1]


def test_watchdog_parks_instead_of_exiting_when_handover_unavailable():
    """Live successor + failing MAINPID transfer: no os._exit may happen."""
    teardown_done = threading.Event()
    codes, done = _armed_exit_code(
        lambda: HARD_EXIT_PARK, done_after_park=teardown_done.set
    )
    assert codes == []
    assert teardown_done.is_set()


def test_loop_liveness_watchdog_exits_overlap_with_live_successor():
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
            max_strikes=1, overlap_exit_plan=lambda: HARD_EXIT_OVERLAP,
        )
        assert handle is not None
        assert fired.wait(timeout=10.0), "loop-liveness watchdog did not fire"
        handle.stop()

    assert codes == [GATEWAY_OVERLAP_RESTART_EXIT_CODE]


def test_loop_liveness_watchdog_keeps_its_code_without_successor():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.call_soon_threadsafe.side_effect = lambda callback: None
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
            max_strikes=1, overlap_exit_plan=lambda: HARD_EXIT_REQUESTED,
        )
        assert handle is not None
        assert fired.wait(timeout=10.0), "loop-liveness watchdog did not fire"
        handle.stop()

    assert codes == [75]


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
