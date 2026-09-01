"""Tests for the async-delivery capability gate (issue #10760).

Stateless request/response adapters (the API server / WebUI path) cannot route
a background completion back to the agent after a turn ends — there is no
persistent channel and ``APIServerAdapter.send()`` is a no-op stub. So tools
that promise async delivery (``terminal`` notify_on_complete / watch_patterns,
``delegate_agent`` background=True) must refuse the promise on that path instead
of silently registering a watcher that never fires.

This is wired through:
  - ``BasePlatformAdapter.supports_async_delivery`` (default True)
  - ``APIServerAdapter.supports_async_delivery = False``
  - ``gateway.session_context._SESSION_ASYNC_DELIVERY`` contextvar +
    ``async_delivery_supported()`` helper, bound per-session.

These are behavior/invariant tests (how the capability relates to the channel),
not snapshots of a current value.
"""

import json

import pytest

from gateway.session_context import (
    async_delivery_supported,
    clear_session_vars,
    get_session_env,
    reset_session_vars,
    set_session_vars,
)


# ---------------------------------------------------------------------------
# Capability helper
# ---------------------------------------------------------------------------

class TestAsyncDeliverySupported:
    def test_default_unbound_is_supported(self):
        """CLI / cron / unaware paths never bind the var -> supported."""
        assert async_delivery_supported() is True


    def test_set_false_is_unsupported(self):
        tokens = set_session_vars(
            platform="api_server",
            chat_id="sess1",
            session_key="sess1",
            async_delivery=False,
        )
        try:
            assert async_delivery_supported() is False
            # Platform must still be readable for routing/diagnostics even
            # though delivery is unsupported.
            assert get_session_env("HERMES_SESSION_PLATFORM") == "api_server"
        finally:
            clear_session_vars(tokens)


# ---------------------------------------------------------------------------
# Stateless runners — issues #53027 / #63142
# ---------------------------------------------------------------------------

class TestDeclareStatelessChannel:
    """``hermes -z`` and cron cannot receive a completion after their turn ends.

    Cron clears the ``HERMES_SESSION_*`` routing keys, so an async delegation's
    completion event carries ``session_key=""`` and the gateway watcher drops it
    for lack of routing metadata; either way the job's final response has already
    shipped. One-shot simply exits. Both must bind the capability, or
    ``delegate_agent`` is forced background and every subagent result is lost.
    """


    def test_declare_does_not_engage_full_session_context(self):
        """The helper binds ONLY the capability.

        ``set_session_vars`` latches ``_session_context_engaged``, which flips the
        subprocess env bridge to ContextVar-authoritative. A pure single-process
        one-shot must not trigger that as a side effect of declaring a capability.
        """
        from gateway import session_context as sc

        reset_session_vars()
        engaged_before = sc._session_context_engaged
        try:
            sc.declare_stateless_channel()
            assert sc._session_context_engaged is engaged_before
        finally:
            reset_session_vars()


class TestStatelessChannelRejectsBackgroundDelegation:
    """The behavioral contract: a stateless channel must refuse background work.

    This is the regression that #53027 / #63142 describe — a background dispatch
    on a channel that can never deliver the completion. The contract is NOT a
    silent inline fallback (that would run a foreground fan-out the model never
    asked for): the call fails clearly, starts no work, and points the caller
    at the foreground mode.
    """

    def test_background_delegation_fails_clearly_when_channel_is_stateless(
        self, monkeypatch
    ):
        import tools.delegate_tool as dt
        from gateway.session_context import declare_stateless_channel

        class _Parent:
            _delegate_depth = 0
            _subagent_id = None

        built = []
        ran = []
        dispatched = []

        def _fake_build(**kw):
            built.append(kw)
            return type("C", (), {"_subagent_id": "s1"})()

        def _fake_dispatch(*a, **kw):
            dispatched.append(kw)
            return {"delegation_id": "deleg_x"}

        def _child(task_index, goal, child=None, parent_agent=None, **kw):
            ran.append(goal)
            return {
                "task_index": 0, "status": "completed", "summary": f"done: {goal}",
                "api_calls": 1, "duration_seconds": 0.1, "model": "m",
                "exit_reason": "completed",
            }

        creds = {
            "model": "m", "provider": None, "base_url": None, "api_key": None,
            "api_mode": None, "command": None, "args": None,
        }
        monkeypatch.setattr(dt, "_build_child_agent", _fake_build)
        monkeypatch.setattr(dt, "_run_single_child", _child)
        monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
        monkeypatch.setattr(
            "tools.async_delegation.dispatch_async_delegation_batch", _fake_dispatch
        )

        reset_session_vars()
        try:
            declare_stateless_channel()
            out = dt.delegate_agent(
                goal="review the spec", background=True, parent_agent=_Parent()
            )
        finally:
            reset_session_vars()

        parsed = json.loads(out)
        # Fail clearly: an error result, never a dispatch receipt or work
        # product smuggled in as an inline run.
        assert parsed.get("error"), "stateless channel must reject background=true"
        assert parsed.get("status") != "dispatched"
        assert "results" not in parsed
        # Start no work: no child built, no child run, nothing dispatched.
        assert not built, "no child agent may be constructed before the rejection"
        assert not ran, "no child may run before the rejection"
        assert not dispatched, "stateless channel must not dispatch a detached child"
        # Point at the way out: run it in the foreground this turn.
        error_text = parsed["error"].lower()
        assert "no work was started" in error_text
        assert "omit `background`" in error_text
        assert "foreground" in error_text


# ---------------------------------------------------------------------------
# Adapter capability flag
# ---------------------------------------------------------------------------

class TestAdapterCapabilityFlag:


    def test_api_server_bind_chokepoint_hardwires_no_delivery(self):
        """Every API-server agent-entry path binds through
        _bind_api_server_session, which hardwires async_delivery=False — a new
        route physically cannot reintroduce the silent no-op (#10760)."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.session_context import clear_session_vars, get_session_env

        tokens = APIServerAdapter._bind_api_server_session(
            chat_id="c1", session_key="sk1", session_id="sid1"
        )
        try:
            assert async_delivery_supported() is False
            assert get_session_env("HERMES_SESSION_PLATFORM") == "api_server"
        finally:
            clear_session_vars(tokens)


# ---------------------------------------------------------------------------
# terminal_tool: refuses to register a watcher on unsupported sessions
# ---------------------------------------------------------------------------

class TestTerminalNotifyGate:
    @pytest.fixture(autouse=True)
    def _clean_watchers(self):
        from tools.process_registry import process_registry

        process_registry.pending_watchers = []
        yield
        process_registry.pending_watchers = []

    def _run_bg(self, command):
        from tools.terminal_tool import terminal_tool

        return json.loads(
            terminal_tool(command=command, background=True, notify_on_complete=True)
        )

    def test_api_server_skips_watcher_and_notes(self):
        from tools.process_registry import process_registry

        tokens = set_session_vars(
            platform="api_server", chat_id="s1", session_key="s1", async_delivery=False
        )
        try:
            d = self._run_bg("sleep 30 && echo DONE")
        finally:
            clear_session_vars(tokens)

        assert d.get("notify_on_complete") is False
        assert d.get("notify_unsupported"), "must explain the limitation"
        assert "poll" in d["notify_unsupported"].lower()
        assert len(process_registry.pending_watchers) == 0


