"""Mission-bound gateway chats suppress the fallback-switch status notice.

A chat served by an active assistant mission (plugins/missions) faces an
end-user persona, not the operator: the one-shot fallback-observability
notice the agent emits on successful provider-fallback recovery
("🔄 Switched to fallback model: …") is plumbing there and must not be
delivered. Every other status class — and the same notice in operator
chats without an active mission — keeps today's behavior byte-for-byte.
"""

import concurrent.futures
import logging
import sys
import types

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run import (
    TurnRunner,
    _mission_chat_suppresses_status,
    _prepare_gateway_status_message,
)

# The exact shape run_agent._emit_pending_fallback_notice emits
# (agent/chat_completion_helpers.py formats the producer side).
FALLBACK_NOTICE = (
    "🔄 Switched to fallback model: stealth/ox-alpha via openrouter "
    "→ gpt-5.6-sol via openai-codex"
)

# Statuses that must be unaffected by the mission gate: they ride the same
# _status_callback_sync path and their fate is decided solely by the existing
# noise/redaction filters, mission or not.
OTHER_STATUSES = [
    "still on it",
    "⏳ Working — 3 min",
    "⏳ Retrying in 4.2s (attempt 1/3)...",
    "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
]


def _install_mission_plugin(monkeypatch, chat_id, mission):
    """Point plugins.missions at a fake with an active mission for chat_id."""
    seen = []

    def _find_active_mission_for_chat(chat):
        seen.append(chat)
        return mission if chat == chat_id else None

    fake = types.SimpleNamespace(
        find_active_mission_for_chat=_find_active_mission_for_chat
    )
    monkeypatch.setitem(sys.modules, "plugins.missions", fake)
    return seen


class _StubSource:
    def __init__(self, platform=Platform.WHATSAPP):
        self.platform = platform
        self.chat_id = "12345"


class _StubCtx:
    """Minimal TurnContext stand-in for the real _status_callback_sync path."""

    def __init__(self, chat_id="12345"):
        self.source = _StubSource()
        self._status_adapter = object()
        self._status_chat_id = chat_id
        self._status_thread_metadata = None
        self._loop_for_step = object()
        self._cleanup_progress = False

    def _run_still_current(self):
        return True


def _deliver_via_status_callback(monkeypatch, ctx, event_type, message):
    """Run the REAL TurnRunner._status_callback_sync and capture deliveries.

    Replaces the module-level coro factory + threadsafe bridge (same pattern
    as test_plugin_message_injection.py) so the test observes exactly what
    would be handed to the adapter, without an event loop.
    """
    delivered = []

    def _fake_send_coro(_adapter, chat_id, etype, content, metadata):
        delivered.append((chat_id, etype, content))
        return object()

    def _fake_schedule(coro, _loop, **_kwargs):
        future = concurrent.futures.Future()
        future.set_result(True)
        return future

    monkeypatch.setattr("gateway.run._send_or_update_status_coro", _fake_send_coro)
    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", _fake_schedule)
    TurnRunner(runner=object(), ctx=ctx)._status_callback_sync(event_type, message)
    return delivered


def test_fallback_notice_suppressed_for_mission_chat(monkeypatch):
    """Active mission → the notice is dropped before any delivery."""
    _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})

    assert _mission_chat_suppresses_status("12345", "lifecycle", FALLBACK_NOTICE)

    delivered = _deliver_via_status_callback(
        monkeypatch, _StubCtx(), "lifecycle", FALLBACK_NOTICE
    )
    assert delivered == []


def test_suppressed_notice_logs_at_debug_like_existing_branch(monkeypatch, caplog):
    """The mission suppression logs the drop like the noisy-status branch."""
    _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        _deliver_via_status_callback(
            monkeypatch, _StubCtx(), "lifecycle", FALLBACK_NOTICE
        )

    assert any(
        "status_callback suppressed for mission chat" in record.getMessage()
        for record in caplog.records
    )


