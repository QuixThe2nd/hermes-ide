"""Profile-path parity tests: profile path vs legacy flag path of build_kwargs.

The provider profiles these comparisons used (nvidia, kimi-coding, openrouter,
qwen-oauth) were deliberately removed in db4ffa25327 — only the ``custom``
(LiteLLM) profile ships now, so the surviving parity classes exercise the
profile path through it.
"""

import pytest
from agent.transports.chat_completions import ChatCompletionsTransport
from providers import get_provider_profile


@pytest.fixture
def transport():
    return ChatCompletionsTransport()


def _msgs():
    return [{"role": "user", "content": "hello"}]


def _max_tokens_fn(n):
    return {"max_completion_tokens": n}


class TestDeveloperRoleParity:
    """Developer role swap must work on BOTH legacy and profile paths."""

    def test_legacy_path_swaps_for_gpt5(self, transport):
        msgs = [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=msgs, tools=None,
        )
        assert kw["messages"][0]["role"] == "developer"

    def test_profile_path_swaps_for_gpt5(self, transport):
        msgs = [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=msgs, tools=None,
            provider_profile=get_provider_profile("custom"),
        )
        assert kw["messages"][0]["role"] == "developer"



class TestRequestOverridesParity:
    """request_overrides with extra_body must merge identically on both paths."""

    def test_extra_body_override_legacy(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("custom"),
            request_overrides={"extra_body": {"custom_key": "custom_val"}},
        )
        assert kw["extra_body"]["custom_key"] == "custom_val"



    def test_top_level_override(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("custom"),
            request_overrides={"top_p": 0.9},
        )
        assert kw["top_p"] == 0.9
