"""Exactly one provider-failure message per failed turn reaches chat (2026-11-12 incident).

A turn that exhausted its API retries sent TWO near-identical "⚠️ The AI model
service kept failing. Use /retry…" messages: the immediate "❌ API failed after
4 retries — …" status callback and the failed turn's final_response both
rewrote to the same provider-error copy. The contract under test:

- a provider-error-shaped STATUS line is dropped at the gateway status filter
  (the failed turn's final response is the canonical single failure message);
- opt-in live ``retry_progress`` events still pass through;
- the FINAL response still rewrites to the user-safe reply (that path must
  not be suppressed — it is the surviving copy);
- raw-text/programmatic surfaces (``local``) keep statuses unchanged.
"""

from __future__ import annotations

import pytest


def _prepare(platform, event_type, message):
    from gateway.run import _prepare_gateway_status_message
    return _prepare_gateway_status_message(platform, event_type, message)


_EXHAUSTION_STATUS = "❌ API failed after 4 retries — TimeoutError: codex stream produced no parsed stream event within 120s"


def test_provider_error_status_is_dropped_for_chat_platforms():
    assert _prepare("telegram", "lifecycle", _EXHAUSTION_STATUS) is None
    assert _prepare("discord", "status", _EXHAUSTION_STATUS) is None


def test_live_retry_progress_events_still_pass_through():
    out = _prepare("telegram", "retry_progress", "⚠️ API call failed (attempt 2/4) error_type=TimeoutError — retrying in 8s")
    assert out is not None, "the opt-in retry_progress lane must keep flowing during the stall"


def test_final_response_still_rewrites_to_the_provider_error_reply():
    from gateway.run import _sanitize_gateway_final_response
    final = _sanitize_gateway_final_response(
        "telegram", "API call failed after 4 retries: TimeoutError: connection closed")
    assert "kept failing" in final
    assert "/retry" in final, "the canonical failure message must tell the user what to do"


def test_raw_text_platforms_keep_the_status_unchanged():
    out = _prepare("local", "lifecycle", _EXHAUSTION_STATUS)
    assert out == _EXHAUSTION_STATUS


def test_normal_status_lines_still_flow():
    assert _prepare("telegram", "status", "🧠 Remembering this conversation…") == "🧠 Remembering this conversation…"
