"""A rate-limit retry names the provider's reset window on the status line (#26889).

"Rate limited. Waiting 60s" hides the one fact that decides whether to wait or
switch models: a per-minute throttle and a 13-minute plan window look identical
without it. The hint is derived from the same ``reset_at`` the credential pool
uses, so the relative ``resets_in_seconds`` a Codex/ChatGPT usage-limit body
carries must land there too.
"""

import time
from unittest.mock import MagicMock

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.turn_recovery import compute_error_backoff, reset_hint


def _codex_429(**fields):
    err = Exception("HTTP 429: The usage limit has been reached")
    err.body = {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached", **fields}}
    return err


def test_relative_resets_in_seconds_becomes_reset_at_when_no_epoch_is_given():
    ctx = extract_api_error_context(_codex_429(resets_in_seconds=756))
    assert abs(ctx["reset_at"] - (time.time() + 756)) < 5
    # An explicit epoch wins over the relative field (same precedence as the credential pool).
    ctx = extract_api_error_context(_codex_429(resets_at=1_900_000_000, resets_in_seconds=756))
    assert ctx["reset_at"] == 1_900_000_000
    # No signal at all → no hint, not a crash.
    assert reset_hint(Exception("HTTP 429")) == "" and reset_hint(_codex_429(resets_in_seconds=-5)) == ""


def test_rate_limit_retry_status_names_the_reset_window():
    agent = MagicMock()
    from agent.status_output import StatusOutputMixin
    for name in ("_emit_diagnostic_wait", "_buffer_diagnostic_status"):
        setattr(agent, name, getattr(StatusOutputMixin, name).__get__(agent))
    agent._client_log_context.return_value = ""

    compute_error_backoff(
        agent, _codex_429(resets_in_seconds=756), retry_count=1, max_retries=3,
        is_rate_limited=True, is_zai_coding_overload=False,
        base_url="https://example.test/v1", model="test/model",
    )

    text = agent._buffer_status.call_args.args[0]
    assert text.startswith("⏱️ Rate limited. Resets in ~13m. Waiting ")
