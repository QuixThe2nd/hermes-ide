"""Unified gateway restart requests stay attached without timeout escalation."""

import argparse
import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

from hermes_cli import gateway as gw


def test_observer_reports_successor_count_changes_and_old_exit(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    records = iter([
        {
            "pid": 100,
            "gateway_state": "draining",
            "restart_requested": True,
            "active_agents": 2,
            "active_work": [{"kind": "chat", "session": "discord:dm:1", "elapsed_s": 65}],
        },
        {
            "pid": 200,
            "gateway_state": "running",
            "active_agents": 0,
            "platforms": {"discord": {"state": "connected", "writer_pid": 200}},
        },
        {
            "pid": 100,
            "gateway_state": "draining",
            "restart_requested": True,
            "active_agents": 1,
            "active_work": [{"kind": "chat", "session": "discord:dm:1", "elapsed_s": 70}],
        },
    ])
    monkeypatch.setattr(progress, "_read_status", lambda home: next(records, None))
    alive = iter([True, True, False])
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: next(alive))
    emitted = []

    outcome = progress.observe_gateway_restart(
        100, home=tmp_path, emit=emitted.append, sleep=lambda _: None,
    )

    assert outcome.completed is True
    assert outcome.successor_pid == 200
    assert any("New gateway PID 200 is up" in line and "discord" in line for line in emitted)
    assert (
        "⏳ Old gateway PID 100: waiting on 2 active agent(s): "
        "chat turn discord:dm:1, running 1m05s"
    ) in emitted
    assert any("Old gateway PID 100: waiting on 1 active agent(s)" in line for line in emitted)
    assert any("discord:dm:1" in line and "1m05s" in line for line in emitted)
    assert emitted[-1] == "✓ Old gateway PID 100 exited — restart complete"


def test_observer_reads_predecessor_counts_after_successor_owns_runtime_status(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 200,
        "gateway_state": "running",
        "platforms": {"discord": {"state": "connected", "writer_pid": 200}},
    })
    drain_path = tmp_path / "gateway_restart_drain.json"

    def write_count(count):
        drain_path.write_text(json.dumps({
            "pid": 100, "restart_requested": True, "active_agents": count,
            "active_work": [],
        }), encoding="utf-8")

    write_count(2)
    alive = iter([True, False])
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: next(alive))
    emitted = []

    outcome = progress.observe_gateway_restart(
        100, home=tmp_path, emit=emitted.append,
        sleep=lambda _: write_count(1),
    )

    assert outcome.completed is True
    assert emitted == [
        "✓ New gateway PID 200 is up (discord connected)",
        "⏳ Old gateway PID 100: waiting on 2 active agent(s)",
        "⏳ Old gateway PID 100: waiting on 1 active agent(s)",
        "✓ Old gateway PID 100 exited — restart complete",
    ]


@pytest.mark.asyncio
async def test_real_control_socket_progress_never_signals_an_acknowledged_restart(
    tmp_path, monkeypatch,
):
    from gateway.control_socket import GatewayControlServer
    from hermes_cli import gateway_restart_progress as progress

    home = tmp_path / "home"
    home.mkdir()
    state_path = home / "gateway_state.json"
    drain_path = home / "gateway_restart_drain.json"
    state_path.write_text(json.dumps({
        "pid": 100, "gateway_state": "running", "active_agents": 0,
    }), encoding="utf-8")

    def write_drain(count):
        drain_path.write_text(json.dumps({
            "pid": 100, "restart_requested": True, "active_agents": count,
            "active_work": [{
                "kind": "chat", "session": "discord:dm:1", "elapsed_s": 65,
            }],
        }), encoding="utf-8")

    def restart(params):
        assert params == {"restart_platforms": True, "via_service": True}
        state_path.write_text(json.dumps({
            "pid": 100, "gateway_state": "draining", "restart_requested": True,
            "active_agents": 2,
            "active_work": [{
                "kind": "chat", "session": "discord:dm:1", "elapsed_s": 65,
            }],
        }), encoding="utf-8")
        return {"queued": True, "status": "restarting"}

    server = GatewayControlServer(home, verb_handlers={"restart": restart})
    assert await server.start()
    alive = iter([True, True, False])
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: next(alive))
    kill = MagicMock(side_effect=AssertionError("an acknowledged restart must not be signalled"))
    monkeypatch.setattr(progress.os, "kill", kill)
    emitted = []

    def advance_handoff(_):
        if not drain_path.exists():
            state_path.write_text(json.dumps({
                "pid": 200, "gateway_state": "running", "active_agents": 0,
                "platforms": {"discord": {"state": "connected", "writer_pid": 200}},
            }), encoding="utf-8")
            write_drain(2)
        else:
            write_drain(1)

    try:
        outcome = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: progress.restart_gateway_with_progress(
                100, home=home, via_service=True, emit=emitted.append,
                sleep=advance_handoff,
            ),
        )
    finally:
        await server.stop()

    assert outcome.completed is True and outcome.successor_pid == 200
    assert emitted == [
        "⏳ Old gateway PID 100: waiting on 2 active agent(s): "
        "chat turn discord:dm:1, running 1m05s",
        "✓ New gateway PID 200 is up (discord connected)",
        "⏳ Old gateway PID 100: waiting on 1 active agent(s): "
        "chat turn discord:dm:1, running 1m05s",
        "✓ Old gateway PID 100 exited — restart complete",
    ]
    kill.assert_not_called()


