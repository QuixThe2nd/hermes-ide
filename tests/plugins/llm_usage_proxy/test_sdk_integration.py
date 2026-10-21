"""Real OpenAI / Anthropic SDK clients through the usage-route seams."""

from __future__ import annotations

import json

import pytest

from agent.anthropic_adapter import build_anthropic_client
from agent.process_bootstrap import build_keepalive_http_client
from hermes_cli.llm_usage_routes import (
    _reset_registry,
    activate_routing,
    register_route_table,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from conftest import respond_json

OPENAI_KEY = "sk-openai-integration-test-key"
ANTHROPIC_KEY = "sk-ant-api03-integration-test-key"


@pytest.fixture(autouse=True)
def _clean_route_registry():
    _reset_registry()
    yield
    _reset_registry()


@pytest.fixture
def profile_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    return home


def _activate_routing(home, *, proxy_port: int, route_name: str, logical_base: str) -> str:
    key = str(home.resolve())
    proxy_origin = f"http://127.0.0.1:{proxy_port}"
    errors = register_route_table(
        proxy_origin,
        {route_name: logical_base},
        profile=key,
    )
    assert errors == []
    activate_routing(profile=key)
    return key


def _openai_completion_payload(model: str = "glm-5") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _anthropic_message_payload(model: str = "claude-sonnet-5") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def test_openai_sdk_routes_through_proxy_and_preserves_logical_request(
    start_upstream, start_proxy, profile_home
):
    direct = start_upstream(respond_json(_openai_completion_payload()))
    proxy_upstream = start_upstream(respond_json(_openai_completion_payload()))
    proxy = start_proxy(
        {"openai-test": f"http://127.0.0.1:{proxy_upstream.server_address[1]}/v4"}
    )
    logical_base = f"http://127.0.0.1:{direct.server_address[1]}/v1"

    token = set_hermes_home_override(str(profile_home.resolve()))
    http_client = None
    try:
        _activate_routing(
            profile_home,
            proxy_port=proxy.server_address[1],
            route_name="openai-test",
            logical_base=logical_base,
        )

        from openai import OpenAI

        http_client = build_keepalive_http_client(logical_base)
        probe = http_client.post(
            f"{logical_base}/chat/completions",
            json={"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        )
        assert str(probe.request.url).startswith(logical_base)

        client = OpenAI(
            api_key=OPENAI_KEY,
            base_url=logical_base,
            http_client=http_client,
        )
        response = client.chat.completions.create(
            model="glm-5",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response.choices[0].message.content == "ok"
        assert str(client.base_url).rstrip("/") == logical_base.rstrip("/")
    finally:
        reset_hermes_home_override(token)
        if http_client is not None:
            http_client.close()

    assert len(direct.requests) == 0
    assert len(proxy_upstream.requests) == 2
    sent = proxy_upstream.requests[-1]
    assert sent["headers"]["Authorization"] == f"Bearer {OPENAI_KEY}"
    assert sent["path"].startswith("/v4/chat/completions")


def test_anthropic_sdk_routes_through_proxy_and_preserves_logical_request(
    start_upstream, start_proxy, profile_home
):
    direct = start_upstream(respond_json(_anthropic_message_payload()))
    proxy_upstream = start_upstream(respond_json(_anthropic_message_payload()))
    proxy = start_proxy(
        {
            "anthropic-test": f"http://127.0.0.1:{proxy_upstream.server_address[1]}",
        }
    )
    logical_with_v1 = f"http://127.0.0.1:{direct.server_address[1]}/v1"
    route_base = logical_with_v1.removesuffix("/v1")

    token = set_hermes_home_override(str(profile_home.resolve()))
    client = None
    try:
        _activate_routing(
            profile_home,
            proxy_port=proxy.server_address[1],
            route_name="anthropic-test",
            logical_base=route_base,
        )

        client = build_anthropic_client(ANTHROPIC_KEY, base_url=logical_with_v1)
        raw = client.with_raw_response.messages.create(
            model="claude-sonnet-5",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )
        response = raw.parse()
        assert response.content[0].text == "ok"
        assert str(client.base_url).rstrip("/") == route_base.rstrip("/")
        assert str(raw.http_response.request.url).startswith(route_base)
    finally:
        reset_hermes_home_override(token)
        if client is not None:
            client.close()

    assert len(direct.requests) == 0
    assert len(proxy_upstream.requests) == 1
    sent = proxy_upstream.requests[0]
    assert sent["headers"]["X-Api-Key"] == ANTHROPIC_KEY
    assert sent["path"] == "/v1/messages"
    body = json.loads(sent["body"])
    assert body["model"] == "claude-sonnet-5"
