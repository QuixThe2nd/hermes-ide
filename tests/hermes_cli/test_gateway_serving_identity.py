"""`hermes gateway status` serving-identity block.

The status command must separate the AUTHORITATIVE serving gateway from what
the supervisor and predecessor-era files say: during an overlap restart the
pid file, gateway_state.json and systemd MainPID all name the draining
predecessor, and a stale/unreachable identity must print "unknown", never a
false serving claim.
"""

from __future__ import annotations

from hermes_cli.gateway import _print_serving_identity


def _patch_sources(monkeypatch, *, identify=None, state=None, chained=None,
                   state_pid_is_live=False, state_is_stale=False, pid_live=None):
    from gateway import control_socket, restart_chain
    from gateway import status as status_mod

    monkeypatch.setattr(control_socket, "identify_gateway", lambda home, **kw: identify)
    monkeypatch.setattr(status_mod, "read_runtime_status", lambda path=None: state)
    monkeypatch.setattr(status_mod, "runtime_status_pid_is_live", lambda record: state_pid_is_live)
    monkeypatch.setattr(status_mod, "runtime_status_is_stale", lambda record, ttl_s=None: state_is_stale)
    monkeypatch.setattr(restart_chain, "peek_chained_restart", lambda: chained)
    if pid_live is not None:
        monkeypatch.setattr(status_mod, "_pid_exists", pid_live)


def test_status_prints_unknown_when_no_identity_source_answers(monkeypatch, capsys):
    _patch_sources(monkeypatch)
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "Serving gateway identity:" in out
    assert "⚠ Serving gateway: unknown" in out
    assert "✓ Serving gateway:" not in out


def test_status_prints_serving_successor_beside_draining_predecessor(monkeypatch, capsys):
    _patch_sources(
        monkeypatch,
        identify={
            "pid": 1438, "start_time": 123456, "kind": "hermes-gateway",
            "overlap": {"role": "draining-predecessor", "successor_pid": 709505},
        },
        pid_live=lambda pid: True,
    )
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "✓ Serving gateway: pid 709505" in out
    assert "pid 1438" in out
    assert "draining" in out.lower()
    assert "not an incomplete restart" in out


def test_status_reports_pending_chained_restart(monkeypatch, capsys):
    _patch_sources(
        monkeypatch,
        identify={"pid": 709505, "start_time": 123457, "kind": "hermes-gateway"},
        chained={"source": "telegram user 42 (restart)"},
        pid_live=lambda pid: True,
    )
    _print_serving_identity()
    out = capsys.readouterr().out
    assert "✓ Serving gateway: pid 709505" in out
    assert "Chained restart queued" in out
    assert "telegram user 42" in out


def test_status_silent_when_nothing_to_say(monkeypatch, capsys):
    """No gateway installed at all: no identity block, no noise."""
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        "gateway.serving_identity.serving_status_lines", lambda **kw: [])
    _print_serving_identity()
    assert capsys.readouterr().out == ""
