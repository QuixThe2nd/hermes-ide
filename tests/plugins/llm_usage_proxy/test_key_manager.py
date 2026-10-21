"""Key-manager mode: the key store, caller tokens, and credential injection.

Everything runs against loopback fake upstreams on ephemeral ports — the
"provider keys" here are throwaway strings, never real credentials, and the
key store lives in a temp HERMES_HOME.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat

import pytest

from plugins.llm_usage_proxy.server import (
    CALLER_LABEL_HEADER,
    CALLER_TOKEN_HEADER,
    KeyRotator,
    KeyStore,
    apply_upstream_auth,
    bearer_token,
    key_fingerprint,
)

from conftest import proxy_request, respond_json, wait_for_row_count

CLIENT_KEY = "sk-client-holds-this-one-123456"
STORE_KEY_A = "sk-store-key-aaaaaaaa"
STORE_KEY_B = "sk-store-key-bbbbbbbb"


def _zai(upstream) -> dict:
    return {"zai": f"http://127.0.0.1:{upstream.server_address[1]}/v4"}


@pytest.fixture
def keys_file(tmp_path):
    """A key store outside the proxy's own tmp dir, as the unit would have."""
    return str(tmp_path / "keys" / "keys.json")


@pytest.fixture
def managed_store(keys_file):
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A, STORE_KEY_B])
    return store


def _authorized_proxy(start_proxy, upstream, keys_file, *, store_keys=True):
    """A --manage-keys proxy whose store holds the caller token it returns."""
    store = KeyStore(keys_file)
    if store_keys:
        store.set_route_keys("zai", [STORE_KEY_A, STORE_KEY_B])
    token, _ = store.create_caller("alice")
    proxy = start_proxy(
        _zai(upstream),
        db_name="managed.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )
    return proxy, store, token


def _upstream_auth_headers(upstream) -> dict:
    """Headers the fake upstream saw on its first (only) request."""
    return {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}


# ── 1. The key store: file modes, shapes, redaction ─────────────────────────


def test_key_store_file_and_directory_modes(keys_file):
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A])
    store.create_caller("alice")

    # 0700/0600 exactly: the store chmods explicitly rather than trusting the
    # process umask, so a 022 umask cannot leave a provider key world-readable.
    assert stat.S_IMODE(os.stat(keys_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(keys_file)).st_mode) == 0o700


def test_key_store_set_replaces_dedupes_and_keeps_rotation_order(keys_file):
    store = KeyStore(keys_file)
    assert store.set_route_keys("zai", [STORE_KEY_B, STORE_KEY_A, STORE_KEY_B]) == (
        STORE_KEY_B,
        STORE_KEY_A,
    )
    assert store.route_keys("zai") == (STORE_KEY_B, STORE_KEY_A)
    # A second `set` replaces rather than appending to the rotation.
    store.set_route_keys("zai", [STORE_KEY_A])
    assert store.route_keys("zai") == (STORE_KEY_A,)


def test_key_store_rejects_bad_names_and_empty_key_lists(keys_file):
    store = KeyStore(keys_file)
    with pytest.raises(ValueError):
        store.set_route_keys("../escape", [STORE_KEY_A])
    with pytest.raises(ValueError):
        store.set_route_keys("zai", ["", "  "])
    with pytest.raises(ValueError):
        store.create_caller("has space")


def test_key_store_persists_and_is_seen_by_a_later_instance(keys_file):
    KeyStore(keys_file).set_route_keys("zai", [STORE_KEY_A])
    token, _ = KeyStore(keys_file).create_caller("alice")

    reopened = KeyStore(keys_file)
    assert reopened.route_keys("zai") == (STORE_KEY_A,)
    assert reopened.caller_for_token(token) == "alice"


def test_running_store_picks_up_a_cli_write_without_restart(keys_file):
    """The server holds its store for the process lifetime; the CLI does not.

    A running proxy must serve a newly added key without a restart, or every
    `keys set` would need `manage-keys off`/`on` around it.
    """
    server_side = KeyStore(keys_file)
    assert server_side.route_keys("zai") == ()

    cli_side = KeyStore(keys_file)
    cli_side.set_route_keys("zai", [STORE_KEY_A])
    cli_side.create_caller("alice")

    assert server_side.route_keys("zai") == (STORE_KEY_A,)
    assert server_side.caller_count() == 1


