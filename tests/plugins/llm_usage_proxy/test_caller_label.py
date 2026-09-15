"""The caller label: harness attribution that is never a credential.

A client names itself with ``X-Usage-Caller``. The label is recorded on the
ledger row whether or not the proxy runs in key-manager mode, a matched
caller token wins over it, and it is stripped before anything is forwarded
upstream — it is a name, not a credential, so it can never authenticate and
never cause a refusal.

Claude Code's connectivity probes cannot send custom headers at all, so
their ``claude-cli/…`` User-Agent (and the bare ``axios/…`` of its bundled
axios, and Node's own ``node`` fetch UA) is accepted as a name of last
resort.
Together the three name every caller the proxy knows about — and in
key-manager mode a request nothing can name is refused before the upstream
call, its row recorded under the sentinel caller ``unattributed``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.process_bootstrap import build_keepalive_http_client
from hermes_cli.llm_usage_routes import (
    HERMES_CALLER_LABEL,
    _caller_label_for_profile,
    _proxied_request,
    _reset_registry,
    activate_routing,
    register_route_table,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.llm_usage_proxy.server import (
    CALLER_LABEL_HEADER,
    CALLER_LABEL_MAX_CHARS,
    CALLER_LABEL_RE,
    CALLER_TOKEN_HEADER,
    KeyStore,
    caller_label_from_user_agent,
    sanitize_caller_label,
)

from conftest import proxy_request, respond_json, wait_for_row_count

STORE_KEY = "sk-store-key-cccccccc"
LABEL = "codex-cli"


def _zai(upstream) -> dict:
    return {"zai": f"http://127.0.0.1:{upstream.server_address[1]}/v4"}


def _post(port, *, headers, body=None):
    return proxy_request(
        port,
        "POST",
        "/p/zai/chat/completions",
        body=body if body is not None else json.dumps({"model": "glm-5"}).encode(),
        headers=headers,
    )


def _upstream_headers(upstream) -> dict:
    return {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}


# ── 1. The sanitizer ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["hermes", LABEL, "a.b_c:d", "0" * CALLER_LABEL_MAX_CHARS, "  hermes  "],
)
def test_safe_labels_are_accepted(value):
    assert sanitize_caller_label(value) == value.strip()


def test_long_label_is_capped_at_the_first_safe_characters():
    assert sanitize_caller_label("a" * (CALLER_LABEL_MAX_CHARS + 1)) == (
        "a" * CALLER_LABEL_MAX_CHARS
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "   ",
        "has spaces",
        "needs/no/slashes",
        "ünïcode",
        # One bad character inside the cap poisons the whole label.
        "a" * (CALLER_LABEL_MAX_CHARS - 1) + "!",
    ],
)
def test_unusable_labels_are_ignored_not_rewritten(value):
    assert sanitize_caller_label(value) is None


# ── 2. Attribution on the ledger ─────────────────────────────────────────────


def test_label_recorded_without_manage_keys(start_upstream, start_proxy):
    """Plain passthrough mode still attributes the row to the label."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    status, _, _ = _post(proxy.server_address[1], headers={CALLER_LABEL_HEADER: LABEL})

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == LABEL
    assert rows[0]["outcome"] == "completed"


def test_missing_label_stays_null(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    _post(proxy.server_address[1], headers={})

    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] is None


