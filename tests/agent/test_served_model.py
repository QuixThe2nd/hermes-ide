"""agent.served_model — per-response served-model capture behind routing proxies (#54864)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from agent.served_model import (
    capture_served_model_from_stream,
    is_displayable_served_model,
    install_served_model_capture,
    result_model_fields,
    served_model_from_headers,
    store_served_model,
)


class _Agent:
    model = "hermes-router"
    _fallback_activated = False
    _primary_runtime: dict = {}


def test_httpx_hook_captures_litellm_header_and_preserves_when_absent():
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

    served["value"] = ""  # no routing header: the previously captured displayable value survives
    client.chat.completions.create(model="hermes-router", messages=[{"role": "user", "content": "hi"}])
    assert result_model_fields(agent) == {
        "requested_model": "hermes-router",
        "served_model": "gpt-4o-2024-11-20",
    }

    served["value"] = "HERMES-ROUTER"  # an alias echo must not replace the real deployment
    client.chat.completions.create(model="hermes-router", messages=[{"role": "user", "content": "hi"}])
    assert agent.last_served_model == "gpt-4o-2024-11-20"

    # Hermes' own fallback route surfaces when no proxy value has been captured.
    agent.last_served_model = None
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


@pytest.mark.parametrize(("requested", "candidate"), [
    ("hermes", "hermes"),
    ("router/Hermes", "HERMES"),
    ("hermes", "router/hermes"),
])
def test_store_served_model_skips_requested_alias(requested, candidate):
    agent = _Agent()
    agent.model = requested
    agent.last_served_model = "glm-5.3-flash"

    store_served_model(agent, candidate)

    assert agent.last_served_model == "glm-5.3-flash"


def test_store_served_model_leaves_alias_only_capture_unset():
    agent = _Agent()
    agent.model = "router/Hermes"

    store_served_model(agent, "HERMES")

    assert not hasattr(agent, "last_served_model")
    assert result_model_fields(agent) == {"requested_model": "router/Hermes", "served_model": None}

    store_served_model(agent, " zai/glm-5.3-flash ")
    assert agent.last_served_model == "zai/glm-5.3-flash"

    store_served_model(agent, "hermes")
    assert agent.last_served_model == "zai/glm-5.3-flash"


@pytest.mark.parametrize("value", [
    None,
    "",
    "3132475889042",
    "https://example.com/model",
])
def test_store_served_model_skips_undisplayable_values(value):
    agent = _Agent()
    agent.last_served_model = "glm-5.3-flash"

    store_served_model(agent, value)

    assert agent.last_served_model == "glm-5.3-flash"


def test_stream_capture_does_not_replace_real_model_with_requested_alias():
    agent = _Agent()
    agent.model = "hermes"
    agent.last_served_model = "glm-5.3-flash"
    stream = SimpleNamespace(response=SimpleNamespace(headers={"x-litellm-model-name": "hermes"}))

    capture_served_model_from_stream(agent, stream)

    assert agent.last_served_model == "glm-5.3-flash"


@pytest.mark.parametrize("headers", [
    {"x-litellm-model-id": "31324f758efb073636feda516865fd641caf2eb384a3e86bd0fe7e5d80aca6c4"},
    {"x-litellm-model-api-base": "http://192.168.30.20:8792/p/xai"},
    {"x-litellm-model-id": "3132475889042"},
], ids=["hex-id", "api-base-url", "numeric-id"])
def test_undisplayable_only_headers_do_not_capture_or_overwrite(headers):
    assert served_model_from_headers(headers) is None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "hermes-router",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }, headers=headers)

    client = openai.OpenAI(api_key="x", base_url="http://proxy.test/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    agent = _Agent()
    agent.last_served_model = "xai/grok-4.6"
    install_served_model_capture(agent, client)

    client.chat.completions.create(model="hermes-router", messages=[{"role": "user", "content": "hi"}])
    assert agent.last_served_model == "xai/grok-4.6"