def test_key_store_describe_shows_fingerprints_never_secrets(keys_file):
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A])
    token, _ = store.create_caller("alice")

    described = json.dumps(store.describe())
    assert STORE_KEY_A not in described
    assert token not in described
    assert key_fingerprint(STORE_KEY_A) in described


def test_broken_key_store_is_treated_as_empty_not_fatal(keys_file):
    os.makedirs(os.path.dirname(keys_file), exist_ok=True)
    with open(keys_file, "w", encoding="utf-8") as handle:
        handle.write("{not json")

    store = KeyStore(keys_file)
    assert store.route_keys("zai") == ()
    assert store.caller_count() == 0
    # A later write rebuilds a valid store rather than preserving the damage.
    store.set_route_keys("zai", [STORE_KEY_A])
    assert KeyStore(keys_file).route_keys("zai") == (STORE_KEY_A,)


def test_key_store_ignores_malformed_entries(keys_file):
    os.makedirs(os.path.dirname(keys_file), exist_ok=True)
    with open(keys_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 1,
                "routes": {"zai": [STORE_KEY_A], "../x": ["nope"], "bad": "not-a-list"},
                "callers": {"alice": "tok", "bad name": "tok"},
            },
            handle,
        )

    store = KeyStore(keys_file)
    assert store.route_keys("zai") == (STORE_KEY_A,)
    assert store.route_keys("../x") == ()
    assert store.caller_for_token("tok") == "alice"


def test_key_rotator_round_robins_and_step_does_not_advance(managed_store):
    store = managed_store
    rotator = KeyRotator(store)

    issued = [rotator.issue("zai") for _ in range(4)]
    assert [key for key, _ in issued] == [STORE_KEY_A, STORE_KEY_B] * 2

    # A retry steps to the neighbour of the key it is replacing; the cursor
    # stays where `issue` left it, so the next request still gets its own turn.
    assert rotator.step("zai", 0) == (STORE_KEY_B, 1)
    assert rotator.step("zai", 1) == (STORE_KEY_A, 0)
    assert rotator.issue("zai") == (STORE_KEY_A, 0)
    assert rotator.step("route-without-keys", 0) == (None, -1)


def test_apply_upstream_auth_keeps_the_clients_dialect():
    headers = [
        ("Authorization", f"Bearer {CLIENT_KEY}"),
        ("X-Api-Key", CLIENT_KEY),
        ("Host", "upstream"),
    ]
    assert apply_upstream_auth(headers, STORE_KEY_A) == [
        ("Authorization", f"Bearer {STORE_KEY_A}"),
        ("X-Api-Key", STORE_KEY_A),
        ("Host", "upstream"),
    ]
    # No credential at all: a Bearer header is injected (OpenAI-compatible
    # default) rather than sending the request unauthenticated.
    assert apply_upstream_auth([("Host", "upstream")], STORE_KEY_A) == [
        ("Host", "upstream"),
        ("Authorization", f"Bearer {STORE_KEY_A}"),
    ]
    # The input list is never mutated in place.
    assert headers[0] == ("Authorization", f"Bearer {CLIENT_KEY}")


def test_bearer_token_parses_only_bearer_scheme():
    assert bearer_token(f"Bearer {CLIENT_KEY}") == CLIENT_KEY
    assert bearer_token(f"bearer {CLIENT_KEY}") == CLIENT_KEY
    assert bearer_token("Bearer") is None
    assert bearer_token("Basic dXNlcjpwYXNz") is None
    assert bearer_token("") is None
    assert bearer_token(None) is None


def test_read_key_value_dash_reads_stdin(monkeypatch):
    from plugins.llm_usage_proxy.cli import _read_key_value

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-from-stdin-987654\n"))
    assert _read_key_value("-") == "sk-from-stdin-987654"
    assert _read_key_value(" sk-from-argv ") == "sk-from-argv"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(ValueError):
        _read_key_value("-")


# ── 2. Auth injection on the request path ───────────────────────────────────


def test_manage_keys_replaces_bearer_with_stored_key(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=json.dumps({"model": "glm-5"}).encode(),
        headers={
            "Authorization": f"Bearer {CLIENT_KEY}",
            CALLER_TOKEN_HEADER: token,
        },
    )

    assert status == 200
    seen = _upstream_auth_headers(upstream)
    assert seen["authorization"] == f"Bearer {STORE_KEY_A}"
    assert CLIENT_KEY not in seen["authorization"]
    assert "x-api-key" not in seen  # the client's dialect is preserved, not duplicated


