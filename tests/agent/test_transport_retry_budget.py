"""Time-bounded extended retry budget for transport-class API failures (2026-11-12 incident).

WAN latency spikes made the upstream unreachable for ~25 minutes with intermittent
recovery; each turn burned ``agent.api_max_retries`` (4 attempts, ~8 min) and died.
Contracts under test:

- transport/transient verdicts (timeout / server_error / overloaded, retryable) earn
  extra attempts inside a configurable time window with capped exponential backoff
  plus jitter, clamped to the remaining window;
- non-transient verdicts (401/403 auth, 400 format, context-length, rate limits)
  keep the current fast-fail behaviour;
- once the window closes the turn ends exactly as before (max-retries exhaustion).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


# ── Classification gate ────────────────────────────────────────────────────

class _Http(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _classified(status, message):
    from agent.error_classifier import classify_api_error
    return classify_api_error(_Http(status, message), provider="openrouter", model="m")


@pytest.mark.parametrize(("status", "message", "qualifies"), [
    (502, "Bad Gateway", True),                     # upstream 502 → server_error
    (503, "service unavailable", True),             # overloaded
    (504, "gateway timeout", True),                 # timeout class
    (None, "", True),                               # raw TimeoutError below
    (401, "invalid api key", False),                # auth fast-fail
    (403, "forbidden", False),                      # auth fast-fail
    (400, "unknown parameter: foo", False),         # format error fast-fail
    (429, "rate limit exceeded", False),            # rate limits keep their own policy
])
def test_only_transport_class_verdicts_qualify(status, message, qualifies):
    from agent.transport_retry import is_transport_retry_classified

    error = TimeoutError("request timed out") if status is None else _Http(status, message)
    if status is None:
        from agent.error_classifier import classify_api_error
        classified = classify_api_error(error, provider="openrouter", model="m")
    else:
        classified = _classified(status, message)
    assert is_transport_retry_classified(classified) is qualifies


def test_context_length_overflow_never_qualifies():
    from agent.transport_retry import is_transport_retry_classified

    classified = _classified(400, "context length exceeded: reduce the length")
    assert classified.reason.value == "context_overflow"
    assert not is_transport_retry_classified(classified)


# ── Budget / backoff scheduling ────────────────────────────────────────────

def _retry_state():
    from agent.turn_retry_state import TurnRetryState
    return TurnRetryState()


def _classified_stub(reason="timeout", retryable=True):
    from agent.error_classifier import FailoverReason
    return types.SimpleNamespace(reason=getattr(FailoverReason, reason), retryable=retryable)


class _WindowAgent:
    _transport_retry_window_seconds = 100.0


@pytest.mark.real_retry_backoff
def test_waits_use_capped_exponential_backoff_within_the_window(monkeypatch):
    from agent import transport_retry
    from agent.retry_utils import jittered_backoff as _real_backoff

    # The tests/agent conftest autouse-fixtures jittered_backoff to 0.0; use a
    # deterministic no-jitter schedule to assert the exponential shape.
    monkeypatch.setattr(transport_retry, "jittered_backoff",
                        lambda attempt, **kw: _real_backoff(attempt, base_delay=kw["base_delay"],
                                                             max_delay=kw["max_delay"], jitter_ratio=0.0))
    monkeypatch.setattr(transport_retry.time, "monotonic", lambda: 1000.0)
    agent, retry = _WindowAgent(), _retry_state()
    waits = []
    for _ in range(4):
        wait = transport_retry.next_transport_retry(agent, retry, _classified_stub())
        assert wait is not None
        waits.append(wait)
        # advance the clock by the scheduled wait
        now = transport_retry.time.monotonic() + wait
        monkeypatch.setattr(transport_retry.time, "monotonic", lambda: now)
    assert waits == sorted(waits), "backoff must be non-decreasing"
    assert waits[0] < waits[-1], "backoff must grow"
    assert waits[-1] <= 120.0 + 1.0, "single waits stay under the cap (+jitter)"


def test_wait_is_clamped_to_the_remaining_window(monkeypatch):
    from agent import transport_retry

    monkeypatch.setattr(transport_retry.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(transport_retry, "jittered_backoff", lambda *a, **k: 10_000.0)
    agent, retry = _WindowAgent(), _retry_state()
    wait = transport_retry.next_transport_retry(agent, retry, _classified_stub())
    assert wait == pytest.approx(100.0), "a huge scheduled wait must clamp to the remaining window"


def test_window_expiry_and_disable_return_none(monkeypatch):
    from agent import transport_retry

    monkeypatch.setattr(transport_retry.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(transport_retry, "jittered_backoff", lambda *a, **k: 1.0)
    agent, retry = _WindowAgent(), _retry_state()
    assert transport_retry.next_transport_retry(agent, retry, _classified_stub("auth")) is None
    assert transport_retry.next_transport_retry(agent, retry, _classified_stub()) is not None
    # clock past deadline + window
    monkeypatch.setattr(transport_retry.time, "monotonic", lambda: 1000.0 + 100.0 + 0.1)
    assert transport_retry.next_transport_retry(agent, retry, _classified_stub()) is None
    # window <= 0 disables the budget entirely
    monkeypatch.setattr(transport_retry.time, "monotonic", lambda: 1000.0)
    disabled = types.SimpleNamespace(_transport_retry_window_seconds=0.0)
    assert transport_retry.next_transport_retry(disabled, _retry_state(), _classified_stub()) is None


# ── Config surface ─────────────────────────────────────────────────────────

def _make_agent(transport_retry_window_seconds=None):
    cfg = {"agent": {}}
    if transport_retry_window_seconds is not None:
        cfg["agent"]["transport_retry_window_seconds"] = transport_retry_window_seconds
    with patch("agent.process_bootstrap.OpenAI"), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        from run_agent import AIAgent
        return AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )


def test_default_transport_retry_window_is_twenty_five_minutes():
    assert _make_agent()._transport_retry_window_seconds == 1500.0


def test_transport_retry_window_honors_config_override_and_survives_garbage():
    assert _make_agent(60)._transport_retry_window_seconds == 60.0
    assert _make_agent("not-a-number")._transport_retry_window_seconds == 1500.0
    assert _make_agent(0)._transport_retry_window_seconds == 0.0


# ── Retry-loop decision (settle_unrecovered_error) ─────────────────────────

class _LoopAgent:
    log_prefix = ""
    verbose_logging = False
    provider = "openrouter"
    model = "m"
    base_url = "https://openrouter.ai/api/v1"
    _interrupt_requested = False
    _transport_retry_window_seconds = 100.0
    _transport_retry_deadline = None
    _transport_retry_attempts = 0

    def _try_recover_primary_transport(self, *a, **kw):
        return False

    def _has_pending_fallback(self):
        return False

    def _try_activate_fallback(self):
        return False

    def _summarize_api_error(self, error):
        return str(error)

    def _persist_session(self, *a, **kw):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _settle(agent, error, classified, retry, *, retry_count, max_retries):
    from agent.turn_api_error import settle_unrecovered_error
    return settle_unrecovered_error(
        agent, api_error=error, classified=classified, _retry=retry,
        status_code=getattr(error, "status_code", None), error_msg=str(error).lower(),
        is_context_length_error=False, is_rate_limited=False, _is_zai_coding_overload=False,
        _provider="openrouter", _base="https://openrouter.ai/api/v1", _model="m",
        messages=[], api_messages=[], api_kwargs=None, active_system_prompt=None,
        conversation_history=None, approx_tokens=10, retry_count=retry_count,
        max_retries=max_retries, compression_attempts=0, api_call_count=1,
    )


def _timeout_error():
    from agent.error_classifier import classify_api_error
    error = TimeoutError("codex stream produced no parsed stream event within 120s")
    return error, classify_api_error(error, provider="openrouter", model="m")


@pytest.mark.real_retry_backoff
def test_transport_error_keeps_retrying_within_the_window(monkeypatch):
    """Retries exhausted + transport-class error + open window → one more attempt,
    not a dead turn (before the fix this returned the terminal result immediately)."""
    from agent import turn_api_error
    from agent.turn_retry_state import TurnRetryState

    agent, retry = _LoopAgent(), TurnRetryState()
    error, classified = _timeout_error()
    sleeps: list = []
    # Deterministic first wait so the extended attempt observably sleeps
    # (the conftest's fast-backoff fixture is opted out below via marker).
    from agent import transport_retry
    monkeypatch.setattr(transport_retry, "jittered_backoff", lambda *a, **k: 5.0)

    def _capture_sleep(*a, **kw):
        sleeps.append(a[1] if len(a) > 1 else kw.get("wait_time"))

    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", _capture_sleep)
    verdict = _settle(agent, error, classified, retry, retry_count=4, max_retries=4)

    assert verdict.action == "continue", f"transport window must buy another attempt, got {verdict.action}"
    assert verdict.max_retries == 5, "the extended attempt must raise the loop ceiling by exactly one"
    assert sleeps and sleeps[0] > 0, "the extended attempt must wait with interruptible backoff"


def test_transport_error_gives_up_once_the_window_closes(monkeypatch):
    from agent import turn_api_error
    from agent.turn_retry_state import TurnRetryState
    import time as _time

    agent = _LoopAgent()
    agent._transport_retry_window_seconds = 0.001
    # Deadline already stamped in the past: the window is provably closed.
    agent._transport_retry_deadline = _time.monotonic() - 0.1
    retry = TurnRetryState()
    error, classified = _timeout_error()
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep",
                        lambda *a, **kw: None)
    verdict = _settle(agent, error, classified, retry, retry_count=4, max_retries=4)

    assert verdict.action == "return"
    assert verdict.result is not None and verdict.result.get("failed") is True


def test_auth_error_keeps_fast_fail(monkeypatch):
    """401 must still end the turn at retry exhaustion — no extended budget."""
    from agent import turn_api_error
    from agent.turn_retry_state import TurnRetryState

    agent, retry = _LoopAgent(), TurnRetryState()
    error = _Http(401, "invalid api key")
    from agent.error_classifier import classify_api_error
    classified = classify_api_error(error, provider="openrouter", model="m")
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", lambda *a, **kw: None)
    verdict = _settle(agent, error, classified, retry, retry_count=4, max_retries=4)

    assert verdict.action == "return", "non-transient errors must keep the fast-fail path"
    assert verdict.result is not None and verdict.result.get("failed") is True


def test_interrupt_during_extended_wait_cancels_the_turn(monkeypatch):
    """/stop during the extended wait must abort promptly, not retry."""
    from agent import turn_api_error
    from agent.turn_api_error import settle_unrecovered_error
    from agent.turn_retry_state import TurnRetryState

    agent, retry = _LoopAgent(), TurnRetryState()
    error, classified = _timeout_error()
    interrupted_result = {"final_response": "stopped", "interrupted": True, "messages": []}

    def _sleep(agent_, wait_time, _retry, **kw):
        return interrupted_result

    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", _sleep)
    verdict = _settle(agent, error, classified, retry, retry_count=4, max_retries=4)
    assert verdict.action == "return" and verdict.result is interrupted_result
