"""Rate-limit / credits header capture and low-credit notices for ``AIAgent``.

Parses provider response headers into ``_rate_limit_state`` / ``_credits_state`` and emits sticky notices.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""
from typing import Any


def _response_headers(http_response: Any):
    """Headers of a response, or None when there is nothing to parse."""
    return getattr(http_response, "headers", None) if http_response is not None else None


class RateLimitCreditsMixin:
    """Rate-limit + credits header capture and notices (see module docstring)."""


    def _capture_nous_model_switch(self, http_response: Any) -> None:
        """Record the Nous gateway's ``x-nous-model-switch`` header (a named account asked for the
        free tier's model; the gateway served its backing model and named it). Applied between
        calls by ``hermes_cli.anon_auth.apply_model_switch``. Fail-open."""
        headers = _response_headers(http_response)
        if not headers:
            return
        try:
            from hermes_cli.anon_auth import note_model_switch
            note_model_switch(self, headers)
        except Exception:
            pass  # Never let header parsing break the agent loop
