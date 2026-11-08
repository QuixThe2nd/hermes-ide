"""Update clients cover unit transactions without hiding manager failures."""
import subprocess

import pytest

from hermes_cli import update_cmd_fleet as fleet


def test_overlap_successor_prevents_a_second_systemd_start(tmp_path, monkeypatch):
    """A tracked overlap successor is settled; unit start would spawn a duplicate."""
    commands = []
    main_pids = iter([42, 84])

    def systemctl(cmd, *, timeout):
        commands.append(cmd)
        if "show" in cmd:
            return subprocess.CompletedProcess(cmd, 0, str(next(main_pids)), "")
        return subprocess.CompletedProcess(cmd, 0, "active", "")

    (tmp_path / "gateway_state.json").write_text(
        '{"pid": 84, "gateway_state": "running", "active_agents": 0}', encoding="utf-8"
    )
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args: True)
    monkeypatch.setattr(fleet, "_gateway_home_for_pid", lambda pid: tmp_path)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 84)
    restarted, failed = [], []

    fleet._restart_one_systemd_gateway_unit(
        "hermes-gateway-test", scope="user", scope_cmd=["systemctl", "--user"],
        drain_budget=1, _manage_cmd_cache={"user": ["systemctl", "--user"]},
        restarted_services=restarted, failed_or_stale_units=failed,
    )

    assert restarted == ["hermes-gateway-test"] and not failed
    assert not any("start" in cmd or "restart" in cmd for cmd in commands)


def test_untracked_overlap_successor_is_not_reported_as_systemd_success(tmp_path, monkeypatch):
    """A live child without the unit MainPID handover is not a completed update restart."""
    commands = []

    def systemctl(cmd, *, timeout):
        commands.append(cmd)
        output = "42" if "show" in cmd else "active"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    (tmp_path / "gateway_state.json").write_text(
        '{"pid": 84, "gateway_state": "running", "active_agents": 0}', encoding="utf-8"
    )
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args: True)
    monkeypatch.setattr(fleet, "_gateway_home_for_pid", lambda pid: tmp_path)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 84)
    restarted, failed = [], []

    fleet._restart_one_systemd_gateway_unit(
        "hermes-gateway-test", scope="user", scope_cmd=["systemctl", "--user"],
        drain_budget=1, _manage_cmd_cache={"user": ["systemctl", "--user"]},
        restarted_services=restarted, failed_or_stale_units=failed,
    )

    assert restarted == []
    assert failed == ["hermes-gateway-test"]
    assert not any("start" in cmd or "restart" in cmd for cmd in commands)


def test_active_predecessor_is_not_mistaken_for_a_systemd_replacement(tmp_path, monkeypatch):
    """An ACK plus the old active MainPID is not a completed fleet restart."""
    commands = []

    def systemctl(cmd, *, timeout):
        commands.append(cmd)
        output = "42" if "show" in cmd else "active"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    (tmp_path / "gateway_state.json").write_text(
        '{"pid": 42, "gateway_state": "draining", "active_agents": 1}', encoding="utf-8"
    )
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args: True)
    monkeypatch.setattr(fleet, "_gateway_home_for_pid", lambda pid: tmp_path)
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda *args, **kwargs: True)
    restarted, failed = [], []

    fleet._restart_one_systemd_gateway_unit(
        "hermes-gateway-test", scope="user", scope_cmd=["systemctl", "--user"],
        drain_budget=1, _manage_cmd_cache={"user": ["systemctl", "--user"]},
        restarted_services=restarted, failed_or_stale_units=failed,
    )

    assert restarted == []
    assert failed == ["hermes-gateway-test"]
    assert not any("start" in cmd or "restart" in cmd for cmd in commands)


