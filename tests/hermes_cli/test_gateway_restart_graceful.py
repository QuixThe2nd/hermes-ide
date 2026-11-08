"""Graceful systemd restarts must not turn an observation timeout into a kill."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gateway.status as gateway_status
import hermes_cli.update_cmd_drain_report as drain_report
from hermes_cli import gateway as gw
from hermes_cli.gateway_restart_progress import RestartOutcome


@pytest.fixture
def graceful_timeout(monkeypatch):
    pid = 4242
    monkeypatch.setattr(gw, "_systemd_scope_preamble", lambda *a, **k: False)
    monkeypatch.setattr(gw, "refresh_systemd_unit_if_needed", lambda **k: None)
    monkeypatch.setattr(gateway_status, "get_running_pid", lambda: pid)
    monkeypatch.setattr(gw, "_systemd_main_pid", lambda **k: pid)
    monkeypatch.setattr(gw, "_get_restart_exit_wait_budget", lambda: 30.0)
    monkeypatch.setattr(drain_report, "drain_progress_reporter", lambda **k: None)
    graceful = MagicMock(return_value=RestartOutcome(accepted=False))
    monkeypatch.setattr("hermes_cli.gateway_restart_progress.restart_gateway_with_progress", graceful)
    probe = MagicMock(return_value=gw.GATEWAY_LOOP_ALIVE)
    monkeypatch.setattr(gw, "probe_gateway_loop_liveness", probe)
    reset_and_run = MagicMock()
    monkeypatch.setattr(gw, "_systemd_reset_and_run", reset_and_run)
    return SimpleNamespace(graceful=graceful, probe=probe, reset_and_run=reset_and_run)


@pytest.mark.parametrize("verdict", [gw.GATEWAY_LOOP_ALIVE, gw.GATEWAY_LOOP_UNKNOWN])
def test_acknowledged_restart_leaves_live_or_uncertain_service_untouched(
    graceful_timeout, monkeypatch, capsys, verdict
):
    graceful_timeout.probe.return_value = verdict
    graceful_timeout.graceful.return_value = RestartOutcome(accepted=True, completed=False)

    gw.systemd_restart()

    graceful_timeout.reset_and_run.assert_not_called()
    output = capsys.readouterr().out
    assert "reporting until the old process exits" in output
    graceful_timeout.graceful.assert_called_once_with(4242, via_service=True)


def test_explicit_force_allows_escalation_after_graceful_timeout(graceful_timeout):
    gw.systemd_restart(force=True)

    graceful_timeout.reset_and_run.assert_called_once_with(
        "restart", system=False, previous_pid=4242
    )


def test_proven_wedged_loop_allows_escalation_after_graceful_timeout(graceful_timeout):
    graceful_timeout.probe.side_effect = [gw.GATEWAY_LOOP_ALIVE, gw.GATEWAY_LOOP_WEDGED]

    gw.systemd_restart()

    graceful_timeout.reset_and_run.assert_called_once_with(
        "restart", system=False, previous_pid=4242
    )


def test_restart_request_send_failure_is_not_reported_as_a_pending_drain(graceful_timeout):

    with pytest.raises(SystemExit) as exc_info:
        gw._systemd_graceful_restart_action(False, 4242)

    assert exc_info.value.code == 1
    graceful_timeout.reset_and_run.assert_not_called()


def test_cmd_restart_forwards_explicit_force_to_systemd(monkeypatch):
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", lambda **k: None)
    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", lambda *a, **k: False)
    monkeypatch.setattr(gw, "_installed_service_kind_for", lambda windows: "systemd")
    service_call = MagicMock()
    monkeypatch.setattr(gw, "_service_call", service_call)

    gw._cmd_restart(SimpleNamespace(system=False, all=False, force=True))

    service_call.assert_called_once_with("systemd", "restart", False, force=True)