def test_turn_boundary_publishes_predecessor_drain_snapshot(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kwargs: None)
    runner = object.__new__(GatewayRunner)
    runner._restart_requested = True
    runner._active_work_count = lambda: 2
    runner._describe_active_work = lambda: [
        {"kind": "chat", "session": "discord:dm:1", "elapsed_s": 65},
    ]

    runner._persist_active_agents()

    payload = json.loads((tmp_path / "gateway_restart_drain.json").read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["restart_requested"] is True
    assert payload["active_agents"] == 2
    assert payload["active_work"][0]["session"] == "discord:dm:1"


def test_observer_waits_for_successor_when_old_gateway_exits_first(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    records = iter([
        {
            "pid": 100,
            "gateway_state": "draining",
            "restart_requested": True,
            "active_agents": 1,
        },
        {},
        {
            "pid": 200,
            "gateway_state": "running",
            "platforms": {"discord": {"state": "connected", "writer_pid": 200}},
        },
    ])
    monkeypatch.setattr(progress, "_read_status", lambda home: next(records))
    alive = iter([True, False, False])
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: next(alive))
    emitted = []

    outcome = progress.observe_gateway_restart(
        100, home=tmp_path, emit=emitted.append, sleep=lambda _: None,
    )

    assert outcome.completed is True
    assert outcome.successor_pid == 200
    assert emitted == [
        "⏳ Old gateway PID 100: waiting on 1 active agent(s)",
        "✓ New gateway PID 200 is up (discord connected)",
        "✓ Old gateway PID 100 exited — restart complete",
    ]


def test_observer_announces_successor_after_its_platforms_connect(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    records = iter([
        {
            "pid": 200, "gateway_state": "running",
            "platforms": {"discord": {"state": "connecting", "writer_pid": 200}},
        },
        {
            "pid": 200, "gateway_state": "running",
            "platforms": {"discord": {"state": "connected", "writer_pid": 200}},
        },
    ])
    monkeypatch.setattr(progress, "_read_status", lambda home: next(records))
    alive = iter([True, False])
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: next(alive))
    emitted = []

    outcome = progress.observe_gateway_restart(
        100, home=tmp_path, emit=emitted.append, sleep=lambda _: None,
    )

    assert outcome.completed is True
    assert emitted == [
        "✓ New gateway PID 200 is up (discord connected)",
        "✓ Old gateway PID 100 exited — restart complete",
    ]


def test_observer_excludes_platforms_preserved_from_the_predecessor(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 200,
        "gateway_state": "running",
        "platforms": {
            "discord": {"state": "connected", "writer_pid": 200},
            "telegram": {"state": "connected", "writer_pid": 100},
            "slack": {"state": "connecting", "writer_pid": 200},
        },
    })
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: False)
    emitted = []

    outcome = progress.observe_gateway_restart(
        100, home=tmp_path, emit=emitted.append, sleep=lambda _: None,
    )

    assert outcome.completed is True
    assert emitted == [
        "✓ New gateway PID 200 is up (discord connected)",
        "✓ Old gateway PID 100 exited — restart complete",
    ]


