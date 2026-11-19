"""Behavior tests for hub-owned overlap successor attestation."""

import os

import pytest

import gateway.overlap_registry as registry_module
from gateway.overlap_registry import OverlapSuccessorRegistry


def test_unknown_spawn_fingerprint_does_not_prune_before_live_attach(monkeypatch, tmp_path):
    successor_pid = 5150
    attached_start_time = 123456
    live_starts = {successor_pid: None, os.getpid(): 700}
    monkeypatch.setattr(
        registry_module, "get_process_start_time", lambda pid: live_starts.get(pid)
    )
    registry = OverlapSuccessorRegistry()

    with pytest.raises(RuntimeError, match="not spawned"):
        registry.attach(
            pid=9999,
            start_time=attached_start_time,
            gateway_home=str(tmp_path),
            generation_id="foreign",
            peer_pid=9999,
        )

    # Popen may return before the child's process fingerprint is available.
    registry.record_spawn(successor_pid, str(tmp_path), predecessor_pid=4242)
    assert registry.snapshot() == []

    live_starts[successor_pid] = attached_start_time
    registry.attach(
        pid=successor_pid,
        start_time=attached_start_time,
        gateway_home=str(tmp_path),
        generation_id="successor",
        peer_pid=successor_pid,
    )

    for _ in range(2):
        successors = registry.snapshot()
        assert len(successors) == 1
        assert successors[0]["pid"] == successor_pid
        assert successors[0]["start_time"] == attached_start_time
        assert successors[0]["state"] == "attached"

    # A live process with a different birth fingerprint is a reused PID.
    live_starts[successor_pid] = attached_start_time + 1
    assert registry.snapshot() == []
