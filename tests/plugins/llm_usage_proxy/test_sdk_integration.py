"""Real OpenAI / Anthropic SDK clients through the usage-route seams."""

from __future__ import annotations

import json
import socket

import pytest

from agent.anthropic_adapter import build_anthropic_client
from agent.process_bootstrap import build_keepalive_http_client
from hermes_cli.llm_usage_routes import (
    _reset_registry,
    activate_routing,
    register_route_table,
    routing_state,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from conftest import respond_json, wait_for_row_count

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


# ── plugin register()/load-path bootstrap (the CLI never starts a gateway) ──


class _RecordingCtx:
    """Minimal PluginContext stand-in: records what register() wires up."""

    def __init__(self):
        self.cli_commands: list[str] = []
        self.hooks: dict[str, list] = {}

    def register_cli_command(
        self, *, name, help, setup_fn, handler_fn=None, description=""
    ):
        self.cli_commands.append(name)

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)


@pytest.fixture
def fake_dns(monkeypatch):
    """Map fake provider hosts onto loopback so route tables built from the
    profile config (which reject loopback bases) can point at the recording
    fake upstream. Real clients, HTTP, and SQLite still run."""
    real_getaddrinfo = socket.getaddrinfo
    mapping: dict[str, str] = {}

    def _resolve(host, port, *args, **kwargs):
        return real_getaddrinfo(mapping.get(host, host), port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    return mapping


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _install_fake_systemctl(monkeypatch) -> list[list[str]]:
    """Stand in for systemctl; records every call. reconcile resolves the
    runner as a module global at call time, so patching the attribute on the
    plugin's systemd module covers the whole lifecycle."""
    calls: list[list[str]] = []

    def _run(argv):
        argv = [str(part) for part in argv]
        calls.append(argv)
        if "is-enabled" in argv:
            return (0, "enabled\n", "")
        if "is-active" in argv:
            return (0, "active\n", "")
        return (0, "", "")

    monkeypatch.setattr(
        "plugins.llm_usage_proxy.systemd.default_systemctl_runner", _run
    )
    return calls


def test_plugin_register_activates_routing_and_sdk_transport(
    start_upstream, start_proxy, tmp_path, monkeypatch, fake_dns
):
    """Causal chain: plugin register() -> lifecycle reconcile -> verified
    route activation -> real SDK client request flows through the proxy and
    lands in its SQLite ledger. No manual register_route_table/activate."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    upstream = start_upstream(respond_json(_openai_completion_payload()))
    fake_dns["fake-zai.test"] = "127.0.0.1"
    logical_base = f"http://fake-zai.test:{upstream.server_address[1]}/v4"
    proxy_port = _free_port()
    (home / "config.yaml").write_text(
        "llm_usage_proxy:\n"
        "  enabled: true\n"
        f"  port: {proxy_port}\n"
        "  upstreams:\n"
        f'    testroute: "{logical_base}"\n'
    )

    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.systemd import profile_identity

    cfg = load_llm_usage_proxy_config()
    table = build_route_table(cfg)
    assert table["testroute"] == logical_base

    # The already-running, verified sidecar reconcile must adopt/stand down
    # for: identity + exactly this route table, on the configured port.
    proxy = start_proxy(
        table,
        identity=profile_identity(home),
        port=proxy_port,
    )
    assert proxy.server_address[1] == proxy_port

    systemctl_calls = _install_fake_systemctl(monkeypatch)

    token = set_hermes_home_override(str(home.resolve()))
    http_client = None
    try:
        from plugins.llm_usage_proxy import register

        ctx = _RecordingCtx()
        register(ctx)

        # CLI registration and the gateway hook are preserved.
        assert ctx.cli_commands == ["llm_usage_proxy"]
        assert "on_gateway_start" in ctx.hooks

        state = routing_state()
        assert state["active"] is True
        assert state["proxy_origin"] == f"http://127.0.0.1:{proxy_port}"
        assert state["routes"]["testroute"] == logical_base

        from openai import OpenAI

        http_client = build_keepalive_http_client(logical_base)
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

    # The request reached the real upstream only via the proxy...
    assert len(upstream.requests) == 1
    sent = upstream.requests[0]
    assert sent["headers"]["Authorization"] == f"Bearer {OPENAI_KEY}"
    assert sent["path"].startswith("/v4/chat/completions")

    # ...and the proxy ledgered it with provider-reported usage.
    rows = wait_for_row_count(tmp_path / "usage.sqlite", 1)
    assert len(rows) == 1
    assert rows[0]["upstream"] == "testroute"
    assert rows[0]["prompt_tokens"] == 3
    assert rows[0]["completion_tokens"] == 1

    # Adopting an already-running verified unit must not restart anything.
    assert not any("restart" in call for call in systemctl_calls)


def test_plugin_register_disabled_profile_makes_no_lifecycle_calls(
    tmp_path, monkeypatch
):
    """A disabled profile's register() wires CLI + gateway hook only: no
    reconcile, no systemctl, no routing state changes — traffic stays direct."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("llm_usage_proxy:\n  enabled: false\n")

    systemctl_calls = _install_fake_systemctl(monkeypatch)
    reconcile_calls: list[dict] = []
    monkeypatch.setattr(
        "plugins.llm_usage_proxy.lifecycle.reconcile_proxy_on_load",
        lambda **kwargs: reconcile_calls.append(kwargs),
    )

    token = set_hermes_home_override(str(home.resolve()))
    try:
        from plugins.llm_usage_proxy import register

        ctx = _RecordingCtx()
        register(ctx)
    finally:
        reset_hermes_home_override(token)

    assert ctx.cli_commands == ["llm_usage_proxy"]
    assert "on_gateway_start" in ctx.hooks
    assert reconcile_calls == []
    assert systemctl_calls == []
    state = routing_state()
    assert state["active"] is False
    assert state["routes"] == {}