def test_manage_keys_replaces_x_api_key_with_stored_key(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={"X-Api-Key": CLIENT_KEY, CALLER_TOKEN_HEADER: token},
    )

    assert status == 200
    seen = _upstream_auth_headers(upstream)
    assert seen["x-api-key"] == STORE_KEY_A
    assert CLIENT_KEY not in json.dumps(seen)
    assert "authorization" not in seen


def test_manage_keys_injects_bearer_when_client_sends_no_credential(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )

    assert status == 200
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {STORE_KEY_A}"


def test_manage_keys_strips_the_caller_token_header(
    start_upstream, start_proxy, keys_file
):
    """The caller token is a credential for the proxy, never for the provider."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )

    assert CALLER_TOKEN_HEADER.lower() not in _upstream_auth_headers(upstream)


def test_route_without_stored_keys_keeps_passthrough(
    start_upstream, start_proxy, keys_file
):
    """Injection is per route: a route with no keys is not rewritten."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)
    # A second route over the same upstream, with no keys in the store.
    proxy.upstreams["kimi"] = proxy.upstreams["zai"]

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/kimi/chat/completions",
        body=b"{}",
        headers={"Authorization": f"Bearer {CLIENT_KEY}", CALLER_TOKEN_HEADER: token},
    )

    assert status == 200
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {CLIENT_KEY}"


def test_without_manage_keys_the_store_is_ignored(
    start_upstream, start_proxy, keys_file
):
    """No --manage-keys: today's passthrough, byte for byte, keys or not."""
    upstream = start_upstream(respond_json({"ok": True}))
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A])
    store.create_caller("alice")
    proxy = start_proxy(
        _zai(upstream), db_name="passthrough.sqlite", keys_path=keys_file
    )

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=json.dumps({"model": "glm-5"}).encode(),
        headers={"Authorization": f"Bearer {CLIENT_KEY}"},
    )

    assert status == 200
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {CLIENT_KEY}"
    assert STORE_KEY_A not in json.dumps(_upstream_auth_headers(upstream))


# ── 3. Caller validation and attribution ────────────────────────────────────


def test_caller_token_is_recorded_in_the_ledger(start_upstream, start_proxy, keys_file):
    upstream = start_upstream(
        respond_json({
            "model": "glm-5",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })
    )
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=json.dumps({"model": "glm-5"}).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )

    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "alice"
    assert rows[0]["total_tokens"] == 10
    # A caller token presented as a bearer credential is still swapped for the
    # route's provider key — it never travels upstream as itself.
    seen = _upstream_auth_headers(upstream)["authorization"]
    assert seen in (f"Bearer {STORE_KEY_A}", f"Bearer {STORE_KEY_B}")
    assert token not in seen


def test_caller_token_header_wins_over_a_bearer_credential(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    # The Authorization value is a *credential-shaped* string that is not a
    # caller token; the dedicated header is what names the caller.
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={"Authorization": f"Bearer {CLIENT_KEY}", CALLER_TOKEN_HEADER: token},
    )

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "alice"


def test_unknown_caller_token_is_refused_and_recorded(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, _ = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=json.dumps({"model": "glm-5"}).encode(),
        headers={"Authorization": f"Bearer {CLIENT_KEY}"},
    )

    assert status == 401
    assert json.loads(body)["error"] == "unknown caller token"
    # Nothing was sent upstream.
    assert upstream.requests == []
    # The attempt is still a ledger row — a refusal is not a silent gap.
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["outcome"] == "rejected"
    assert rows[0]["status_code"] == 401
    assert rows[0]["caller"] is None


def test_missing_caller_token_is_refused_when_tokens_are_configured(
    start_upstream, start_proxy, keys_file
):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, _ = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, _ = proxy_request(proxy.server_address[1], "GET", "/p/zai/models")

    assert status == 401
    assert upstream.requests == []


def test_non_ascii_token_is_a_401_not_a_crash(start_upstream, start_proxy, keys_file):
    """A header value is client input; comparing it must not raise."""
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, _ = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        # http.client encodes header values as latin-1, so this arrives as a
        # non-ASCII *str* — exactly what a naive str compare_digest rejects.
        headers={CALLER_TOKEN_HEADER: "tökén"},
    )

    assert status == 401
    assert json.loads(body)["error"] == "unknown caller token"
    assert upstream.requests == []