def test_restart_request_uses_control_socket_before_signal(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "running",
    })
    query = MagicMock(return_value={"queued": True, "status": "restarting"})
    kill = MagicMock()
    monkeypatch.setattr(progress, "query_gateway_control", query)
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True and outcome.attached is False
    query.assert_called_once_with(
        tmp_path, "restart",
        params={"restart_platforms": True, "via_service": True},
    )
    kill.assert_not_called()


def test_restart_request_falls_back_to_sigusr1_for_older_gateway(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "running",
    })
    monkeypatch.setattr(progress, "query_gateway_control", lambda *a, **k: None)
    kill = MagicMock()
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=False)

    assert outcome.accepted is True
    kill.assert_called_once_with(100, progress.signal.SIGUSR1)


def test_socket_reported_restart_is_attached_without_another_signal(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "running",
    })
    monkeypatch.setattr(progress, "query_gateway_control", lambda *a, **k: {
        "queued": False, "status": "already_in_progress",
    })
    kill = MagicMock()
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True and outcome.attached is True
    kill.assert_not_called()


def test_socket_reported_chained_restart_is_attached_without_another_signal(
    tmp_path, monkeypatch
):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "running",
    })
    query = MagicMock(return_value={"queued": True, "status": "chained"})
    monkeypatch.setattr(progress, "query_gateway_control", query)
    kill = MagicMock()
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True and outcome.attached is True
    query.assert_called_once()
    kill.assert_not_called()


def test_committed_restart_attaches_without_sending_another_request(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "draining", "restart_requested": True,
    })
    query = MagicMock()
    kill = MagicMock()
    monkeypatch.setattr(progress, "query_gateway_control", query)
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True
    assert outcome.attached is True
    query.assert_not_called()
    kill.assert_not_called()