def test_fallback_notice_passes_through_without_active_mission(monkeypatch):
    """No mission for the chat (or no plugin at all) → notice delivered as-is."""
    # Plugin present but this chat has no active mission.
    seen = _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})

    delivered = _deliver_via_status_callback(
        monkeypatch, _StubCtx(chat_id="99999"), "lifecycle", FALLBACK_NOTICE
    )

    assert delivered == [("99999", "lifecycle", FALLBACK_NOTICE)]
    # The chat identity is resolved by the plugin, via the string chat id.
    assert seen == ["99999"]
    # The notice is byte-identical after the normal preparation path.
    assert _prepare_gateway_status_message(
        Platform.WHATSAPP, "lifecycle", FALLBACK_NOTICE
    ) == FALLBACK_NOTICE


def test_fallback_notice_passes_through_when_plugin_absent(monkeypatch):
    """This tree ships no plugins.missions — the gate must be a no-op then."""
    monkeypatch.delitem(sys.modules, "plugins.missions", raising=False)

    assert not _mission_chat_suppresses_status("12345", "lifecycle", FALLBACK_NOTICE)

    delivered = _deliver_via_status_callback(
        monkeypatch, _StubCtx(), "lifecycle", FALLBACK_NOTICE
    )
    assert delivered == [("12345", "lifecycle", FALLBACK_NOTICE)]


def test_fallback_notice_passes_through_when_plugin_raises(monkeypatch):
    """Lookup (or import) failure must fall through to existing behavior."""

    def _boom(_chat):
        raise RuntimeError("mission store unavailable")

    monkeypatch.setitem(
        sys.modules,
        "plugins.missions",
        types.SimpleNamespace(find_active_mission_for_chat=_boom),
    )

    assert not _mission_chat_suppresses_status("12345", "lifecycle", FALLBACK_NOTICE)

    delivered = _deliver_via_status_callback(
        monkeypatch, _StubCtx(), "lifecycle", FALLBACK_NOTICE
    )
    assert delivered == [("12345", "lifecycle", FALLBACK_NOTICE)]


def test_unrelated_statuses_unchanged_for_mission_chats(monkeypatch):
    """With an active mission, every non-fallback status keeps today's fate."""
    _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})
    ctx = _StubCtx()

    for event_type, message in (
        *[("lifecycle", msg) for msg in OTHER_STATUSES],
        ("warn", "⚠️ Max retries (3) exhausted — trying fallback..."),
    ):
        with_mission = _deliver_via_status_callback(
            monkeypatch, ctx, event_type, message
        )
        assert not _mission_chat_suppresses_status(ctx._status_chat_id, event_type, message)
        # Today's behavior: heartbeats deliver, noise stays filtered — decided
        # entirely by the existing filters, never by the mission gate.
        expected = _prepare_gateway_status_message(
            ctx.source.platform, event_type, message
        )
        if expected is None:
            assert with_mission == []
        else:
            assert with_mission == [(ctx._status_chat_id, event_type, expected)]


def test_suppression_decision_runs_before_preparation(monkeypatch):
    """The mission gate drops the notice before _prepare_gateway_status_message.

    Ordering guard: suppression must not depend on (or disturb) redaction —
    the notice is dropped upstream of preparation for mission chats, and
    preparation still runs for every status that is not suppressed.
    """
    _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})

    real_prepare = gateway_run._prepare_gateway_status_message
    calls = []

    def _spy_prepare(platform, event_type, message):
        calls.append(message)
        return real_prepare(platform, event_type, message)

    monkeypatch.setattr(
        gateway_run, "_prepare_gateway_status_message", _spy_prepare
    )

    _deliver_via_status_callback(
        monkeypatch, _StubCtx(), "lifecycle", FALLBACK_NOTICE
    )
    assert calls == []

    _deliver_via_status_callback(monkeypatch, _StubCtx(), "lifecycle", "still on it")
    assert calls == ["still on it"]


def test_only_the_fallback_switch_prefix_is_in_scope(monkeypatch):
    """Lookalike statuses that merely mention fallback must not be dropped."""
    _install_mission_plugin(monkeypatch, "12345", {"id": "mission-1"})

    lookalikes = [
        "🔄 Primary model failed — switching to fallback: m2 via p2",
        "⚠️ Max retries (3) exhausted — trying fallback...",
        "🔄 Switched to fallback model (earlier): m1 via p1",
        "Note: 🔄 Switched to fallback model: m1 via p1",
        "",
    ]
    for message in lookalikes:
        assert not _mission_chat_suppresses_status("12345", "lifecycle", message)
