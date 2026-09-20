"""A status snapshot observes the credential pool; it never leases (refreshes / rotates) an entry.

``get_codex_auth_status`` / ``get_xai_oauth_auth_status`` back every credential-gated listing
(``/model`` picker, ``hermes doctor``, dashboard cards). When that read ran ``pool.select()`` it
refreshed an expiring single-use token, and a *transient* failure of that speculative POST benched
the entry with a persisted cooldown — the picker then rendered the provider as unconfigured
("needs setup" / "0 models") while the runtime resolver kept serving the same credential.

Fixtures adapted from #114379 by @Finn763.
"""

import base64
import json
import time

from agent import credential_pool
from agent.credential_pool import load_pool
from hermes_cli.auth import AuthError, DEFAULT_CODEX_BASE_URL, get_codex_auth_status


def _jwt_with_exp(offset_seconds: int) -> str:
    def _b64(payload: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("utf-8")

    return f"{_b64({'alg': 'none'})}.{_b64({'exp': int(time.time()) + offset_seconds})}.sig"


def _pool_only_codex_home(tmp_path, monkeypatch, *, access_tokens: list):
    """HERMES_HOME whose only Codex credentials live in ``credential_pool.openai-codex``; the token
    endpoint is a transient failure (the credential itself is still good)."""
    import hermes_cli.auth as auth
    import hermes_cli.codex_models as codex_models

    home = tmp_path / "hermes"
    home.mkdir()
    entries = [
        {"id": f"codex-pool-entry-{i}", "label": f"device_code-{i}", "auth_type": "oauth", "source": "device_code",
         "priority": i, "request_count": 0, "access_token": token, "refresh_token": f"codex-refresh-token-{i}",
         "base_url": DEFAULT_CODEX_BASE_URL}
        for i, token in enumerate(access_tokens)
    ]
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {"openai-codex": entries}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-cli"))
    monkeypatch.setattr(codex_models, "_fetch_models_from_api", lambda access_token: [])
    refresh_calls: list = []

    def _transient_failure(access_token, refresh_token, *args, **kwargs):
        refresh_calls.append(refresh_token)
        raise AuthError("Codex token refresh failed with status 503.", provider="openai-codex",
                        code="codex_refresh_failed")

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _transient_failure)
    return home, refresh_calls


def _persisted_pool(home) -> list:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]


def test_status_snapshot_does_not_refresh_or_bench_an_expiring_pool_entry(tmp_path, monkeypatch):
    home, refresh_calls = _pool_only_codex_home(tmp_path, monkeypatch, access_tokens=[_jwt_with_exp(-3600)])

    status = get_codex_auth_status()

    assert refresh_calls == [], "a status read spent the single-use pool refresh token"
    assert status["logged_in"] is True, status
    assert [e.get("last_status") for e in _persisted_pool(home)] == [None]
    assert load_pool("openai-codex").has_available() is True

    from hermes_cli.model_switch import list_authenticated_providers

    rows = [r for r in list_authenticated_providers(current_provider="openai-codex", current_model="gpt-5.6-sol")
            if r["slug"] == "openai-codex"]
    assert rows and rows[0]["total_models"] > 0, rows

    # Control: the runtime lease still refreshes the same entry.
    load_pool("openai-codex").select()
    assert refresh_calls == ["codex-refresh-token-0"]


def test_status_snapshot_leaves_round_robin_order_and_counts_untouched(tmp_path, monkeypatch):
    home, _ = _pool_only_codex_home(
        tmp_path, monkeypatch, access_tokens=[_jwt_with_exp(3600), _jwt_with_exp(3600)])
    monkeypatch.setattr(credential_pool, "get_pool_strategy", lambda provider: credential_pool.STRATEGY_ROUND_ROBIN)
    before = _persisted_pool(home)

    assert get_codex_auth_status()["logged_in"] is True
    assert _persisted_pool(home) == before, "a status read rotated or re-counted the persisted pool"

    # Control: a runtime selection still rotates and persists the new order.
    load_pool("openai-codex").select()
    assert _persisted_pool(home) != before


def _singleton_only_codex_home(tmp_path, monkeypatch, *, tokens: dict, codex_cli_tokens: dict):
    """HERMES_HOME whose Codex credentials are the ``providers.openai-codex`` singleton only, with a
    valid Codex CLI login sitting beside it in ``CODEX_HOME``."""
    home, codex_home = tmp_path / "hermes", tmp_path / "codex"
    home.mkdir()
    codex_home.mkdir()
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "active_provider": "openai-codex",
        "providers": {"openai-codex": {"tokens": tokens, "auth_mode": "chatgpt"}}}), encoding="utf-8")
    (codex_home / "auth.json").write_text(json.dumps({"tokens": codex_cli_tokens}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return home


def _singleton_tokens(home) -> dict:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))["providers"]["openai-codex"]["tokens"]


def test_status_snapshot_never_adopts_codex_cli_tokens(tmp_path, monkeypatch):
    """#68004: a Hermes store missing its refresh_token is recovery-eligible on the runtime path, but
    ``hermes status`` / ``hermes doctor`` must not import the Codex CLI's single-use token family."""
    from hermes_cli.auth import resolve_codex_runtime_credentials

    stale = {"access_token": _jwt_with_exp(-60)}
    home = _singleton_only_codex_home(
        tmp_path, monkeypatch, tokens=stale,
        codex_cli_tokens={"access_token": _jwt_with_exp(86400), "refresh_token": "cli-refresh"})

    get_codex_auth_status()

    assert _singleton_tokens(home) == stale, "a status read persisted the Codex CLI login into auth.json"

    # Control: the runtime resolver still self-heals from the CLI file.
    assert resolve_codex_runtime_credentials()["source"] == "hermes-auth-store"
    assert _singleton_tokens(home)["refresh_token"] == "cli-refresh"


def test_status_snapshot_never_refreshes_an_expiring_singleton(tmp_path, monkeypatch):
    """#68004: an expiring singleton token is reported as stored; only the runtime lease may spend
    the refresh token (and ``read_only`` wins over ``force_refresh``)."""
    import hermes_cli.auth as auth
    from hermes_cli.auth import resolve_codex_runtime_credentials

    expiring = {"access_token": _jwt_with_exp(30), "refresh_token": "singleton-refresh"}
    home = _singleton_only_codex_home(
        tmp_path, monkeypatch, tokens=expiring, codex_cli_tokens={})
    refresh_calls: list = []

    def _rotate(access_token, refresh_token, *args, **kwargs):
        refresh_calls.append(refresh_token)
        return {"access_token": _jwt_with_exp(86400), "refresh_token": "rotated-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rotate)

    status = get_codex_auth_status()
    resolve_codex_runtime_credentials(force_refresh=True, read_only=True)

    assert refresh_calls == [], "a status read spent the single-use singleton refresh token"
    assert status["logged_in"] is True and status["api_key"] == expiring["access_token"]
    assert _singleton_tokens(home) == expiring

    # Control: the runtime path refreshes and persists the rotated pair.
    resolve_codex_runtime_credentials()
    assert refresh_calls == ["singleton-refresh"]
    assert _singleton_tokens(home)["refresh_token"] == "rotated-refresh"
