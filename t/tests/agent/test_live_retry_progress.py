"""Tests for opt-in LIVE retry/fallback progress (``display.retry_progress``).

Default behavior is unchanged: ``_buffer_status`` stores retry/fallback chatter
and only ``_flush_status_buffer`` (terminal failure) surfaces it.  When the
gateway turns ``_live_retry_status`` on, the same buffered lines are ALSO
emitted live through ``status_callback("retry_progress", ...)`` so the user
sees progress during a long provider stall instead of a silent spinner.
"""

from __future__ import annotations

import time

import pytest

from run_agent import AIAgent


def _make_bare_agent(status_callback=None):
    """Construct an AIAgent without running __init__ — mirror of the pattern in
    tests/run_agent/test_retry_status_buffer.py."""
    agent = object.__new__(AIAgent)
    agent.log_prefix = ""
    agent.status_callback = status_callback
    agent.suppress_status_output = False
    agent._mute_post_response = False
    agent._executing_tools = False
    agent._print_fn = None
    return agent


def _arm_live(agent, *, seconds_since_last_emit=3600.0):
    """Turn live retry progress on and backdate the throttle window."""
    agent._live_retry_status = True
    # Pretend the previous live emission was long ago so the throttle
    # doesn't mask the emission under test.
    agent._last_live_retry_emit_ts = time.time() - seconds_since_last_emit
    agent._last_live_retry_text = None
    return agent


# ── Default off ────────────────────────────────────────────────────────────

def test_live_retry_status_defaults_off():
    """No flag set → _buffer_status must not touch status_callback."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    assert calls == []
    assert agent._retry_status_buffer == [("status", "⏳ Retrying in 4.2s (attempt 1/3)...")]


def test_live_retry_status_falsy_off():
    """Explicitly-False flag (the resolved display default) stays silent."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    agent._live_retry_status = False

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    assert calls == []


# ── Flag on ────────────────────────────────────────────────────────────────

def test_live_retry_status_emits_retry_progress_event():
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    # Emitted live exactly once, on the dedicated event type.
    assert calls == [("retry_progress", "⏳ Retrying in 4.2s (attempt 1/3)...")]
    # AND still buffered — live emission is additive, it does not replace the
    # drop-on-success / flush-on-failure semantics.
    assert agent._retry_status_buffer == [("status", "⏳ Retrying in 4.2s (attempt 1/3)...")]


def test_live_retry_status_missing_callback_is_silent():
    """status_callback=None (bare/CLI agents) must not raise."""
    agent = _make_bare_agent(None)
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    assert agent._retry_status_buffer == [("status", "⏳ Retrying in 4.2s (attempt 1/3)...")]


def test_live_retry_status_flush_and_clear_unchanged():
    """Buffer lifecycle is untouched by the live emission."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)
    emitted = []
    agent._emit_status = lambda msg: emitted.append(msg)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    # Success path: the buffered copy is dropped, nothing replayed.
    agent._clear_status_buffer()
    assert emitted == []
    assert agent._retry_status_buffer == []

    agent._buffer_status("⚠️ Rate limited — switching to fallback provider...")
    # Terminal-failure path: the buffered trace still replays in order.
    agent._flush_status_buffer()
    assert emitted == ["⚠️ Rate limited — switching to fallback provider..."]
    assert agent._retry_status_buffer == []


def test_buffer_vprint_never_emits_live():
    """_buffer_vprint stays silent-only even with the flag on."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_vprint("⚠️  API call failed")

    assert calls == []
    assert agent._retry_status_buffer == [("vprint", "⚠️  API call failed")]


# ── Throttle ───────────────────────────────────────────────────────────────

def test_throttle_suppresses_immediate_second_emission():
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")
    agent._buffer_status("⏳ Retrying in 6.4s (attempt 2/3)...")

    # Both buffered, only the first went live (<5s apart).
    assert calls == [("retry_progress", "⏳ Retrying in 4.2s (attempt 1/3)...")]
    assert len(agent._retry_status_buffer) == 2


