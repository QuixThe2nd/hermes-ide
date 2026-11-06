"""Connect-failure classification + reconnect-queue escalation (OOF-156).

Connect failures used to funnel every
startup failure into an indefinitely-retried state — including permanent
failures like revoked tokens, missing privileged intents, and sidecar deps
that can never install on an immutable image. Fleet triage found agents that
had been silently "retrying" for weeks (OOF-151/152/153).

Two-part fix, both covered here:

1. Per-adapter classification: auth/permission/deterministic failures are
   classified by exception TYPE (never message text) as ``retryable=False``
   so they exit via the existing non-retryable fatal path.
2. Gateway escalation: platforms continuously in the reconnect queue past a
   threshold get ``needs_attention`` flagged in runtime status. Retries never
   stop — the deliberate removal of auto-pause stands (a transient outage
   must self-heal without operator action).
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import (
    GatewayRunner,
    _reconnect_needs_attention,
)


# ── Discord: connect exception classification ──────────────────────────


class LoginFailure(Exception):
    """Name-matched stand-in for discord.LoginFailure."""


class PrivilegedIntentsRequired(Exception):
    """Name-matched stand-in for discord.PrivilegedIntentsRequired."""


def _discord_classifier():
    from plugins.platforms.discord.adapter import DiscordAdapter

    # Instance-bound (the intents branch reads the adapter's allowlists to
    # tailor its guidance); a bare instance with empty allowlists suffices.
    adapter = object.__new__(DiscordAdapter)
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    return adapter._classify_connect_exception


class TestDiscordConnectClassification:
    def test_login_failure_is_terminal(self):
        code, message, retryable = _discord_classifier()(LoginFailure("Improper token"))
        assert code == "discord_auth_error"
        assert retryable is False
        assert "Developer Portal" in message

    def test_privileged_intents_is_terminal(self):
        code, message, retryable = _discord_classifier()(
            PrivilegedIntentsRequired("shard 0 requested privileged intents")
        )
        assert code == "discord_intents_required"
        assert retryable is False
        assert "Message Content Intent" in message

    def test_unknown_exception_is_retryable_with_explicit_code(self):
        # The old behavior set NO fatal code at all, which the gateway read
        # as "probably transient". Every failure must now carry a code.
        code, message, retryable = _discord_classifier()(OSError("connection reset"))
        assert code == "discord_connect_error"
        assert retryable is True

    def test_auth_message_text_does_not_classify(self):
        code, _message, retryable = _discord_classifier()(
            RuntimeError("LoginFailure: PrivilegedIntentsRequired")
        )
        assert code == "discord_connect_error"
        assert retryable is True


# ── Gateway: needs_attention escalation ────────────────────────────────


class TestReconnectNeedsAttention:
    def test_fresh_entry_is_not_flagged_and_gets_stamped(self):
        # In-flight upgrade path: entries queued before queued_at existed are
        # treated as newly queued, not instantly escalated.
        info = {"attempts": 3}
        now = time.monotonic()
        assert _reconnect_needs_attention(info, now) is False
        assert info["queued_at"] == now

    def test_below_threshold_is_not_flagged(self):
        now = time.monotonic()
        info = {"queued_at": now - 60}
        assert _reconnect_needs_attention(info, now) is False

    def test_past_threshold_is_flagged(self):
        import gateway.run as run_module

        now = time.monotonic()
        info = {"queued_at": now - (run_module._RECONNECT_ATTENTION_AFTER_SECONDS + 1)}
        assert _reconnect_needs_attention(info, now) is True

    def test_zero_threshold_disables_escalation(self, monkeypatch):
        import gateway.run as run_module

        monkeypatch.setattr(run_module, "_RECONNECT_ATTENTION_AFTER_SECONDS", 0)
        info = {"queued_at": time.monotonic() - 999999}
        assert _reconnect_needs_attention(info, time.monotonic()) is False


def _make_runner():
    """Minimal GatewayRunner via object.__new__ (same pattern as
    test_platform_reconnect.py)."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="test")}
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._shutdown_all_gateway_honcho = lambda: None
    runner.session_store = MagicMock()
    return runner


class TestWatcherAttentionEscalation:
    @pytest.mark.asyncio
    async def test_watcher_flags_long_queued_platform_and_keeps_retrying(self, monkeypatch):
        import gateway.run as run_module

        runner = _make_runner()
        status_writes = []
        monkeypatch.setattr(
            runner,
            "_update_platform_runtime_status",
            lambda platform, **kw: status_writes.append((platform, kw)),
        )

        threshold = run_module._RECONNECT_ATTENTION_AFTER_SECONDS
        runner._failed_platforms[Platform.DISCORD] = {
            "config": PlatformConfig(enabled=True, token="test"),
            "attempts": 40,
            # Not yet due for a retry — escalation must not depend on the
            # backoff schedule lining up.
            "next_retry": time.monotonic() + 300,
            "queued_at": time.monotonic() - threshold - 10,
        }

        real_sleep = asyncio.sleep
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                runner._running = False
            await real_sleep(0)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await runner._platform_reconnect_watcher()

        attention = [kw for _p, kw in status_writes if kw.get("needs_attention")]
        assert attention, f"expected a needs_attention status write, got {status_writes!r}"
        assert attention[0]["platform_state"] == "retrying"
        assert attention[0].get("retrying_since")
        # Platform must STILL be queued — escalation is a signal, never a
        # circuit breaker.
        assert Platform.DISCORD in runner._failed_platforms
        assert runner._failed_platforms[Platform.DISCORD].get("attention_flagged") is True

    @pytest.mark.asyncio
    async def test_watcher_flags_only_once(self, monkeypatch):
        import gateway.run as run_module

        runner = _make_runner()
        status_writes = []
        monkeypatch.setattr(
            runner,
            "_update_platform_runtime_status",
            lambda platform, **kw: status_writes.append((platform, kw)),
        )

        threshold = run_module._RECONNECT_ATTENTION_AFTER_SECONDS
        runner._failed_platforms[Platform.DISCORD] = {
            "config": PlatformConfig(enabled=True, token="test"),
            "attempts": 40,
            "next_retry": time.monotonic() + 300,
            "queued_at": time.monotonic() - threshold - 10,
        }

        real_sleep = asyncio.sleep
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count > 3:
                runner._running = False
            await real_sleep(0)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await runner._platform_reconnect_watcher()

        attention = [kw for _p, kw in status_writes if kw.get("needs_attention")]
        assert len(attention) == 1, (
            f"needs_attention must be written once per episode, got {len(attention)}"
        )


# ── Status file: new platform fields round-trip ────────────────────────


class TestRuntimeStatusAttentionFields:
    def test_needs_attention_and_retrying_since_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from gateway import status as status_module

        status_module.write_runtime_status(
            platform="discord",
            platform_state="retrying",
            error_code="discord_connect_error",
            error_message="boom",
            needs_attention=True,
            retrying_since="2026-08-11T00:00:00+00:00",
        )
        payload = status_module.read_runtime_status()
        platform = payload["platforms"]["discord"]
        assert platform["needs_attention"] is True
        assert platform["retrying_since"] == "2026-08-11T00:00:00+00:00"

        # Reconnect clears both.
        status_module.write_runtime_status(
            platform="discord",
            platform_state="connected",
            error_code=None,
            error_message=None,
            needs_attention=False,
            retrying_since=None,
        )
        payload = status_module.read_runtime_status()
        platform = payload["platforms"]["discord"]
        assert platform["needs_attention"] is False
        assert platform["retrying_since"] is None