def test_attach_uses_predecessor_snapshot_after_successor_owns_runtime_status(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 200, "gateway_state": "running",
    })
    (tmp_path / "gateway_restart_drain.json").write_text(json.dumps({
        "pid": 100, "restart_requested": True, "active_agents": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: True)
    query = MagicMock()
    kill = MagicMock()
    monkeypatch.setattr(progress, "query_gateway_control", query)
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True
    assert outcome.attached is True
    query.assert_not_called()
    kill.assert_not_called()


def test_attach_does_not_restart_successor_when_predecessor_just_exited(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 200, "gateway_state": "running",
    })
    (tmp_path / "gateway_restart_drain.json").write_text(json.dumps({
        "pid": 100, "restart_requested": True, "active_agents": 0,
    }), encoding="utf-8")
    monkeypatch.setattr(progress, "_pid_exists", lambda pid: False)
    query = MagicMock()
    kill = MagicMock()
    monkeypatch.setattr(progress, "query_gateway_control", query)
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(100, home=tmp_path, via_service=True)

    assert outcome.accepted is True and outcome.attached is True
    query.assert_not_called()
    kill.assert_not_called()


def test_systemd_path_uses_non_destructive_progress_observer(monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    restart = MagicMock(return_value=progress.RestartOutcome(accepted=True, completed=True))
    monkeypatch.setattr(progress, "restart_gateway_with_progress", restart)
    legacy = MagicMock(side_effect=AssertionError("legacy bounded waiter must not run"))
    monkeypatch.setattr(gw, "_graceful_restart_via_sigusr1", legacy)

    assert gw._systemd_graceful_restart_action(False, 100) is None
    restart.assert_called_once_with(100, via_service=True)
    legacy.assert_not_called()


def test_update_path_uses_the_same_non_destructive_observer(monkeypatch):
    from hermes_cli import gateway_restart_progress as progress
    from hermes_cli import update_cmd_fleet as fleet

    restart = MagicMock(return_value=progress.RestartOutcome(accepted=True, completed=True))
    monkeypatch.setattr(progress, "restart_gateway_with_progress", restart)
    monkeypatch.setattr(gw, "_is_pid_ancestor_of_current_process", lambda pid: False)
    monkeypatch.setattr(gw, "probe_gateway_loop_liveness", lambda pid: gw.GATEWAY_LOOP_ALIVE)

    assert fleet._drain_or_signal_gateway_for_update(100, 1.0, "hermes-gateway") is True
    restart.assert_called_once_with(100, via_service=True, home=None, from_update=True)


def test_update_request_marks_update_drain_and_uses_sigusr1(tmp_path, monkeypatch):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "_read_status", lambda home: {
        "pid": 100, "gateway_state": "running",
    })
    marker = MagicMock(return_value=True)
    query = MagicMock()
    kill = MagicMock()
    monkeypatch.setattr("gateway.status.write_update_restart_marker", marker)
    monkeypatch.setattr(progress, "query_gateway_control", query)
    monkeypatch.setattr(progress.os, "kill", kill)

    outcome = progress.request_gateway_restart(
        100, home=tmp_path, via_service=True, from_update=True,
    )

    assert outcome.accepted is True
    marker.assert_called_once_with(100, target_home=tmp_path)
    kill.assert_called_once_with(100, progress.signal.SIGUSR1)
    query.assert_not_called()


def test_ctrl_c_detaches_without_force(monkeypatch, capsys):
    from hermes_cli import gateway_restart_progress as progress

    monkeypatch.setattr(progress, "request_gateway_restart", lambda *a, **k: (
        progress.RestartOutcome(accepted=True, attached=False)
    ))
    monkeypatch.setattr(progress, "observe_gateway_restart", MagicMock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(gw, "_systemd_reset_and_run", MagicMock())

    assert gw._systemd_graceful_restart_action(False, 100) is None
    assert "detach" in capsys.readouterr().out.lower()
    gw._systemd_reset_and_run.assert_not_called()


def test_force_help_warns_that_active_chats_are_interrupted(capsys):
    from hermes_cli.subcommands.gateway import build_gateway_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_gateway_parser(
        parser.add_subparsers(), cmd_gateway=lambda: None,
        cmd_proxy=lambda: None, cmd_gateway_enroll=lambda: None,
    )
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["gateway", "restart", "--help"])

    assert exc_info.value.code == 0
    assert "interrupts active chats" in capsys.readouterr().out


def _launchd_update_sibling(monkeypatch, drain_result):
    from hermes_cli import update_cmd_fleet as fleet

    current = "ai.hermes.gateway-current"
    sibling = "ai.hermes.gateway-sibling"
    monkeypatch.setattr(gw, "get_launchd_label", lambda: current)
    monkeypatch.setattr(gw, "launchd_gateway_labels_for_install", lambda: [current, sibling])
    monkeypatch.setattr(
        gw, "_locate_launchd_gateway_service",
        lambda label: ("gui/501", 300) if label == sibling else (None, None),
    )
    monkeypatch.setattr(
        fleet, "_restart_launchd_gateway_after_update",
        lambda supervision_verify: ([current], []),
    )
    drain = MagicMock(return_value=drain_result)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", drain)
    monkeypatch.setattr(fleet, "_gateway_home_for_pid", lambda pid: None)
    # The retired waiter stays mocked so the fail-before run cannot signal a test process.
    monkeypatch.setattr(gw, "_graceful_restart_via_sigusr1", lambda *a, **k: bool(drain_result))
    monkeypatch.setattr(gw, "_wait_for_launchd_service_pid", lambda *a, **k: True)
    kickstart = MagicMock()
    monkeypatch.setattr(gw, "_launchd_kickstart", kickstart)
    restarted, failed = [], []

    fleet._restart_macos_launchd_gateways(restarted, failed, drain_budget=1.0)
    return drain, kickstart, restarted, failed


def test_update_launchd_sibling_uses_the_same_non_destructive_observer(monkeypatch):
    drain, kickstart, restarted, failed = _launchd_update_sibling(monkeypatch, True)

    drain.assert_called_once_with(300, 1.0, "ai.hermes.gateway-sibling")
    kickstart.assert_not_called()
    assert restarted == ["ai.hermes.gateway-current", "ai.hermes.gateway-sibling"]
    assert failed == []


def test_update_launchd_sibling_does_not_kickstart_an_unacknowledged_live_gateway(monkeypatch):
    drain, kickstart, restarted, failed = _launchd_update_sibling(monkeypatch, None)

    drain.assert_called_once_with(300, 1.0, "ai.hermes.gateway-sibling")
    kickstart.assert_not_called()
    assert restarted == ["ai.hermes.gateway-current"]
    assert failed == ["ai.hermes.gateway-sibling"]
