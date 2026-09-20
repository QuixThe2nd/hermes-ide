"""A persisted Codex session must not loop on 401 ``token_expired`` when the credential is fine.

Issue #88510: the Codex backend rejects a stale replayed ``encrypted_content`` blob with the
auth signature (401 ``token_expired``), so a resumed session failed on every prompt while a
fresh session on the same bearer worked. After the one-shot OAuth refresh changed nothing,
``recover_after_classification`` must strip cached ``codex_reasoning_items`` and retry once,
exactly like the 400 ``invalid_encrypted_content`` path; a 401 without cached reasoning is a
real expiry and stays on the credential path.
"""
from __future__ import annotations

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState

_TOKEN_EXPIRED = "Provided authentication token is expired. Please try signing in again."


class _Codex401(Exception):
    def __init__(self, code: str | None = "token_expired"):
        super().__init__(f"HTTP 401: {_TOKEN_EXPIRED}")
        self.status_code = 401
        self.message = _TOKEN_EXPIRED
        err = {"message": _TOKEN_EXPIRED, "type": "invalid_request_error"}
        if code:
            err["code"] = code
        self.body = {"error": err}


class _Agent:
    """Codex OAuth agent whose refresh path yields the same token (nothing to adopt)."""

    log_prefix = ""
    provider = "openai-codex"
    api_mode = "codex_responses"
    base_url = "https://chatgpt.com/backend-api/codex"
    model = "gpt-5.3-codex"
    api_key = "same-bearer"
    _codex_reasoning_replay_enabled = True

    def __init__(self):
        self.refresh_calls = 0

    def _recover_with_credential_pool(self, **kwargs):
        return False, False

    def _try_refresh_codex_client_credentials(self, *, force=True):
        self.refresh_calls += 1
        return False

    def _extract_api_error_context(self, error):
        from agent.agent_runtime_helpers import extract_api_error_context
        return extract_api_error_context(error)

    def _disable_codex_reasoning_replay(self, messages=None):
        from run_agent import AIAgent
        return AIAgent._disable_codex_reasoning_replay(self, messages)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _cached_history(n: int = 2):
    return [
        {"role": "assistant", "content": f"answer {i}",
         "codex_reasoning_items": [{"type": "reasoning", "id": f"rs_{i}", "encrypted_content": "gAAA" * 8}]}
        for i in range(n)
    ]


def _recover(agent, err, retry, messages):
    classified = classify_api_error(err, provider=agent.provider, model=agent.model)
    assert classified.reason == FailoverReason.auth  # never reclassified
    return recover_after_classification(
        agent, err, classified, retry, status_code=401,
        error_context=agent._extract_api_error_context(err), messages=messages, api_messages=list(messages),
    )


def test_lingering_token_expired_strips_cached_reasoning_once_after_refresh():
    agent, retry, messages = _Agent(), TurnRetryState(), _cached_history()

    retried, _ = _recover(agent, _Codex401(), retry, messages)

    assert retried is True
    assert agent.refresh_calls == 1  # the credential path ran first and changed nothing
    assert agent._codex_reasoning_replay_enabled is False
    assert not any("codex_reasoning_items" in m for m in messages)
    # A second identical 401 in the same turn is a real auth failure: no second strip.
    assert _recover(agent, _Codex401(), retry, messages) == (False, False)


@pytest.mark.parametrize("err, history", [
    (_Codex401(), []),                       # real expiry: nothing cached to strip
    (_Codex401(code=None), _cached_history()),  # generic 401: not the token_expired signature
])
def test_token_expired_without_cached_reasoning_stays_on_auth_path(err, history):
    agent, retry = _Agent(), TurnRetryState()

    assert _recover(agent, err, retry, history) == (False, False)
    assert agent.refresh_calls == 1
    assert agent._codex_reasoning_replay_enabled is True
    assert retry.invalid_encrypted_content_retry_attempted is False
