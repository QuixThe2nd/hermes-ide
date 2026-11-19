"""A pending successor still attests itself to the hub after its env markers are popped."""

import os

from gateway import overlap_restart


def test_attachment_keeps_predecessor_after_env_pop(monkeypatch):
    monkeypatch.setenv(overlap_restart.OVERLAP_SUCCESSOR_ENV, "1")
    monkeypatch.setenv(overlap_restart.OVERLAP_PREDECESSOR_PID_ENV, "4242")
    overlap_restart.remember_pending_predecessor(overlap_restart.overlap_predecessor_pid())
    try:
        os.environ.pop(overlap_restart.OVERLAP_SUCCESSOR_ENV)
        os.environ.pop(overlap_restart.OVERLAP_PREDECESSOR_PID_ENV)
        assert overlap_restart.overlap_attachment()["overlap_predecessor_pid"] == 4242
        overlap_restart.forget_pending_predecessor()
        assert "overlap_predecessor_pid" not in overlap_restart.overlap_attachment()
    finally:
        overlap_restart.forget_pending_predecessor()


def test_plain_gateway_never_claims_a_predecessor(monkeypatch):
    monkeypatch.delenv(overlap_restart.OVERLAP_SUCCESSOR_ENV, raising=False)
    overlap_restart.forget_pending_predecessor()
    assert overlap_restart.overlap_predecessor_pid() is None
