"""Lazy routing bootstrap at the HTTP-client boundary.

``main()`` skips plugin discovery for the built-in subcommands, so a plain
``hermes chat`` never calls the plugin's ``register()`` — nothing registers a
route table or activates routing before it builds its provider clients. These
tests drive the chain that has to work anyway, from the profile's *config* and
its already-running sidecar to the first client built through the real
``build_keepalive_http_client`` seam.

No test here loads the plugin, calls ``register_route_table``, or activates
routing by hand (the one exception is the explicit-state test, which sets up a
pre-existing decision in order to prove the bootstrap defers to it). The
sidecar each test runs is the environment a systemd unit provides, not a stand
in for the condition under test: the in-process registration/activation is
always left to the code under test.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.process_bootstrap import build_keepalive_http_client
from hermes_cli.llm_usage_routes import (
    _reset_registry,
    base_url_routable,
    deactivate_routing,
    register_route_table,
    reroute_url,
    routing_state,
)
from plugins.llm_usage_proxy.probe import port_in_use

from conftest import respond_json, wait_for_row_count

OPENAI_KEY = "sk-openai-lazy-bootstrap-test-key"
ROUTE_NAME = "testroute"
LOGICAL_HOST = "fake-zai.test"


@pytest.fixture(autouse=True)
def _clean_route_registry():
    _reset_registry()
    yield
    _reset_registry()


@pytest.fixture
def fake_dns(monkeypatch):
    """Map the fake provider host onto loopback, so the profile config can name
    a non-loopback base (route tables reject loopback targets) that really
    resolves to the recording fake upstream."""
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
    """Record every systemctl invocation; never touches the real manager."""
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


def _count_reconciles(monkeypatch) -> list[int]:
    """Transparently count lifecycle entries — the real reconcile still runs."""
    from plugins.llm_usage_proxy import lifecycle

    real = lifecycle.reconcile_proxy_on_load
    calls: list[int] = []

    def _counting(**kwargs):
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "reconcile_proxy_on_load", _counting)
    return calls


def _write_enabled_config(hermes_home, *, proxy_port: int, logical_base: str) -> None:
    (hermes_home / "config.yaml").write_text(
        "llm_usage_proxy:\n"
        "  enabled: true\n"
        f"  port: {proxy_port}\n"
        "  upstreams:\n"
        f'    {ROUTE_NAME}: "{logical_base}"\n'
    )


def _openai_completion_payload() -> dict:
    return {
        "id": "chatcmpl-lazy-bootstrap",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "glm-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _start_foreign_listener(request) -> ThreadingHTTPServer:
    """A listener that answers ``{"ok": true}`` — not this profile's proxy."""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request.addfinalizer(server.server_close)
    request.addfinalizer(server.shutdown)
    return server