def test_throttle_suppresses_different_text_within_window():
    """The 5s window suppresses regardless of text change — a stall produces
    many distinct retry lines and they must not become chat spam."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")
    agent._buffer_status("⚠️ Rate limited — switching to fallback provider...")

    assert len(calls) == 1


def test_throttle_suppresses_identical_text_even_after_window():
    """Byte-identical repeats are dropped forever, not just for 5s — a wedged
    provider retrying the same line every backoff tick should not re-bubble."""
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")
    # Force the window open again — identical text must still be suppressed.
    agent._last_live_retry_emit_ts = time.time() - 3600.0
    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    assert len(calls) == 1
    assert len(agent._retry_status_buffer) == 2


def test_throttle_allows_new_text_after_window():
    calls = []
    agent = _make_bare_agent(lambda event, msg: calls.append((event, msg)))
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")
    # Re-arm: pretend 5s+ have elapsed since the first live emission.
    agent._last_live_retry_emit_ts = time.time() - 5.5
    agent._buffer_status("⚠️ Rate limited — switching to fallback provider...")

    assert calls == [
        ("retry_progress", "⏳ Retrying in 4.2s (attempt 1/3)..."),
        ("retry_progress", "⚠️ Rate limited — switching to fallback provider..."),
    ]


# ── Robustness ─────────────────────────────────────────────────────────────

def test_callback_exception_is_swallowed():
    """A raising status_callback must never break the retry loop."""
    agent = _make_bare_agent(_boom)
    _arm_live(agent)

    agent._buffer_status("⏳ Retrying in 4.2s (attempt 1/3)...")

    # The buffered copy still landed — the failure was contained to delivery.
    assert agent._retry_status_buffer == [("status", "⏳ Retrying in 4.2s (attempt 1/3)...")]
    # And the throttle bookkeeping advanced, so a later line isn't double-fired.
    assert agent._last_live_retry_text == "⏳ Retrying in 4.2s (attempt 1/3)..."


def _boom(event, msg):
    raise RuntimeError("simulated status_callback failure")


# ── Display-config resolution ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "platform", ["telegram", "discord", "slack", "whatsapp", "unknown_platform"]
)
def test_retry_progress_defaults_falsy(platform):
    from gateway.display_config import resolve_display_setting

    assert not resolve_display_setting({}, platform, "retry_progress")


def test_retry_progress_global_true():
    from gateway.display_config import resolve_display_setting

    assert resolve_display_setting(
        {"display": {"retry_progress": True}}, "discord", "retry_progress"
    ) is True


def test_retry_progress_per_platform_override_wins():
    from gateway.display_config import resolve_display_setting

    cfg = {
        "display": {
            "retry_progress": True,
            "platforms": {"discord": {"retry_progress": False}},
        }
    }
    assert resolve_display_setting(cfg, "discord", "retry_progress") is False
    # Sibling platform keeps the global setting.
    assert resolve_display_setting(cfg, "telegram", "retry_progress") is True


def test_retry_progress_per_platform_opt_in_only():
    from gateway.display_config import resolve_display_setting

    cfg = {"display": {"platforms": {"discord": {"retry_progress": True}}}}
    assert resolve_display_setting(cfg, "discord", "retry_progress") is True
    assert not resolve_display_setting(cfg, "telegram", "retry_progress")


def test_retry_progress_string_values_normalise_to_bool():
    """A quoted "false"/"true" must not survive as a truthy string."""
    from gateway.display_config import resolve_display_setting

    cfg = {"display": {"retry_progress": "false"}}
    assert resolve_display_setting(cfg, "discord", "retry_progress") is False
    cfg = {"display": {"platforms": {"discord": {"retry_progress": "true"}}}}
    assert resolve_display_setting(cfg, "discord", "retry_progress") is True


# ── Gateway delivery filter ────────────────────────────────────────────────

RETRY_LINE = "⏳ Retrying in 8s (attempt 1/10)..."


@pytest.mark.parametrize("platform", ["telegram", "slack", "feishu", "discord"])
def test_gateway_retry_progress_event_bypasses_noise_filter(platform):
    """event_type="retry_progress" is an explicit user opt-in: the noisy-status
    regex that hides replayed retry chatter must NOT suppress it."""
    from gateway.run import _prepare_gateway_status_message

    out = _prepare_gateway_status_message(platform, "retry_progress", RETRY_LINE)
    assert out is not None
    assert "Retrying in 8s" in out


@pytest.mark.parametrize("platform", ["telegram", "slack", "feishu", "discord"])
def test_gateway_lifecycle_retry_chatter_stays_suppressed(platform):
    """Unchanged default: the same line on the "lifecycle" rail is dropped."""
    from gateway.run import _prepare_gateway_status_message

    assert _prepare_gateway_status_message(platform, "lifecycle", RETRY_LINE) is None


@pytest.mark.parametrize(
    "line",
    [
        "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
        "⚠️ Max retries (3) exhausted — trying fallback...",
        "⚠️ Stream drop mid tool-call detected — retry 2/3...",
        "⚠️ Stale connections from a previous provider issue — reconnecting...",
        "🗜️ Compressed 30 → 12 messages, retrying...",
    ],
)
def test_gateway_retry_progress_bypass_applies_to_all_chatter(line):
    """Every flavor of regex-suppressed retry chatter flips to visible on the
    retry_progress rail — not just the "Retrying in Ns" form."""
    from gateway.run import _prepare_gateway_status_message

    assert _prepare_gateway_status_message("telegram", "lifecycle", line) is None
    assert _prepare_gateway_status_message("telegram", "retry_progress", line) is not None


def test_gateway_retry_progress_still_redacts_secrets():
    """Bypassing the noise filter must not bypass secret redaction."""
    from gateway.run import _prepare_gateway_status_message

    secret = "sk-ABCDEF0123456789abcdef0123"
    out = _prepare_gateway_status_message(
        "telegram",
        "retry_progress",
        f"⏳ Retrying in 8s (attempt 1/10)... ({secret})",
    )
    assert out is not None
    assert secret not in out


def test_gateway_retry_progress_empty_message_returns_none():
    from gateway.run import _prepare_gateway_status_message

    assert _prepare_gateway_status_message("telegram", "retry_progress", "   ") is None