def test_invalid_label_is_ignored_and_still_served(start_upstream, start_proxy):
    """An unusable label is dropped; it neither breaks nor renames the row."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    for bad in ("has spaces", "needs/no/slashes", "ünïcode"):
        status, _, _ = _post(proxy.server_address[1], headers={CALLER_LABEL_HEADER: bad})
        assert status == 200

    rows = wait_for_row_count(proxy.store.path, 3)
    assert [row["caller"] for row in rows] == [None, None, None]


def test_token_name_wins_over_label(start_upstream, start_proxy, tmp_path):
    """A matched caller token is proof of identity; the label is only a claim."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY])
    token, _ = store.create_caller("alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="managed.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = _post(
        proxy.server_address[1],
        headers={"Authorization": f"Bearer {token}", CALLER_LABEL_HEADER: LABEL},
    )

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "alice"


def test_label_does_not_authenticate_when_callers_exist(
    start_upstream, start_proxy, tmp_path
):
    """A label is never a credential: with caller tokens configured one is
    still required, and the refusal still records who claimed to be calling."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.create_caller("alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="managed.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, body = _post(proxy.server_address[1], headers={CALLER_LABEL_HEADER: LABEL})

    assert status == 401
    assert json.loads(body)["error"] == "missing caller token"
    assert upstream.requests == []
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == LABEL
    assert rows[0]["outcome"] == "rejected"


def test_label_with_a_valid_token_is_accepted(start_upstream, start_proxy, tmp_path):
    """Sending a label must not be what makes a request fail."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.create_caller("alice")
    token, _ = store.create_caller("bob")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="managed2.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = _post(
        proxy.server_address[1],
        headers={CALLER_TOKEN_HEADER: token, CALLER_LABEL_HEADER: LABEL},
    )

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "bob"


# ── 3. The label never travels upstream ──────────────────────────────────────


