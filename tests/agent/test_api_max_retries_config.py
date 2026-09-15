"""Tests for agent.api_max_retries config surface.

Closes #11616 — make the hardcoded ``max_retries = 3`` in the agent's API
retry loop user-configurable so fallback-provider setups can fail over
faster on flaky primaries instead of burning ~3x180s on the same stall.
"""
from unittest.mock import patch

from run_agent import AIAgent


def _make_agent(api_max_retries=None, retry_base_delay=None, retry_max_delay=None):
    """Build an AIAgent with a mocked config.load_config that returns a
    config tree containing the given agent retry settings (or defaults)."""
    cfg = {"agent": {}}
    if api_max_retries is not None:
        cfg["agent"]["api_max_retries"] = api_max_retries
    if retry_base_delay is not None:
        cfg["agent"]["retry_base_delay"] = retry_base_delay
    if retry_max_delay is not None:
        cfg["agent"]["retry_max_delay"] = retry_max_delay

    with patch("agent.process_bootstrap.OpenAI"), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_default_api_max_retries_is_three():
    """No config override → legacy default of 3 retries preserved."""
    agent = _make_agent()
    assert agent._api_max_retries == 3


def test_default_api_retry_backoff_delays_are_legacy_values():
    """No config override → legacy retry backoff timings are preserved."""
    agent = _make_agent()
    assert agent._retry_base_delay == 2.0
    assert agent._retry_max_delay == 60.0


def test_api_retry_backoff_delays_honor_config_override():
    """Setting retry delay config propagates to the agent."""
    agent = _make_agent(retry_base_delay=5, retry_max_delay=300)
    assert agent._retry_base_delay == 5.0
    assert agent._retry_max_delay == 300.0


def test_api_retry_backoff_delays_fall_back_on_invalid_value():
    """Garbage retry delay values don't crash agent init."""
    agent = _make_agent(retry_base_delay="not-a-number", retry_max_delay="bad")
    assert agent._retry_base_delay == 2.0
    assert agent._retry_max_delay == 60.0


def test_api_max_retries_honors_config_override():
    """Setting agent.api_max_retries in config propagates to the agent."""
    agent = _make_agent(api_max_retries=1)
    assert agent._api_max_retries == 1

    agent2 = _make_agent(api_max_retries=5)
    assert agent2._api_max_retries == 5