def test_no_caller_tokens_configured_accepts_labeled_traffic(
    start_upstream, start_proxy, keys_file
):
    """Compat: Hermes's in-process routing sends no caller token — its
    X-Usage-Caller label is what attributes the row while no tokens exist."""
    upstream = start_upstream(respond_json({"ok": True}))
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A])
    proxy = start_proxy(
        _zai(upstream),
        db_name="nocallers.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={
            "Authorization": f"Bearer {CLIENT_KEY}",
            CALLER_LABEL_HEADER: "hermes",
        },
    )

    assert status == 200
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "hermes"
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {STORE_KEY_A}"


# ── 4. Rotation: round-robin and one retry on 401/429 ───────────────────────


def test_requests_rotate_across_the_route_keys(start_upstream, start_proxy, keys_file):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)
    headers = {CALLER_TOKEN_HEADER: token}

    for _ in range(4):
        proxy_request(
            proxy.server_address[1],
            "POST",
            "/p/zai/chat/completions",
            body=b"{}",
            headers=headers,
        )

    seen = [req["headers"]["Authorization"] for req in upstream.requests]
    assert seen == [f"Bearer {STORE_KEY_A}", f"Bearer {STORE_KEY_B}"] * 2


@pytest.mark.parametrize("status", [401, 429])
def test_auth_failure_retries_once_with_the_next_key(
    start_upstream, start_proxy, keys_file, status
):
    """A revoked key is swapped for the route's next one within the request."""
    seen_keys: list[str] = []

    def respond(handler) -> None:
        auth = handler.headers.get("Authorization")
        seen_keys.append(auth)
        if auth == f"Bearer {STORE_KEY_A}":
            handler.send_response(status)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(200)
        body = json.dumps({"ok": True}).encode()
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    upstream = start_upstream(respond)
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    status_code, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )

    # The client never sees the failed key's answer.
    assert status_code == 200
    assert seen_keys == [f"Bearer {STORE_KEY_A}", f"Bearer {STORE_KEY_B}"]

    rows = wait_for_row_count(proxy.store.path, 1)
    assert len(rows) == 1  # one row per request, not per upstream attempt
    assert rows[0]["status_code"] == 200
    assert rows[0]["caller"] == "alice"


def test_auth_failure_with_one_key_is_returned_not_retried(
    start_upstream, start_proxy, keys_file
):
    """Nothing to rotate to: the upstream answer is what the client gets."""
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A])
    token, _ = store.create_caller("alice")

    def respond(handler) -> None:
        handler.send_response(401)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    upstream = start_upstream(respond)
    proxy = start_proxy(
        _zai(upstream),
        db_name="singlekey.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )

    assert status == 401
    assert len(upstream.requests) == 1


def test_retry_cap_is_one_per_request(start_upstream, start_proxy, keys_file):
    """Both keys dead: exactly one retry, then the failure is forwarded."""
    store = KeyStore(keys_file)
    store.set_route_keys("zai", [STORE_KEY_A, STORE_KEY_B])
    token, _ = store.create_caller("alice")
    seen: list[str] = []

    def respond(handler) -> None:
        seen.append(handler.headers.get("Authorization"))
        handler.send_response(401)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    upstream = start_upstream(respond)
    proxy = start_proxy(
        _zai(upstream),
        db_name="bothdead.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )

    assert status == 401
    assert len(seen) == 2  # one attempt plus the single allowed retry


# ── 5. Model pickers: /models is just another forwarded request ─────────────


@pytest.mark.parametrize("path", ["/p/zai/models", "/p/zai/v1/models"])
def test_models_endpoints_are_forwarded_with_the_stored_key(
    start_upstream, start_proxy, keys_file, path
):
    upstream = start_upstream(
        respond_json({"object": "list", "data": [{"id": "glm-5"}]})
    )
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    status, _, body = proxy_request(
        proxy.server_address[1], "GET", path, headers={CALLER_TOKEN_HEADER: token}
    )

    assert status == 200
    assert json.loads(body)["data"][0]["id"] == "glm-5"
    assert upstream.requests[0]["path"].endswith(path.removeprefix("/p/zai"))
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {STORE_KEY_A}"
    rows = wait_for_row_count(proxy.store.path, 1)
    assert rows[0]["caller"] == "alice"


def test_models_endpoints_forward_without_manage_keys(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"object": "list", "data": []}))
    proxy = start_proxy(_zai(upstream), db_name="models.sqlite")

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "GET",
        "/p/zai/models",
        headers={"Authorization": f"Bearer {CLIENT_KEY}"},
    )

    assert status == 200
    assert _upstream_auth_headers(upstream)["authorization"] == f"Bearer {CLIENT_KEY}"


