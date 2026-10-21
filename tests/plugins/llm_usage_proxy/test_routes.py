"""Profile-scoped usage-route registry: isolation and transport wrapping."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from agent.process_bootstrap import build_keepalive_http_client
from hermes_cli.llm_usage_routes import (
    _reset_registry,
    activate_routing,
    clear_route_table,
    register_route_table,
    reroute_url,
    routing_state,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from conftest import respond_json


@pytest.fixture(autouse=True)
def _clean_route_registry():
    _reset_registry()
    yield
    _reset_registry()


@pytest.fixture
def two_profiles(tmp_path):
    prof_a = tmp_path / "profA"
    prof_b = tmp_path / "profB"
    for path in (prof_a, prof_b):
        path.mkdir(parents=True)
    return prof_a, prof_b


def _under_override(home, fn):
    token = set_hermes_home_override(str(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


def _setup_profile_routing(
    home,
    *,
    proxy_port: int,
    logical_base: str,
    route_name: str = "test",
) -> str:
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


def _post_chat(client: httpx.Client, logical_base: str) -> httpx.Response:
    return client.post(
        f"{logical_base.rstrip('/')}/chat/completions",
        json={"model": "glm-5", "messages": []},
        headers={"Authorization": "Bearer sk-route-test"},
    )


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_enabled_profile_routes_through_proxy_other_stays_direct(
    start_upstream, start_proxy, two_profiles, async_mode
):
    """Profile A hits the proxy upstream; profile B hits the direct recorder."""
    direct = start_upstream(respond_json({"ok": True}))
    proxy_upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        {"test": f"http://127.0.0.1:{proxy_upstream.server_address[1]}/v4"}
    )
    logical_base = f"http://127.0.0.1:{direct.server_address[1]}/v1"
    prof_a, prof_b = two_profiles

    _under_override(
        prof_a,
        lambda: _setup_profile_routing(
            prof_a,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        ),
    )

    if async_mode:

        async def _run_a():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_a:
                resp_a = await client_a.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                assert resp_a.status_code == 200
                assert str(resp_a.request.url).startswith(logical_base)

        _under_override(prof_a, lambda: asyncio.run(_run_a()))
        assert len(proxy_upstream.requests) == 1
        assert len(direct.requests) == 0

        direct.requests.clear()
        proxy_upstream.requests.clear()

        async def _run_b():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_b:
                resp = await client_b.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                assert resp.status_code == 200
                assert str(resp.request.url).startswith(logical_base)

        _under_override(prof_b, lambda: asyncio.run(_run_b()))
        assert len(direct.requests) == 1
        assert len(proxy_upstream.requests) == 0
        assert direct.requests[0]["headers"]["Authorization"] == "Bearer sk-route-test"
    else:
        def _run_a():
            with build_keepalive_http_client(logical_base) as client_a:
                resp_a = _post_chat(client_a, logical_base)
                assert resp_a.status_code == 200
                assert str(resp_a.request.url).startswith(logical_base)

        _under_override(prof_a, _run_a)

        assert len(proxy_upstream.requests) == 1
        assert len(direct.requests) == 0
        assert (
            proxy_upstream.requests[0]["headers"]["Authorization"]
            == "Bearer sk-route-test"
        )

        direct.requests.clear()
        proxy_upstream.requests.clear()

        def _run_b():
            with build_keepalive_http_client(logical_base) as client_b:
                resp_b = _post_chat(client_b, logical_base)
                assert resp_b.status_code == 200
                assert str(resp_b.request.url).startswith(logical_base)

        _under_override(prof_b, _run_b)

        assert len(direct.requests) == 1
        assert len(proxy_upstream.requests) == 0


def test_wrapper_built_under_profile_a_keeps_routing_after_switch_to_b(
    start_upstream, start_proxy, two_profiles
):
    """Transport wrappers bind the constructing profile, not the active one."""
    direct = start_upstream(respond_json({"ok": True}))
    proxy_upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        {"test": f"http://127.0.0.1:{proxy_upstream.server_address[1]}/v4"}
    )
    logical_base = f"http://127.0.0.1:{direct.server_address[1]}/v1"
    prof_a, prof_b = two_profiles

    def _build_client():
        _setup_profile_routing(
            prof_a,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        return build_keepalive_http_client(logical_base)

    client_a = _under_override(prof_a, _build_client)

    _under_override(prof_b, lambda: _post_chat(client_a, logical_base).raise_for_status())

    assert len(proxy_upstream.requests) == 1
    assert len(direct.requests) == 0

    client_a.close()


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_clear_route_table_affects_only_calling_profile(
    start_upstream, start_proxy, two_profiles, async_mode
):
    """Clearing profile A must not disturb an already-registered profile B."""
    direct = start_upstream(respond_json({"ok": True}))
    proxy_upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        {"test": f"http://127.0.0.1:{proxy_upstream.server_address[1]}/v4"}
    )
    logical_base = f"http://127.0.0.1:{direct.server_address[1]}/v1"
    prof_a, prof_b = two_profiles

    _under_override(
        prof_a,
        lambda: _setup_profile_routing(
            prof_a,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        ),
    )
    _under_override(
        prof_b,
        lambda: _setup_profile_routing(
            prof_b,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        ),
    )

    key_a = str(prof_a.resolve())
    key_b = str(prof_b.resolve())
    assert routing_state(profile=key_a)["active"] is True
    assert routing_state(profile=key_b)["active"] is True

    if async_mode:

        async def _prove_a_proxy():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_a:
                resp_a = await client_a.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                resp_a.raise_for_status()

        async def _prove_b_proxy():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_b:
                resp_b = await client_b.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                resp_b.raise_for_status()

        _under_override(prof_a, lambda: asyncio.run(_prove_a_proxy()))
        _under_override(prof_b, lambda: asyncio.run(_prove_b_proxy()))
        assert len(proxy_upstream.requests) == 2
        assert len(direct.requests) == 0

        proxy_upstream.requests.clear()
        direct.requests.clear()

        _under_override(prof_a, lambda: clear_route_table(profile=str(prof_a.resolve())))

        async def _run_a_direct():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_a:
                resp_a = await client_a.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                resp_a.raise_for_status()

        _under_override(prof_a, lambda: asyncio.run(_run_a_direct()))
        assert len(direct.requests) == 1
        assert len(proxy_upstream.requests) == 0

        async def _run_b_proxy():
            async with build_keepalive_http_client(
                logical_base, async_mode=True
            ) as client_b:
                resp_b = await client_b.post(
                    f"{logical_base}/chat/completions",
                    json={"model": "glm-5", "messages": []},
                    headers={"Authorization": "Bearer sk-route-test"},
                )
                resp_b.raise_for_status()

        _under_override(prof_b, lambda: asyncio.run(_run_b_proxy()))
        assert len(proxy_upstream.requests) == 1
        assert len(direct.requests) == 1
    else:
        def _prove_a_proxy():
            with build_keepalive_http_client(logical_base) as client_a:
                _post_chat(client_a, logical_base).raise_for_status()

        def _prove_b_proxy():
            with build_keepalive_http_client(logical_base) as client_b:
                _post_chat(client_b, logical_base).raise_for_status()

        _under_override(prof_a, _prove_a_proxy)
        _under_override(prof_b, _prove_b_proxy)
        assert len(proxy_upstream.requests) == 2
        assert len(direct.requests) == 0

        proxy_upstream.requests.clear()
        direct.requests.clear()

        _under_override(prof_a, lambda: clear_route_table(profile=str(prof_a.resolve())))

        def _run_a_direct():
            with build_keepalive_http_client(logical_base) as client_a:
                _post_chat(client_a, logical_base).raise_for_status()

        _under_override(prof_a, _run_a_direct)
        assert len(direct.requests) == 1
        assert len(proxy_upstream.requests) == 0

        def _run_b_proxy():
            with build_keepalive_http_client(logical_base) as client_b:
                _post_chat(client_b, logical_base).raise_for_status()

        _under_override(prof_b, _run_b_proxy)
        assert len(proxy_upstream.requests) == 1
        assert len(direct.requests) == 1


def test_routing_state_and_reroute_url_are_profile_scoped(two_profiles):
    prof_a, prof_b = two_profiles
    key_a = str(prof_a.resolve())
    key_b = str(prof_b.resolve())

    _under_override(
        prof_a,
        lambda: register_route_table(
            "http://127.0.0.1:9101",
            {"zai": "https://api.z.ai/api/paas/v4"},
            profile=key_a,
        ),
    )
    _under_override(prof_a, lambda: activate_routing(profile=key_a))

    state_a = routing_state(profile=key_a)
    state_b = routing_state(profile=key_b)
    assert state_a["active"] is True
    assert state_a["routes"]["zai"] == "https://api.z.ai/api/paas/v4"
    assert state_b["active"] is False
    assert state_b["routes"] == {}

    routed = reroute_url(
        "https://api.z.ai/api/paas/v4/chat/completions",
        profile=key_a,
    )
    assert routed == "http://127.0.0.1:9101/p/zai/chat/completions"
    assert reroute_url(
        "https://api.z.ai/api/paas/v4/chat/completions",
        profile=key_b,
    ) is None


def test_env_override_base_reads_profile_scope_not_process_environ(
    two_profiles, monkeypatch
):
    """Discovery resolves the provider env var through the profile, not the shell.

    A value inherited from the parent shell must not win over the profile's
    own ``.env`` — otherwise route discovery in the multiplexed gateway would
    register a sibling profile's endpoint.
    """
    from plugins.llm_usage_proxy.routes import provider_route_bases

    prof_a, _ = two_profiles
    monkeypatch.setenv("GLM_BASE_URL", "https://shell-env.example/api/paas/v4")
    (prof_a / ".env").write_text(
        "GLM_BASE_URL=https://profile-env.example/api/paas/v4\n", encoding="utf-8"
    )

    bases = _under_override(prof_a, lambda: provider_route_bases("zai"))

    assert "https://profile-env.example/api/paas/v4" in bases
    assert "https://shell-env.example/api/paas/v4" not in bases
    # Discovery is a read: the process environment is left untouched.
    import os

    assert os.environ.get("GLM_BASE_URL") == "https://shell-env.example/api/paas/v4"


def test_pool_entry_bases_are_read_from_disk_without_mutating_credentials(
    two_profiles, monkeypatch
):
    """Route discovery must not select, seed, refresh, or rewrite the pool."""
    from plugins.llm_usage_proxy.routes import provider_route_bases

    prof_a, _ = two_profiles
    auth_file = prof_a / "auth.json"
    pool_entry = {
        "id": "acct-1",
        "label": "rotation-a",
        "auth_type": "api_key",
        "priority": 0,
        "source": "test",
        "access_token": "sk-test",
        "base_url": "https://pool-pinned.example/api/paas/v4",
    }
    auth_file.write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": {"zai": [pool_entry]}}),
        encoding="utf-8",
    )
    before = auth_file.read_bytes()

    def _discover():
        assert provider_route_bases("zai") == [
            "https://api.z.ai/api/paas/v4",
            "https://api.z.ai/api/coding/paas/v4",
            "https://pool-pinned.example/api/paas/v4",
        ]
        # Any of load_pool's write-throughs would have changed the bytes.
        assert auth_file.read_bytes() == before
        assert not list(prof_a.glob("*.corrupt"))

    _under_override(prof_a, _discover)