def test_enabled_profile_first_client_bootstraps_routing_and_meters(
    hermes_home, start_upstream, start_proxy, tmp_path, monkeypatch, fake_dns
):
    """Config-enabled profile, sidecar already serving, plugin never loaded:
    building the first provider client reconciles, activates verified routing
    and meters the request. No manual route registration anywhere."""
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    fake_dns[LOGICAL_HOST] = "127.0.0.1"
    logical_base = f"http://{LOGICAL_HOST}:{upstream.server_address[1]}/v4"
    proxy_port = _free_port()
    _write_enabled_config(hermes_home, proxy_port=proxy_port, logical_base=logical_base)

    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.systemd import profile_identity

    # The sidecar a systemd unit would already be serving: this profile's
    # identity and exactly the route table its config renders.
    proxy = start_proxy(
        build_route_table(load_llm_usage_proxy_config()),
        identity=profile_identity(hermes_home),
        port=proxy_port,
    )
    assert proxy.server_address[1] == proxy_port

    systemctl_calls = _install_fake_systemctl(monkeypatch)
    reconciles = _count_reconciles(monkeypatch)

    # Precondition: nothing has decided routing for this profile yet.
    before = routing_state()
    assert before["active"] is False
    assert before["routes"] == {}

    from openai import OpenAI

    http_client = build_keepalive_http_client(logical_base)
    try:
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
        # Provider identity is untouched: the client still speaks to the
        # logical base URL, and the routing decision happened below it.
        assert str(client.base_url).rstrip("/") == logical_base.rstrip("/")

        state = routing_state()
        assert state["active"] is True
        assert state["proxy_origin"] == f"http://127.0.0.1:{proxy_port}"
        assert state["routes"][ROUTE_NAME] == logical_base
    finally:
        http_client.close()

    # The request reached the real upstream only via the proxy...
    assert len(upstream.requests) == 1
    sent = upstream.requests[0]
    assert sent["path"].startswith("/v4/chat/completions")
    assert sent["headers"]["Authorization"] == f"Bearer {OPENAI_KEY}"

    # ...and the proxy ledgered it with the provider-reported usage.
    rows = wait_for_row_count(tmp_path / "usage.sqlite", 1)
    assert len(rows) == 1
    assert rows[0]["upstream"] == ROUTE_NAME
    assert rows[0]["prompt_tokens"] == 3
    assert rows[0]["completion_tokens"] == 1

    # The bootstrap — not the request, and not a second client — ran it, once.
    assert reconciles == [1]
    # Adopting an already-running verified unit restarts nothing.
    assert not any("restart" in call for call in systemctl_calls)


def test_disabled_profile_first_client_stays_direct_without_reconcile(
    hermes_home, start_upstream, monkeypatch
):
    """``enabled: false``: the first client build must not reconcile anything —
    no systemctl, no service start, no routing state — and traffic goes
    straight to the provider."""
    # The configured port names where a proxy *would* serve; nothing may start
    # one for a disabled profile.
    unused_port = _free_port()
    (hermes_home / "config.yaml").write_text(
        f"llm_usage_proxy:\n  enabled: false\n  port: {unused_port}\n"
    )
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"

    systemctl_calls = _install_fake_systemctl(monkeypatch)
    from plugins.llm_usage_proxy import lifecycle

    reconcile_calls: list[dict] = []
    monkeypatch.setattr(
        lifecycle,
        "reconcile_proxy_on_load",
        lambda **kwargs: reconcile_calls.append(kwargs),
    )

    from openai import OpenAI

    http_client = build_keepalive_http_client(logical_base)
    try:
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
    finally:
        http_client.close()

    # No service work for a disabled profile, and nothing listening as a proxy.
    assert reconcile_calls == []
    assert systemctl_calls == []
    assert port_in_use(unused_port) is False

    # The request went direct: the provider answered it itself, nothing was
    # ledgered, and routing is still off for this profile.
    assert len(upstream.requests) == 1
    assert upstream.requests[0]["path"].startswith("/v1/chat/completions")
    state = routing_state()
    assert state["active"] is False
    assert state["routes"] == {}
    assert base_url_routable(logical_base) is False