# ── 6. Schema migration and per-caller reporting ────────────────────────────


def test_existing_db_gains_a_caller_column_with_old_rows_left_null(tmp_path):
    db = str(tmp_path / "legacy.sqlite")
    conn = sqlite3.connect(db)
    with conn:
        # A ledger written before key-manager mode: no `caller` column at all.
        conn.execute(
            "CREATE TABLE usage_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
            " upstream TEXT NOT NULL, model TEXT, prompt_tokens INTEGER,"
            " completion_tokens INTEGER, cached_tokens INTEGER,"
            " reasoning_tokens INTEGER, cache_creation_tokens INTEGER,"
            " total_tokens INTEGER, status_code INTEGER, latency_ms INTEGER,"
            " path TEXT, request_id TEXT, outcome TEXT, usage_complete TEXT)"
        )
        conn.execute(
            "INSERT INTO usage_events (ts, upstream, outcome) VALUES ('t', 'zai', 'completed')"
        )
    conn.close()

    from plugins.llm_usage_proxy.server import UsageStore

    store = UsageStore(db)
    try:
        names = [
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(usage_events)")
        ]
        assert "caller" in names
        store.insert(
            upstream="zai",
            model=None,
            usage=None,
            status_code=200,
            latency_ms=1,
            path="/",
            request_id=None,
            outcome="completed",
            usage_complete="missing",
            caller="alice",
        )
        rows = store.recent(10)
        legacy = [row for row in rows if row["id"] == 1]
        assert legacy and legacy[0]["caller"] is None  # history stays unknown
        attributed = [row for row in rows if row["id"] != 1]
        assert [row["caller"] for row in attributed] == ["alice"]
        assert rows[0]["id"] == 2  # the new row is the newest
    finally:
        store.close()


def test_summary_groups_by_caller(tmp_path):
    from plugins.llm_usage_proxy.server import UsageStore

    store = UsageStore(str(tmp_path / "summary.sqlite"))
    try:
        for caller in ("alice", "alice", "bob"):
            store.insert(
                upstream="zai",
                model=None,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                status_code=200,
                latency_ms=1,
                path="/",
                request_id=None,
                outcome="completed",
                usage_complete="final",
                caller=caller,
            )
        store.insert(
            upstream="zai",
            model=None,
            usage=None,
            status_code=401,
            latency_ms=1,
            path="/",
            request_id=None,
            outcome="rejected",
            usage_complete="missing",
        )

        summary = store.summary()
        assert summary["overall"]["requests"] == 4
        assert summary["overall"]["total_tokens"] == 45
        assert summary["by_caller"]["alice"]["requests"] == 2
        assert summary["by_caller"]["alice"]["total_tokens"] == 30
        assert summary["by_caller"]["bob"]["requests"] == 1
        assert summary["by_caller"]["(unattributed)"]["requests"] == 1
        assert summary["by_caller"]["(unattributed)"]["usage_missing"] == 1
    finally:
        store.close()


def test_usage_rows_expose_the_caller_column(start_upstream, start_proxy, keys_file):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy, _, token = _authorized_proxy(start_proxy, upstream, keys_file)

    proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={CALLER_TOKEN_HEADER: token},
    )
    wait_for_row_count(proxy.store.path, 1)

    status, _, body = proxy_request(proxy.server_address[1], "GET", "/usage?limit=10")
    assert status == 200
    rows = json.loads(body)["rows"]
    assert rows[0]["caller"] == "alice"
    # A caller name is not a secret, but it is not a key either.
    assert STORE_KEY_A not in body.decode()


# ── 7. /health advertises the mode so a stale unit is visible ───────────────


def test_health_reports_manage_keys(start_proxy, keys_file):
    KeyStore(keys_file).set_route_keys("zai", [STORE_KEY_A])
    managed = start_proxy(
        {"zai": "https://api.invalid.example/v4"},
        db_name="health-managed.sqlite",
        manage_keys=True,
        keys_path=keys_file,
    )
    plain = start_proxy(
        {"zai": "https://api.invalid.example/v4"},
        db_name="health-plain.sqlite",
        keys_path=keys_file,
    )

    managed_payload = json.loads(
        proxy_request(managed.server_address[1], "GET", "/health")[2]
    )
    plain_payload = json.loads(
        proxy_request(plain.server_address[1], "GET", "/health")[2]
    )

    assert managed_payload["manage_keys"] is True
    assert plain_payload["manage_keys"] is False


