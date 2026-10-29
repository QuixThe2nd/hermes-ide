"""agent.served_model — per-response served-model capture behind routing proxies (#54864)."""

from __future__ import annotations

import httpx
import openai

from agent.served_model import (
    is_displayable_served_model,
    install_served_model_capture,
    result_model_fields,
    served_model_from_headers,
)


class _Agent:
    model = "hermes-router"
    _fallback_activated = False
    _primary_runtime: dict = {}


def test_httpx_hook_captures_litellm_header_and_clears_when_absent():
    served = {"value": "gpt-4o-2024-11-20"}

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-litellm-model-id": served["value"]} if served["value"] else {}
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "hermes-router",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }, headers=headers)

    client = openai.OpenAI(api_key="x", base_url="http://proxy.test/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    agent = _Agent()
    install_served_model_capture(agent, client)
    install_served_model_capture(agent, client)  # idempotent: one hook per client
    assert len(client._client.event_hooks["response"]) == 1

    client.chat.completions.create(model="hermes-router", messages=[{"role": "user", "content": "hi"}])
    assert result_model_fields(agent) == {"requested_model": "hermes-router", "served_model": "gpt-4o-2024-11-20"}

    served["value"] = ""  # next response has no routing header: the stale value must not survive
    client.chat.completions.create(model="hermes-router", messages=[{"role": "user", "content": "hi"}])
    assert result_model_fields(agent) == {"requested_model": "hermes-router", "served_model": None}

    # Hermes' own fallback route surfaces the same way when no proxy header is present.
    agent._fallback_activated = True
    agent._primary_runtime = {"model": "gpt-5.6-sol"}
    agent.model = "qwen/qwen3.8-max"
    assert result_model_fields(agent) == {"requested_model": "gpt-5.6-sol", "served_model": "qwen/qwen3.8-max"}


def test_served_model_from_headers_model_name_wins_over_model_id():
    assert served_model_from_headers({
        "x-litellm-model-name": "xai/grok-4.6",
        "x-litellm-model-id": "gpt-4o-2024-11-20",
    }) == "xai/grok-4.6"


def test_served_model_from_headers_skips_undisplayable_name_for_displayable_id():
    assert served_model_from_headers({
        "x-litellm-model-name": "3132475889042",
        "x-litellm-model-id": "gpt-4o-2024-11-20",
    }) == "gpt-4o-2024-11-20"


def test_is_displayable_served_model():
    assert is_displayable_served_model("3132475889042") is False
    assert is_displayable_served_model("http://192.168.30.20:8792/p/xai") is False
    assert is_displayable_served_model("https://example.com/model") is False
    assert is_displayable_served_model("xai/grok-4.6") is True
    assert is_displayable_served_model("gpt-4o-2024-11-20") is True
    assert is_displayable_served_model("31324f758efb073636feda516865fd641caf2eb384a3e86bd0fe7e5d80aca6c4") is False


def test_served_model_from_headers_raw_fallback_when_all_undisplayable():
    assert served_model_from_headers({
        "x-litellm-model-id": "3132475889042",
        "x-litellm-model-api-base": "http://192.168.30.20:8792/p/xai",
    }) == "3132475889042"
