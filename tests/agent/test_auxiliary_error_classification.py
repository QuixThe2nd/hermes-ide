"""Auxiliary error classification shares the main classifier's billing table and labels quarantines truthfully.

Invariants for #107166 (OpenRouter org "Budget limit exceeded" 403 must be a payment error for the aux
ladder, like the main loop), #64144 (an unhealthy mark for absent credentials must not claim a payment
error), and #108349 (a vision fallback never lands on a text-only main model; an upstream-capacity 429
never benches the credential pool).
"""
import logging

import pytest

from agent import auxiliary_client as ac
from agent.error_classifier import FailoverReason, classify_api_error


class _Err(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


@pytest.mark.parametrize("body", [
    "Error code: 403 - {'error': {'message': 'Budget limit exceeded (monthly limit). Contact your org admin.', 'code': 403}}",
    "Error code: 403 - {'error': {'message': 'Key limit exceeded (total limit). Contact your org admin.', 'code': 403}}",
])
def test_openrouter_limit_403_is_billing_for_main_loop_and_aux_ladder(body):
    exc = _Err(body, 403)
    assert classify_api_error(exc).reason is FailoverReason.billing
    assert ac._is_payment_error(exc)
    # Every main-classifier billing phrase is an aux payment phrase (one table, no drift).
    assert all(p in ac._PAYMENT_KEYWORDS for p in classify_api_error.__globals__["_BILLING_PATTERNS"])


def test_absent_credentials_quarantine_is_debug_and_names_the_real_reason(caplog):
    ac._reset_aux_unhealthy_cache()
    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        ac._mark_provider_unhealthy("openrouter", ttl=60, reason="OPENROUTER_API_KEY not set", level=logging.DEBUG)
        ac._log_skip_unhealthy("openrouter", "title_generation")
        ac._mark_provider_unhealthy("nous")  # confirmed 402 path keeps the payment wording at WARNING
    marks = [r for r in caplog.records if "marking" in r.getMessage()]
    assert [(r.levelno, "payment" in r.getMessage()) for r in marks] == [(logging.DEBUG, False), (logging.WARNING, True)]
    assert "OPENROUTER_API_KEY not set" in marks[0].getMessage()
    skip = next(r for r in caplog.records if "skipping" in r.getMessage())
    assert "OPENROUTER_API_KEY not set" in skip.getMessage() and "payment" not in skip.getMessage()
    ac._reset_aux_unhealthy_cache()


def test_vision_fallback_skips_text_only_main_model(monkeypatch):
    monkeypatch.setattr(ac, "_read_main_provider", lambda: "zai")
    monkeypatch.setattr(ac, "_read_main_model", lambda: "glm-5.3")
    monkeypatch.setattr(ac, "_main_model_supports_vision", lambda provider, model: False)
    monkeypatch.setattr(ac, "resolve_provider_client", lambda **kw: pytest.fail("must not build a client for a text-only model"))
    assert ac._try_main_agent_model_fallback("nous", "vision", reason="rate limit") == (None, None, "")


def test_upstream_capacity_429_is_not_a_credential_to_bench():
    exc = _Err("Error code: 429 - {'status': 429, 'message': \"The requested model is temporarily at capacity upstream. "
               "This is not your API key's rate limit — please retry shortly.\"}", 429)
    assert ac._is_overloaded_error(exc)
    assert classify_api_error(exc).reason is FailoverReason.overloaded