def test_unacknowledged_manual_gateway_is_not_killed_or_swept(monkeypatch):
    """A refused handoff leaves the old process alive; elapsed time is not force."""
    from hermes_cli import gateway

    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kwargs: set())
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kwargs: [4242])
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kwargs: [])
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *args: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("an unacknowledged live gateway must not be stopped")

    monkeypatch.setattr(gateway, "kill_gateway_processes", forbidden)
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", forbidden)
    monkeypatch.setattr(gateway.os, "kill", forbidden)
    out = fleet._GatewayRestartOutcome(
        incomplete=False, phase_errors=[], pre_restart_gateway_pids=[4242],
        restarted_services=[], failed_or_stale_units=[], relaunched_profiles=[],
        externally_supervised_profiles=[], killed_pids=set(),
    )

    fleet._restart_manual_gateways(out, 1)

    assert out.failed_or_stale_units == ["manual-pid-4242"]
    assert out.killed_pids == set()


@pytest.mark.parametrize("graceful,retry", [(False, False), (False, True), (True, False), ("catchup", False)])
def test_unit_transaction_budget_preserves_scope_and_health(monkeypatch, graceful, retry):
    catchup = graceful == "catchup"
    scope = ["systemctl", "--user"] if catchup else ["systemctl", "--no-ask-password"]
    manage = scope if catchup else ["sudo", "-n", *scope]
    calls = []
    main_pids = iter([42, 84])

    def systemctl(cmd, *, timeout):
        calls.append((cmd, timeout))
        if "list-units" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "hermes-serve-test.service loaded active running", "")
        if "show" in cmd:
            assert cmd[:len(scope)] == scope
            output = str(next(main_pids, 84)) if "--property=MainPID" in cmd else "TimeoutStopUSec=70s\nTimeoutStartUSec=90s"
            return subprocess.CompletedProcess(cmd, 0, output, "")
        if "restart" in cmd or "start" in cmd:
            assert cmd[:len(manage)] == manage
            assert timeout > (90 if graceful else 160)
        return subprocess.CompletedProcess(cmd, 0, "active", "")

    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    if catchup:
        monkeypatch.setattr(fleet, "_SYSTEMD_SCOPES", (("user", scope),))
        monkeypatch.setattr("hermes_cli.gateway.supports_systemd_services", lambda: True)
        restarted, failed, scoped = [], [], set()
        fleet._restart_systemd_gateway_units(restarted, failed, scoped, 45)
        assert restarted == ["hermes-serve-test"] and not failed
        assert scoped == {"user/hermes-serve-test"}
        assert sum("restart" in cmd for cmd, _ in calls) == 1
        return
    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", lambda *a: True)
    health = iter([False, True] if retry else [True])
    monkeypatch.setattr(fleet, "_wait_for_service_active", lambda *a, **kw: next(health))
    name = "hermes-gateway-test" if graceful else "hermes-serve-test"
    restarted, failed = [], []
    fleet._restart_one_systemd_gateway_unit(
        name, scope="system", scope_cmd=scope, drain_budget=45,
        _manage_cmd_cache={"system": manage}, restarted_services=restarted,
        failed_or_stale_units=failed,
    )
    assert restarted == [name] and not failed
    expected_supervisor_verbs = 0 if graceful is True else (2 if retry else 1)
    assert sum("restart" in cmd or "start" in cmd for cmd, _ in calls) == expected_supervisor_verbs


@pytest.mark.parametrize("limits", ["", "TimeoutStopUSec=infinity\nTimeoutStartUSec=invalid", "TimeoutStopUSec=70000000\nTimeoutStartUSec=90s"])
@pytest.mark.parametrize("outcome", [0, 7, "timeout"])
def test_budget_fallback_keeps_real_errors(monkeypatch, limits, outcome):
    def systemctl(cmd, *, timeout):
        if "show" in cmd:
            return subprocess.CompletedProcess(cmd, 0, limits, "")
        if "restart" in cmd:
            assert 160 < timeout < 2**31 / 1000
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, outcome, "", "manager diagnostic")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    if outcome == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            fleet._systemctl_reset_and_restart(["systemctl"], "hermes-serve-test")
    else:
        result = fleet._systemctl_reset_and_restart(["systemctl"], "hermes-serve-test")
        assert result.returncode == outcome
        assert result.stderr == "manager diagnostic"
