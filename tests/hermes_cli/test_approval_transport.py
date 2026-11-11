"""Approval transport plugin contract and fail-closed host routing."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
import yaml

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _manifest(name: str = "fixture-approval") -> PluginManifest:
    return PluginManifest(
        name=name,
        version="1.0.0",
        description="fixture",
        source="user",
        key=name,
    )


def _context(manager: PluginManager, name: str = "fixture-approval") -> PluginContext:
    return PluginContext(_manifest(name), manager)


def _request():
    from hermes_cli.approval_transport import ApprovalRequest

    return ApprovalRequest.create(
        command="rm -rf /tmp/example",
        description="recursive delete",
        pattern_key="rm_recursive",
        pattern_keys=("rm_recursive",),
        session_key="session-a",
        surface="cli",
        allow_session=True,
        allow_permanent=True,
    )


def test_context_registers_owned_approval_transport():
    manager = PluginManager()
    callback = lambda request: request.respond("deny")

    _context(manager).register_approval_transport("phone", callback)

    registered = manager.get_approval_transport("phone")
    assert registered is not None
    assert registered.name == "phone"
    assert registered.plugin_id == "fixture-approval"
    assert registered.present is callback


def test_transport_names_are_unique_and_builtin_is_reserved():
    manager = PluginManager()
    _context(manager, "first").register_approval_transport("phone", lambda request: None)

    with pytest.raises(ValueError, match="already registered"):
        _context(manager, "second").register_approval_transport("phone", lambda request: None)
    with pytest.raises(ValueError, match="reserved"):
        _context(manager).register_approval_transport("builtin", lambda request: None)


def test_force_reload_clears_transport_registry(monkeypatch):
    manager = PluginManager()
    _context(manager).register_approval_transport("phone", lambda request: None)
    manager._discovered = True
    monkeypatch.setattr(manager, "_discover_and_load_inner", lambda: None)

    manager.discover_and_load(force=True)

    assert manager.get_approval_transport("phone") is None


def test_transport_registry_is_manager_and_profile_isolated(monkeypatch, tmp_path):
    first = PluginManager()
    second = PluginManager()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "work"))
    _context(first).register_approval_transport("phone", lambda request: None)

    assert first.get_approval_transport("phone") is not None
    assert second.get_approval_transport("phone") is None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "personal"))
    assert first.get_approval_transport("phone") is None


def test_host_accepts_bound_sync_and_async_decisions():
    from hermes_cli.approval_transport import invoke_approval_transport

    request = _request()
    sync_result = invoke_approval_transport(
        lambda received: received.respond("session"), request, timeout_seconds=1
    )

    async def present(received):
        await asyncio.sleep(0)
        return received.respond("once")

    async_result = invoke_approval_transport(present, request, timeout_seconds=1)

    assert sync_result.choice == "session"
    assert sync_result.failure is None
    assert async_result.choice == "once"
    assert async_result.failure is None


def test_host_rejects_scope_not_offered_by_request():
    from hermes_cli.approval_transport import ApprovalRequest, invoke_approval_transport

    request = ApprovalRequest.create(
        command="dangerous",
        description="dangerous",
        pattern_key="danger",
        pattern_keys=("danger",),
        session_key="session-a",
        surface="cli",
        allow_session=False,
        allow_permanent=False,
    )

    result = invoke_approval_transport(
        lambda received: received.respond("always"), request, timeout_seconds=1
    )

    assert result.choice == "deny"
    assert result.failure == "invalid"


@pytest.mark.parametrize(
    ("present", "failure"),
    [
        (lambda request: {"choice": "once"}, "invalid"),
        (lambda request: request.respond("bogus"), "invalid"),
        (
            lambda request: type(request.respond("once"))(
                request_id="stale",
                request_digest=request.digest,
                choice="once",
            ),
            "stale",
        ),
        (
            lambda request: type(request.respond("once"))(
                request_id=request.request_id,
                request_digest="changed",
                choice="once",
            ),
            "stale",
        ),
    ],
)
def test_host_rejects_invalid_or_stale_decisions(present, failure):
    from hermes_cli.approval_transport import invoke_approval_transport

    result = invoke_approval_transport(present, _request(), timeout_seconds=1)

    assert result.choice == "deny"
    assert result.failure == failure


def test_host_timeout_and_exception_deny_without_waiting_forever():
    from hermes_cli.approval_transport import invoke_approval_transport

    def hangs(_request):
        time.sleep(1)

    def crashes(_request):
        raise RuntimeError("transport offline")

    started = time.monotonic()
    timeout = invoke_approval_transport(hangs, _request(), timeout_seconds=0.02)
    exception = invoke_approval_transport(crashes, _request(), timeout_seconds=1)

    assert time.monotonic() - started < 0.5
    assert (timeout.choice, timeout.failure) == ("deny", "timeout")
    assert (exception.choice, exception.failure) == ("deny", "error")


def test_host_transport_wait_is_interruptible_and_pollable():
    from hermes_cli.approval_transport import invoke_approval_transport

    polls = []

    def hangs(_request):
        time.sleep(1)

    result = invoke_approval_transport(
        hangs,
        _request(),
        timeout_seconds=1,
        poll_interval=0.01,
        on_poll=lambda: polls.append(1),
        is_interrupted=lambda: len(polls) >= 1,
    )

    assert result.choice == "deny"
    assert result.failure == "interrupted"
    assert polls


def test_host_rejects_decision_completed_after_deadline(monkeypatch):
    import hermes_cli.approval_transport as transport_module

    main_thread = threading.get_ident()

    def clock():
        # The host starts at t=0 with a one-second budget. The worker reports
        # completion at t=2 before the host dequeues the result. Acceptance is
        # bound to completion time, not to a queue/scheduler race.
        return 0.0 if threading.get_ident() == main_thread else 2.0

    monkeypatch.setattr(transport_module.time, "monotonic", clock)

    result = transport_module.invoke_approval_transport(
        lambda request: request.respond("once"),
        _request(),
        timeout_seconds=1,
    )

    assert result.choice == "deny"
    assert result.failure == "timeout"


def test_host_caps_hung_transport_workers():
    from hermes_cli.approval_transport import (
        _MAX_ACTIVE_TRANSPORT_WORKERS,
        invoke_approval_transport,
    )

    release = threading.Event()

    def hangs(_request):
        release.wait()

    try:
        results = [
            invoke_approval_transport(hangs, _request(), timeout_seconds=0.001)
            for _ in range(_MAX_ACTIVE_TRANSPORT_WORKERS + 1)
        ]
    finally:
        release.set()

    assert results[-1].choice == "deny"
    assert results[-1].failure == "busy"


def test_hardline_blocks_before_any_transport():
    from tools import approval

    manager = PluginManager()
    calls = []
    _context(manager).register_approval_transport(
        "phone", lambda request: calls.append(request) or request.respond("once")
    )

    result = approval.check_all_command_guards("rm -rf /", "local")

    assert result["approved"] is False
    assert calls == []