# ── 8. CLI: keys and callers without ever printing a secret ─────────────────


def _cli(argv):
    """Run the plugin's CLI exactly as `hermes llm_usage_proxy …` would."""
    import argparse

    from plugins.llm_usage_proxy.cli import llm_usage_proxy_command, register_cli

    parser = argparse.ArgumentParser()
    register_cli(
        parser.add_subparsers(dest="hermes_command").add_parser("llm_usage_proxy")
    )
    args = parser.parse_args(["llm_usage_proxy", *argv])
    return llm_usage_proxy_command(args)


def test_cli_keys_set_list_remove_round_trip(keys_file, monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.llm_usage_proxy.server.default_keys_path", lambda: keys_file
    )

    assert _cli(["keys", "set", "zai", "--key", STORE_KEY_A, "--key", STORE_KEY_B]) == 0
    assert _cli(["keys", "list"]) == 0

    out = capsys.readouterr().out
    assert STORE_KEY_A not in out and STORE_KEY_B not in out
    assert "zai" in out and key_fingerprint(STORE_KEY_A) in out
    assert "#1" in out and "#2" in out

    assert _cli(["keys", "remove", "zai", "--index", "1"]) == 0
    assert KeyStore(keys_file).route_keys("zai") == (STORE_KEY_B,)

    assert _cli(["keys", "remove", "zai", "--index", "9"]) == 1
    assert _cli(["keys", "remove", "zai"]) == 0
    assert KeyStore(keys_file).route_keys("zai") == ()


def test_cli_callers_create_prints_the_token_once(keys_file, monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.llm_usage_proxy.server.default_keys_path", lambda: keys_file
    )

    assert _cli(["callers", "create", "alice"]) == 0
    first = capsys.readouterr().out
    assert "alice" in first
    token = first.strip().splitlines()[1].strip()
    assert token and KeyStore(keys_file).caller_for_token(token) == "alice"

    assert _cli(["callers", "create", "alice"]) == 0
    second = capsys.readouterr().out
    rotated = second.strip().splitlines()[1].strip()
    assert rotated != token  # minting again rotates, and says so
    assert "replaced" in second

    assert _cli(["callers", "list"]) == 0
    listed = capsys.readouterr().out
    assert "alice" in listed
    assert token not in listed and rotated not in listed

    assert _cli(["callers", "remove", "alice"]) == 0
    assert KeyStore(keys_file).caller_count() == 0


def test_cli_rejects_bad_names_without_writing(keys_file, monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.llm_usage_proxy.server.default_keys_path", lambda: keys_file
    )

    assert _cli(["keys", "set", "Bad_Route", "--key", STORE_KEY_A]) == 2
    assert _cli(["keys", "set", "zai"]) == 2
    assert _cli(["callers", "create", "Bad Name"]) == 2

    assert KeyStore(keys_file).describe() == {"routes": {}, "callers": {}}


def test_cli_hint_points_at_manage_keys_when_off(keys_file, monkeypatch, capsys):
    monkeypatch.setattr(
        "plugins.llm_usage_proxy.server.default_keys_path", lambda: keys_file
    )

    _cli(["keys", "set", "zai", "--key", STORE_KEY_A])
    _cli(["keys", "list"])
    out = capsys.readouterr().out
    assert "manage-keys on" in out


def test_build_exec_start_argv_carries_manage_keys_but_no_secret(monkeypatch, tmp_path):
    from plugins.llm_usage_proxy import systemd
    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config

    monkeypatch.setattr(systemd, "profile_identity", lambda home=None: "ident")
    monkeypatch.setattr(systemd, "build_route_table", lambda cfg, environ=None: {})
    monkeypatch.setattr(
        "plugins.auto_update.platform.resolve_python_executable",
        lambda: "/usr/bin/python3",
    )

    cfg = dict(load_llm_usage_proxy_config({"manage_keys": True, "port": 8790}))
    argv = systemd.build_exec_start_argv(cfg, hermes_home=tmp_path)

    assert "--manage-keys" in argv
    assert "--keys-path" in argv
    assert str(systemd.keys_path(tmp_path)) in argv
    # No provider key or caller token is ever rendered into the unit.
    rendered = " ".join(argv)
    assert STORE_KEY_A not in rendered and CLIENT_KEY not in rendered

    plain = systemd.build_exec_start_argv(
        dict(load_llm_usage_proxy_config({"port": 8790})), hermes_home=tmp_path
    )
    assert "--manage-keys" not in plain
