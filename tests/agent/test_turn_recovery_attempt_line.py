"""A non-retryable API failure names itself instead of promising a second attempt.

A 401 on a static-key route (no credential to refresh, no pool entry to rotate to)
goes straight to the fallback chain, so the log used to read ``API call failed
(attempt 1/3)`` while ``attempt 2/3`` never appeared — which reads as a retry
counter that failed to advance (#73237). The classifier's verdict now rides the
same line on both surfaces (logger + buffered status trace)."""

import logging
from unittest.mock import MagicMock

from agent.turn_recovery import log_api_error_attempt


def _agent():
    agent = MagicMock()
    agent._summarize_api_error.return_value = "HTTP 401 invalid key"
    agent._client_log_context.return_value = "provider=custom"
    agent._is_openrouter_url.return_value = False
    agent.verbose_logging = False
    agent.provider, agent.base_url, agent.model = "custom", "https://x.test/v1", "m"
    return agent


def _call(agent, retryable):
    return log_api_error_attempt(
        agent, RuntimeError("401"), retry_count=1, max_retries=3, status_code=401,
        elapsed_time=0.1, api_messages=[], approx_tokens=10, retryable=retryable,
    )


def test_non_retryable_failure_is_named_on_log_and_status_line(caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _call(agent, retryable=False)
    assert "attempt 1/3, not retryable" in caplog.text
    assert "not retryable" in agent._buffer_vprint.call_args_list[0].args[0]


def test_retryable_failure_keeps_the_plain_attempt_counter(caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _call(agent, retryable=True)
    assert "(attempt 1/3)" in caplog.text
    assert "not retryable" not in caplog.text