def test_label_and_token_headers_are_stripped(start_upstream, start_proxy, tmp_path):
    """Both caller headers are for the proxy alone; the provider sees neither."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY])
    token, _ = store.create_caller("alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="managed3.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    _post(
        proxy.server_address[1],
        headers={
            "Authorization": f"Bearer {token}",
            CALLER_LABEL_HEADER: LABEL,
            CALLER_TOKEN_HEADER: token,
        },
    )

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    assert CALLER_TOKEN_HEADER.lower() not in seen
    # The credential itself is replaced with the route's own key as before.
    assert seen["authorization"] == f"Bearer {STORE_KEY}"


def test_label_is_stripped_in_passthrough_mode_too(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    _post(
        proxy.server_address[1],
        headers={CALLER_LABEL_HEADER: LABEL, "Authorization": "Bearer sk-client"},
    )

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    assert seen["authorization"] == "Bearer sk-client"


# ── 4. Hermes labels the traffic it routes itself ────────────────────────────


@pytest.fixture
def profile_home(tmp_path):
    home = tmp_path / "profile-home"
    home.mkdir()
    return home


def _activate(home, *, proxy_port: int, logical_base: str) -> None:
    errors = register_route_table(
        f"http://127.0.0.1:{proxy_port}",
        {"zai": logical_base},
        profile=str(home.resolve()),
    )
    assert errors == []
    activate_routing(profile=str(home.resolve()))


def test_proxied_request_labels_itself_when_the_client_did_not():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    proxied = _proxied_request(request, "http://127.0.0.1:9/p/zai/v1/chat/completions")

    assert proxied.headers[CALLER_LABEL_HEADER] == HERMES_CALLER_LABEL


def test_proxied_request_keeps_a_client_supplied_label():
    request = httpx.Request(
        "POST",
        "https://api.example.test/v1/chat/completions",
        headers={CALLER_LABEL_HEADER: LABEL},
    )
    proxied = _proxied_request(request, "http://127.0.0.1:9/p/zai/v1/chat/completions")

    assert proxied.headers[CALLER_LABEL_HEADER] == LABEL


def test_routed_traffic_is_labeled_and_the_label_is_stripped(
    start_upstream, start_proxy, profile_home
):
    """End to end: a real routed Hermes client is attributed as "hermes"."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    token = set_hermes_home_override(str(profile_home))
    try:
        _activate(
            profile_home,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        with build_keepalive_http_client(logical_base) as client:
            response = client.post(
                f"{logical_base}/chat/completions",
                json={"model": "glm-5", "messages": []},
                headers={"Authorization": "Bearer sk-route-test"},
            )
            assert response.status_code == 200
    finally:
        reset_hermes_home_override(token)

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == HERMES_CALLER_LABEL


# The label a routed transport stamps is derived once, at construction, from
# the profile path it is bound to — the request path cannot resolve profiles.
@pytest.mark.parametrize(
    "home_name, expected",
    [
        # A home that is not under a profiles/ directory is the default
        # profile: the wire label stays exactly what it always was.
        ("plain-home", "hermes"),
        # A named profile home, in either layout.
        ("profiles/coder", "hermes:coder"),
        (".hermes/profiles/coder", "hermes:coder"),
        # A weird profile name is sanitized, never emitted verbatim.
        ("profiles/bad name!", "hermes:badname"),
        ("profiles/wow!!such~chars", "hermes:wowsuchchars"),
        # A name nothing survives sanitizing falls back to the default label.
        ("profiles/!!!", "hermes"),
        ("profiles/…", "hermes"),
        # The whole label is capped at the proxy's limit, prefix included.
        ("profiles/" + "x" * 80, "hermes:" + "x" * 57),
    ],
)
def test_caller_label_is_derived_from_the_bound_profile_home(
    tmp_path, monkeypatch, home_name, expected
):
    monkeypatch.delenv("HERMES_HOME", raising=False)

    label = _caller_label_for_profile(str(tmp_path / home_name))

    assert label == expected
    # Whatever the profile name, the proxy always sees a usable label.
    assert len(label) <= CALLER_LABEL_MAX_CHARS
    assert CALLER_LABEL_RE.match(label)


def test_named_profile_routed_traffic_is_labeled_with_the_profile_name(
    start_upstream, start_proxy, tmp_path
):
    """End to end: a named profile's routed client is attributed "hermes:<name>"."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    token = set_hermes_home_override(str(home))
    try:
        _activate(
            home,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        with build_keepalive_http_client(logical_base) as client:
            response = client.post(
                f"{logical_base}/chat/completions",
                json={"model": "glm-5", "messages": []},
                headers={"Authorization": "Bearer sk-route-test"},
            )
            assert response.status_code == 200
    finally:
        reset_hermes_home_override(token)

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "hermes:coder"


def test_named_profile_proxied_request_keeps_a_client_supplied_label():
    """The profile label is a default, not an override: precedence is unchanged."""
    request = httpx.Request(
        "POST",
        "https://api.example.test/v1/chat/completions",
        headers={CALLER_LABEL_HEADER: LABEL},
    )
    proxied = _proxied_request(
        request, "http://127.0.0.1:9/p/zai/v1/chat/completions", "hermes:coder"
    )

    assert proxied.headers[CALLER_LABEL_HEADER] == LABEL


# A profile can also *choose* its label: llm_usage_proxy.caller_label in the
# profile's own config replaces the derived one — for the default profile too,
# so it can show up as its own subcategory instead of the flat aggregate.
_UNSET = object()


def _write_home_config(home, *, caller_label=_UNSET) -> None:
    lines = ["llm_usage_proxy:"]
    if caller_label is not _UNSET:
        # json.dumps quotes scalars safely for YAML (spaces, unicode, long runs).
        lines.append("  caller_label: " + json.dumps(caller_label))
    else:
        lines.append("  enabled: true")
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "home_name, derived",
    [("plain-home", "hermes"), ("profiles/coder", "hermes:coder")],
)
def test_configured_caller_label_replaces_the_derived_one(
    tmp_path, monkeypatch, home_name, derived
):
    home = tmp_path / home_name
    home.mkdir(parents=True)
    _write_home_config(home, caller_label="hermes:studio")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    token = set_hermes_home_override(str(home))
    try:
        label = _caller_label_for_profile(str(home.resolve()))
    finally:
        reset_hermes_home_override(token)

    assert label == "hermes:studio"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "has spaces",
        "needs/slashes",
        "ünïcode",
        # Over-long is refused whole, not truncated to a prefix.
        "a" * (CALLER_LABEL_MAX_CHARS + 1),
        # Non-string YAML values are not labels.
        123,
        True,
    ],
)
def test_unusable_configured_caller_label_falls_back_to_derivation(
    tmp_path, monkeypatch, value
):
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    _write_home_config(home, caller_label=value)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    token = set_hermes_home_override(str(home))
    try:
        label = _caller_label_for_profile(str(home.resolve()))
    finally:
        reset_hermes_home_override(token)

    assert label == "hermes:coder"
    assert CALLER_LABEL_RE.match(label)


def test_configured_caller_label_absent_or_foreign_derives_as_before(
    tmp_path, monkeypatch
):
    """No key, or a section another home's config belongs to, changes nothing."""
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    # A section without the key — and, below, a config read for a *different*
    # home than the bound profile — must both derive exactly as before.
    _write_home_config(home)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    token = set_hermes_home_override(str(home))
    try:
        assert _caller_label_for_profile(str(home.resolve())) == "hermes:coder"
    finally:
        reset_hermes_home_override(token)

    other = tmp_path / "other-home"
    other.mkdir()
    _write_home_config(other, caller_label="hermes:someone-else")
    assert _caller_label_for_profile(str(home.resolve())) == "hermes:coder"


def test_default_home_configured_label_end_to_end(
    start_upstream, start_proxy, profile_home
):
    """End to end: a default-home profile that chose a label is attributed it."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    _write_home_config(profile_home, caller_label="hermes:studio")
    token = set_hermes_home_override(str(profile_home))
    try:
        _activate(
            profile_home,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        with build_keepalive_http_client(logical_base) as client:
            response = client.post(
                f"{logical_base}/chat/completions",
                json={"model": "glm-5", "messages": []},
                headers={"Authorization": "Bearer sk-route-test"},
            )
            assert response.status_code == 200
    finally:
        reset_hermes_home_override(token)

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "hermes:studio"


def test_named_profile_configured_label_beats_the_profile_name(
    start_upstream, start_proxy, tmp_path
):
    """End to end: the chosen label wins over the derived ``hermes:<name>``."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    _write_home_config(home, caller_label="hermes:studio")
    token = set_hermes_home_override(str(home))
    try:
        _activate(
            home,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        with build_keepalive_http_client(logical_base) as client:
            response = client.post(
                f"{logical_base}/chat/completions",
                json={"model": "glm-5", "messages": []},
                headers={"Authorization": "Bearer sk-route-test"},
            )
            assert response.status_code == 200
    finally:
        reset_hermes_home_override(token)

    seen = _upstream_headers(upstream)
    assert CALLER_LABEL_HEADER.lower() not in seen
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "hermes:studio"


def test_configured_label_still_yields_to_a_client_supplied_one(
    start_upstream, start_proxy, profile_home
):
    """The configured label is a default like the derived one: a request that
    already names itself keeps its own name."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    logical_base = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    _write_home_config(profile_home, caller_label="hermes:studio")
    token = set_hermes_home_override(str(profile_home))
    try:
        _activate(
            profile_home,
            proxy_port=proxy.server_address[1],
            logical_base=logical_base,
        )
        with build_keepalive_http_client(logical_base) as client:
            response = client.post(
                f"{logical_base}/chat/completions",
                json={"model": "glm-5", "messages": []},
                headers={
                    "Authorization": "Bearer sk-route-test",
                    CALLER_LABEL_HEADER: LABEL,
                },
            )
            assert response.status_code == 200
    finally:
        reset_hermes_home_override(token)

    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == LABEL

# ── 5. The harness User-Agent, and the managed-mode attribution gate ─────────

CLAUDE_CLI_UA = "claude-cli/2.1.226 (external, cli)"
AXIOS_UA = "axios/1.12.2"
NODE_UA = "node"

GATE_ERROR = (
    "unattributed request: send a caller token, an 'X-Usage-Caller: <label>'"
    " header, or a known harness User-Agent"
)


def _managed(tmp_path, *, with_caller):
    """A key store for managed mode, with a caller token minted or not."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY])
    if with_caller is not None:
        store.create_caller(with_caller)
    return keys_file


@pytest.mark.parametrize(
    "user_agent, expected",
    [
        (None, None),
        ("", None),
        (CLAUDE_CLI_UA, "claude-code"),
        (AXIOS_UA, "claude-code"),
        (NODE_UA, "claude-code"),
        ("node/22.14.0", "claude-code"),
        ("nodejs/22.14.0", None),
        ("Mozilla/5.0", None),
    ],
)
def test_only_the_harness_probe_user_agents_name_a_caller(user_agent, expected):
    assert caller_label_from_user_agent(user_agent) == expected


def test_probe_user_agent_names_the_row_and_travels_upstream(
    start_upstream, start_proxy, tmp_path
):
    """The CLI's probes cannot send custom headers; their UA is their name."""
    keys_file = _managed(tmp_path, with_caller="alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="probe.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, _ = _post(proxy.server_address[1], headers={"User-Agent": CLAUDE_CLI_UA})

    assert status == 200
    assert len(upstream.requests) == 1
    seen = _upstream_headers(upstream)
    # The UA is a name for the proxy, not a credential to hide from the provider.
    assert seen["user-agent"] == CLAUDE_CLI_UA
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "claude-code"


def test_axios_probe_user_agent_passes_the_gate_as_claude_code(
    start_upstream, start_proxy, tmp_path
):
    """The CLI's bundled axios probes with a bare "axios/<version>" UA; the
    attribution gate admits them as claude-code like the claude-cli probes."""
    keys_file = _managed(tmp_path, with_caller="alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="axios-probe.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, _ = _post(proxy.server_address[1], headers={"User-Agent": AXIOS_UA})

    assert status == 200
    assert len(upstream.requests) == 1
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "claude-code"


def test_node_fetch_probe_user_agent_passes_the_gate_as_claude_code(
    start_upstream, start_proxy, tmp_path
):
    """The CLI's HEAD probes ride on Node's own fetch, which names itself the
    bare "node"; the attribution gate admits them as claude-code too."""
    keys_file = _managed(tmp_path, with_caller="alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="node-probe.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "HEAD",
        "/p/zai/chat/completions",
        headers={"User-Agent": NODE_UA},
    )

    assert status == 200
    assert len(upstream.requests) == 1
    assert upstream.requests[0]["method"] == "HEAD"
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "claude-code"


def test_unattributed_request_is_refused_before_the_upstream_call(
    start_upstream, start_proxy, tmp_path
):
    keys_file = _managed(tmp_path, with_caller="alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="gate.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, body = _post(proxy.server_address[1], headers={})

    assert status == 401
    assert json.loads(body)["error"] == GATE_ERROR
    assert upstream.requests == []
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "unattributed"
    assert rows[0]["outcome"] == "rejected"
    assert rows[0]["status_code"] == 401


def test_gate_follows_manage_keys_even_with_zero_caller_tokens(
    start_upstream, start_proxy, tmp_path
):
    """Managed mode requires attribution on its own; a caller-token-count
    condition would leave the gate inert on the tokenless live deployment,
    where Hermes's traffic is label-attributed and nothing else should pass."""
    keys_file = _managed(tmp_path, with_caller=None)
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="gate0.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, body = _post(proxy.server_address[1], headers={})

    assert status == 401
    assert json.loads(body)["error"] == GATE_ERROR
    assert upstream.requests == []
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "unattributed"


def test_token_name_beats_label_and_user_agent(start_upstream, start_proxy, tmp_path):
    """A matched token is proof; label and UA are only claims."""
    keys_file = str(tmp_path / "keys" / "keys.json")
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY])
    token, _ = store.create_caller("alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream), db_name="precedence.sqlite", manage_keys=True, keys_path=keys_file
    )

    status, _, _ = _post(
        proxy.server_address[1],
        headers={
            "Authorization": f"Bearer {token}",
            CALLER_LABEL_HEADER: "something-else",
            "User-Agent": CLAUDE_CLI_UA,
        },
    )

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "alice"


def test_label_beats_user_agent(start_upstream, start_proxy, tmp_path):
    """A label carries tokenless managed traffic (Hermes's own shape) even
    when the UA names no harness."""
    keys_file = _managed(tmp_path, with_caller=None)
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="label-ua.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = _post(
        proxy.server_address[1],
        headers={CALLER_LABEL_HEADER: "claude-code", "User-Agent": "totally-other/1.0"},
    )

    assert status == 200
    assert len(upstream.requests) == 1
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "claude-code"


def test_label_wins_over_a_known_harness_user_agent(start_upstream, start_proxy):
    """The resolution order is token → label → UA, so an explicit label is
    recorded even when the UA alone would have named a harness."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    _post(
        proxy.server_address[1],
        headers={CALLER_LABEL_HEADER: LABEL, "User-Agent": CLAUDE_CLI_UA},
    )

    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == LABEL


def test_invalid_label_with_no_other_name_hits_the_gate(
    start_upstream, start_proxy, tmp_path
):
    """An unusable label sanitizes to nothing, so with no token and no known
    UA the request is as nameless as one that sent no header at all."""
    keys_file = _managed(tmp_path, with_caller="alice")
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        _zai(upstream),
        db_name="badlabel.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, body = _post(
        proxy.server_address[1], headers={CALLER_LABEL_HEADER: "bad label!"}
    )

    assert status == 401
    assert json.loads(body)["error"] == GATE_ERROR
    assert upstream.requests == []
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "unattributed"


# ── 6. Config schema registration ────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "llm_usage_proxy",
        "llm_usage_proxy.caller_label",
        "llm_usage_proxy.port",
        "llm_usage_proxy.enabled",
        "llm_usage_proxy.manage_keys",
    ],
)
def test_llm_usage_proxy_keys_validate_as_known(key):
    from hermes_cli.config import _validate_config_key

    is_known, suggestion = _validate_config_key(key)
    assert is_known is True
    assert suggestion is None


def test_llm_usage_proxy_caller_label_typo_suggests_caller_label():
    from hermes_cli.config import _validate_config_key

    is_known, suggestion = _validate_config_key("llm_usage_proxy.caller_lable")
    assert not is_known
    assert suggestion is not None
    assert "caller_label" in suggestion


@pytest.mark.parametrize("spelling", ["123", "true", "none", "1.5"])
def test_config_set_caller_label_preserves_string_spellings(spelling):
    """``config set llm_usage_proxy.caller_label X`` stores X verbatim as a str.

    The DEFAULT_CONFIG default is a str, so _coerce_config_set_value's
    string-preservation branch wins over int/bool/None/float coercion —
    a label that happens to spell a scalar is still a label.
    """
    from hermes_cli.config import _coerce_config_set_value

    value = _coerce_config_set_value("llm_usage_proxy.caller_label", spelling)
    assert isinstance(value, str)
    assert value == spelling


def test_configured_caller_label_inert_when_default_unset(tmp_path, monkeypatch):
    """The empty-string DEFAULT_CONFIG caller_label default must not produce a label."""
    from hermes_cli.llm_usage_routes import _configured_caller_label

    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    _write_home_config(home)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    token = set_hermes_home_override(str(home))
    try:
        assert _configured_caller_label(str(home.resolve())) is None
    finally:
        reset_hermes_home_override(token)


def test_default_config_llm_usage_proxy_port():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["llm_usage_proxy"]["port"] == 8790