def test_unavailable_sidecar_stays_direct_and_is_not_retried(
    hermes_home, start_upstream, request, monkeypatch
):
    """Enabled config but the port is held by a listener that does not verify
    as this profile's proxy: the bootstrap stands down, traffic stays direct,
    and no later client re-attempts the reconcile."""
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"

    from plugins.llm_usage_proxy import lifecycle

    # A listener that is not ours (a generic health answer is not identity).
    foreign = _start_foreign_listener(request)
    (hermes_home / "config.yaml").write_text(
        f"llm_usage_proxy:\n  enabled: true\n  port: {foreign.server_address[1]}\n"
    )
    _install_fake_systemctl(monkeypatch)
    reconciles = _count_reconciles(monkeypatch)
    # Keep the stand-down probe short; it must still fail to verify.
    monkeypatch.setattr(lifecycle, "HEALTH_WAIT_TIMEOUT_SEC", 0.25)

    http_client = build_keepalive_http_client(logical_base)
    # A second and third client must not each take their own shot at it.
    extra_clients = [build_keepalive_http_client(logical_base) for _ in range(2)]
    try:
        response = http_client.post(
            f"{logical_base}/chat/completions",
            json={"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        )
        assert response.status_code == 200
    finally:
        http_client.close()
        for extra in extra_clients:
            extra.close()

    assert reconciles == [1]
    state = routing_state()
    assert state["active"] is False
    assert state["routes"] == {}
    assert "did not verify" in state["reason"]

    # Direct: the request bypassed the foreign listener entirely.
    assert len(upstream.requests) == 1
    assert upstream.requests[0]["path"].startswith("/v1/chat/completions")


def test_lazy_bootstrap_never_overrides_an_explicit_deactivation(
    hermes_home, monkeypatch
):
    """A profile that already has routing state — here deactivated on purpose —
    keeps that decision: the bootstrap must not re-reconcile over it."""
    (hermes_home / "config.yaml").write_text("llm_usage_proxy:\n  enabled: true\n")
    upstream_base = "https://fake-zai.test/v4"
    proxy_port = _free_port()
    errors = register_route_table(
        f"http://127.0.0.1:{proxy_port}", {ROUTE_NAME: upstream_base}
    )
    assert errors == []
    deactivate_routing("stood down by an earlier reconcile")

    systemctl_calls = _install_fake_systemctl(monkeypatch)
    reconcile_calls: list[dict] = []
    from plugins.llm_usage_proxy import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "reconcile_proxy_on_load",
        lambda **kwargs: reconcile_calls.append(kwargs),
    )

    # The table stays as the earlier decision left it: registered, so a client
    # built for this base is still an eligible construct, but standing down —
    # nothing is rerouted, and no reconcile is attempted over the decision.
    assert base_url_routable(upstream_base) is True
    assert reroute_url(upstream_base) is None
    assert routing_state()["active"] is False
    assert routing_state()["reason"] == "stood down by an earlier reconcile"
    assert reconcile_calls == []
    assert systemctl_calls == []


def test_explicit_profile_never_bootstraps_and_current_profile_still_does(
    hermes_home, start_upstream, start_proxy, monkeypatch, fake_dns
):
    """A client built for an explicitly named profile must not reconcile the
    current profile's service — and must not consume the current profile's
    one lazy attempt either."""
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    fake_dns[LOGICAL_HOST] = "127.0.0.1"
    logical_base = f"http://{LOGICAL_HOST}:{upstream.server_address[1]}/v4"
    proxy_port = _free_port()
    _write_enabled_config(hermes_home, proxy_port=proxy_port, logical_base=logical_base)

    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.systemd import profile_identity

    start_proxy(
        build_route_table(load_llm_usage_proxy_config()),
        identity=profile_identity(hermes_home),
        port=proxy_port,
    )

    systemctl_calls = _install_fake_systemctl(monkeypatch)
    reconciles = _count_reconciles(monkeypatch)

    # A foreign profile key: nothing to consult, and no reconcile on behalf of
    # the profile whose config actually enables the proxy.
    foreign_profile = str((hermes_home.parent / "sibling-profile").resolve())
    assert base_url_routable(logical_base, profile=foreign_profile) is False
    assert reconciles == []
    assert systemctl_calls == []
    assert routing_state()["routes"] == {}

    # The current profile's first client still bootstraps and gets routing.
    http_client = build_keepalive_http_client(logical_base)
    try:
        assert routing_state()["active"] is True
        assert routing_state()["routes"][ROUTE_NAME] == logical_base
        response = http_client.post(
            f"{logical_base}/chat/completions",
            json={"model": "glm-5", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        )
        assert response.status_code == 200
    finally:
        http_client.close()

    assert reconciles == [1]
    assert len(upstream.requests) == 1


def test_bootstrap_runs_once_per_profile_and_cannot_recurse(
    hermes_home, start_upstream, start_proxy, monkeypatch, fake_dns
):
    """The reconcile is entered exactly once per profile, and a registry read
    made from inside it consults the registry as it stands instead of
    recursing (the lifecycle itself builds HTTP clients)."""
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    fake_dns[LOGICAL_HOST] = "127.0.0.1"
    logical_base = f"http://{LOGICAL_HOST}:{upstream.server_address[1]}/v4"
    proxy_port = _free_port()
    _write_enabled_config(hermes_home, proxy_port=proxy_port, logical_base=logical_base)

    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.systemd import profile_identity

    start_proxy(
        build_route_table(load_llm_usage_proxy_config()),
        identity=profile_identity(hermes_home),
        port=proxy_port,
    )
    _install_fake_systemctl(monkeypatch)

    from plugins.llm_usage_proxy import lifecycle

    real = lifecycle.reconcile_proxy_on_load
    calls: list[tuple[int, bool]] = []

    def _probing_reconcile(**kwargs):
        # Read made while the bootstrap is in flight: no routes yet, and no
        # nested second entry into the lifecycle.
        calls.append((len(calls) + 1, base_url_routable(logical_base)))
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "reconcile_proxy_on_load", _probing_reconcile)

    first = build_keepalive_http_client(logical_base)
    second = build_keepalive_http_client(logical_base)
    try:
        assert base_url_routable(logical_base) is True
    finally:
        first.close()
        second.close()

    # Entered once; the in-flight read saw no routes and did not re-enter.
    assert calls == [(1, False)]
    assert routing_state()["active"] is True


def test_concurrent_first_clients_do_not_slip_through_unrouted(
    hermes_home, start_upstream, start_proxy, tmp_path, monkeypatch, fake_dns
):
    """The first initialization is serialized: several threads building their
    first client at the same moment all wait for the bootstrap's decision
    instead of each deciding for itself while routing is still undecided."""
    upstream = start_upstream(respond_json(_openai_completion_payload()))
    fake_dns[LOGICAL_HOST] = "127.0.0.1"
    logical_base = f"http://{LOGICAL_HOST}:{upstream.server_address[1]}/v4"
    proxy_port = _free_port()
    _write_enabled_config(hermes_home, proxy_port=proxy_port, logical_base=logical_base)

    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
    from plugins.llm_usage_proxy.routes import build_route_table
    from plugins.llm_usage_proxy.systemd import profile_identity

    start_proxy(
        build_route_table(load_llm_usage_proxy_config()),
        identity=profile_identity(hermes_home),
        port=proxy_port,
        db_name="usage-concurrent.sqlite",
    )
    _install_fake_systemctl(monkeypatch)
    reconciles = _count_reconciles(monkeypatch)

    from openai import OpenAI

    workers = 6
    barrier = threading.Barrier(workers)
    contents: list = [None] * workers
    failures: list[BaseException] = []
    clients: list = []

    def _first_client(index: int) -> None:
        http_client = None
        try:
            barrier.wait(30)
            http_client = build_keepalive_http_client(logical_base)
            clients.append(http_client)
            client = OpenAI(
                api_key=OPENAI_KEY,
                base_url=logical_base,
                http_client=http_client,
            )
            response = client.chat.completions.create(
                model="glm-5",
                messages=[{"role": "user", "content": "hi"}],
            )
            contents[index] = response.choices[0].message.content
        except BaseException as exc:  # re-surfaced below, never swallowed
            failures.append(exc)
        finally:
            if http_client is not None:
                http_client.close()

    threads = [
        threading.Thread(target=_first_client, args=(index,)) for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert contents == ["ok"] * workers

    # Exactly one of them initialized routing...
    assert reconciles == [1]
    # ...and every one of their requests was metered: a client that had
    # slipped through unrouted would have reached the upstream directly and
    # left this ledger short.
    rows = wait_for_row_count(tmp_path / "usage-concurrent.sqlite", workers)
    assert len(rows) == workers
    assert {row["upstream"] for row in rows} == {ROUTE_NAME}
    assert len(upstream.requests) == workers
