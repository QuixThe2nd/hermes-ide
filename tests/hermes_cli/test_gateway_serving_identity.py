"""`hermes gateway status` serving-identity block.

The status command must separate the AUTHORITATIVE serving gateway from what
the supervisor and predecessor-era files say: during an overlap restart the
pid file, gateway_state.json and systemd MainPID all name the draining
predecessor, and a stale/unreachable/unattested identity must print
"unknown" (or "starting"), never a false green serving claim.

The draining-predecessor cases run through the REAL control-socket server
and client (an in-process ``GatewayControlServer`` answering ``identify``
with a runner, queried by the same ``identify_gateway`` the CLI uses) — not
a monkeypatched payload — so the wire shape the CLI renders is the one a
live gateway emits.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.gateway import _print_serving_identity


def _patch_sources(monkeypatch, *, identify=None, state=None,
                   state_pid_is_live=False, state_is_stale=False,
                   state_restart_requested=None, successor_identity=None):
    from gateway import control_socket
    from gateway import status as status_mod

    monkeypatch.setattr(control_socket, "identify_gateway", lambda home, **kw: identify)
    monkeypatch.setattr(status_mod, "read_runtime_status", lambda path=None: state)
    monkeypatch.setattr(status_mod, "runtime_status_pid_is_live", lambda record: state_pid_is_live)
    monkeypatch.setattr(status_mod, "runtime_status_is_stale", lambda record, ttl_s=None: state_is_stale)
    if successor_identity is not None:
        monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: successor_identity(pid) != "dead")
        monkeypatch.setattr(
            status_mod, "get_process_start_time",
            lambda pid: successor_identity(pid) == "live" and 999 or None)
    # The gateway.run process scan is host evidence: patch it so tests are
    # hermetic (the dev host may run a live gateway — including a real
    # overlap pair).
    from hermes_cli import gateway as gateway_cli

    monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [])


def test_status_prints_unknown_when_no_identity_source_answers(monkeypatch, capsys):
    _patch_sources(monkeypatch)
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "Serving gateway identity:" in out
    assert "⚠ Serving gateway: unknown" in out
    assert "✓ Serving gateway:" not in out


def test_status_state_draining_record_is_never_serving(monkeypatch, capsys):
    _patch_sources(
        monkeypatch,
        state={"pid": 1438, "gateway_state": "draining", "restart_requested": True},
        state_pid_is_live=True,
    )
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "⚠ Serving gateway: unknown" in out
    assert "✓ Serving gateway:" not in out
    assert "draining" in out




def test_status_silent_when_nothing_to_say(monkeypatch, capsys):
    """No gateway installed at all: no identity block, no noise."""
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        "gateway.serving_identity.serving_status_lines", lambda **kw: [])
    _print_serving_identity()
    assert capsys.readouterr().out == ""


# ── real control-socket round trip: the wire shape the CLI renders ─────────


class _ControlSocketServer:
    """Real GatewayControlServer on the isolated HERMES_HOME's socket."""

    def __init__(self, runner):
        from gateway.control_socket import GatewayControlServer, build_identify_payload
        from hermes_constants import get_hermes_home

        self._server = GatewayControlServer(
            get_hermes_home(),
            verb_handlers={"identify": lambda: build_identify_payload(runner=runner)},
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.started = threading.Event()

    def __enter__(self):
        # A live gateway stamps gateway_state.json at startup, which warms
        # the code-identity cache before any control-socket identify. Do
        # the same here: otherwise the first identify pays several cold
        # `git` subprocess calls, which blows the client's 2s deadline on
        # a saturated test runner and turns this into a wall-clock flake.
        from hermes_cli.version_info import get_code_identity

        get_code_identity()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve())
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        assert self.started.wait(10.0)

    async def _serve(self):
        await self._server.start()
        self._stop = asyncio.Event()
        self.started.set()
        await self._stop.wait()
        await self._server.stop()

    def __exit__(self, *exc):
        loop, stop_event = self._loop, self._stop
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread is not None:
            self._thread.join(10.0)
        self._server.cleanup_files()
        return False


def _live_successor_runner():
    """Draining predecessor reporting THIS test process as the successor.

    The successor PID is real (this process), so the CLI's fingerprint
    probe resolves it live — isolating the rendering from process liveness.
    """
    from gateway.status import get_process_start_time

    return SimpleNamespace(
        adapters={}, _draining=True, _restart_requested=True,
        _overlap_successor_pending=False, _overlap_predecessor_pid=None,
        _overlap_restart_successor_pid=os.getpid(),
        _overlap_restart_successor_start_time=get_process_start_time(os.getpid()),
    )


def _patch_successor_readiness(monkeypatch, ready: bool):
    from gateway import overlap_restart

    monkeypatch.setattr(
        overlap_restart, "overlap_successor_readiness",
        lambda runner: "ready" if ready else "spawned")


@pytest.fixture
def quiet_state(monkeypatch):
    """Keep the state-file ladder out of the socket tests' way."""
    from gateway import status as status_mod
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(status_mod, "read_runtime_status", lambda path=None: None)
    return monkeypatch


def test_status_via_real_socket_green_only_for_hub_attested_successor(
        monkeypatch, capsys, quiet_state):
    _patch_successor_readiness(monkeypatch, ready=True)
    with _ControlSocketServer(_live_successor_runner()):
        _print_serving_identity()
    out = capsys.readouterr().out
    assert f"✓ Serving gateway: pid {os.getpid()} — restart successor" in out
    assert "hub-confirmed serving" in out
    assert "expected handoff" in out


def test_status_via_real_socket_spawned_but_unattested_never_green(
        monkeypatch, capsys, quiet_state):
    _patch_successor_readiness(monkeypatch, ready=False)
    with _ControlSocketServer(_live_successor_runner()):
        _print_serving_identity()
    out = capsys.readouterr().out
    assert "✓ Serving gateway:" not in out
    assert f"successor pid {os.getpid()} is starting" in out
    assert "readiness is not yet confirmed" in out


def test_status_legacy_socket_answer_with_two_gateway_processes_is_ambiguous(
        monkeypatch, capsys, quiet_state):
    """Live incident shape: a pre-overlap answerer still owns the control
    socket while the overlap successor serves turns. A green line naming
    the answerer is wrong; the block must be ambiguous naming both."""
    from gateway import control_socket
    from hermes_cli import gateway as gateway_cli

    monkeypatch.setattr(
        control_socket, "identify_gateway",
        lambda home, **kw: {"pid": 709505, "start_time": 850305})
    monkeypatch.setattr(gateway_cli, "find_gateway_pids",
                        lambda: [709505, 1531038])
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "✓ Serving gateway:" not in out
    assert "pid 709505" in out and "pid 1531038" in out


def test_status_never_serves_from_systemd_main_pid(monkeypatch, capsys, quiet_state):
    """systemd MainPID is supervisor evidence: during overlap it names the
    draining predecessor, so it must never become a serving claim."""
    from hermes_cli import gateway as gateway_cli

    monkeypatch.setattr(gateway_cli, "_systemd_main_pid", lambda system=True: 709505)
    monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [709505, 1531038])
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "✓ Serving gateway:" not in out
    assert "pid 709505" in out and "pid 1531038" in out
