"""The caller label: harness attribution that is never a credential.

A client names itself with ``X-Usage-Caller``. The label is recorded on the
ledger row whether or not the proxy runs in key-manager mode, a matched
caller token wins over it, and it is stripped before anything is forwarded
upstream — it is a name, not a credential, so it can never authenticate and
never cause a refusal.

Claude Code's connectivity probes cannot send custom headers at all, so
their ``claude-cli/…`` User-Agent is accepted as a name of last resort.
Together the two name every caller the proxy knows about — and in
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
    _proxied_request,
    _reset_registry,
    activate_routing,
    register_route_table,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.llm_usage_proxy.server import (
    CALLER_LABEL_HEADER,
    CALLER_LABEL_MAX_CHARS,
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


# ── 5. The harness User-Agent, and the managed-mode attribution gate ─────────

CLAUDE_CLI_UA = "claude-cli/2.1.226 (external, cli)"

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
        ("Mozilla/5.0", None),
    ],
)
def test_only_the_cli_probe_user_agent_names_a_caller(user_agent, expected):
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
