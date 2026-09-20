from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from discord_history.audit import (AuditError, DENIAL_MAX_BYTES,
                                   append_denial_event, safe_error_json)
from discord_history.auth import (AuthorizationError, authorize_bound_request,
                                  get_bound_gateway_principal,
                                  get_presented_gateway_identity)
from discord_history.config import ConfigError, PluginConfig, load_secrets


def _secret_file(tmp_path: Path, **changes: str) -> tuple[Path, bytes]:
    key = b"k" * 32
    values = {
        "DISCORD_HISTORY_DATABASE_URL": "postgresql://archive:pw@localhost/archive",
        "DISCORD_HISTORY_AUDIT_HMAC_KEY": base64.b64encode(key).decode(),
    }
    values.update(changes)
    path = tmp_path / "discord-history.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    path.chmod(0o600)
    return path, key


def test_load_secrets_strict_and_does_not_mutate_environment(tmp_path, monkeypatch):
    path, key = _secret_file(tmp_path)
    monkeypatch.setenv("DISCORD_HISTORY_DATABASE_URL", "spoofed")
    secrets = load_secrets(path)
    assert secrets.database_url.startswith("postgresql://")
    assert secrets.audit_hmac_key == key
    assert os.environ["DISCORD_HISTORY_DATABASE_URL"] == "spoofed"
    assert "pw" not in repr(secrets)


@pytest.mark.parametrize("body,code", [
    ("X=y\n", "unknown_key"),
    ("DISCORD_HISTORY_DATABASE_URL=x\nDISCORD_HISTORY_DATABASE_URL=y\n", "duplicate_key"),
    ("DISCORD_HISTORY_DATABASE_URL\n", "malformed_line"),
    ("DISCORD_HISTORY_DATABASE_URL=x\x00y\n", "nul_byte"),
])
def test_load_secrets_rejects_malformed_content(tmp_path, body, code):
    path = tmp_path / "secrets"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConfigError) as exc:
        load_secrets(path)
    assert exc.value.code == code
    assert str(exc.value) == code


def test_load_secrets_rejects_symlink_and_wrong_mode(tmp_path):
    target, _ = _secret_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ConfigError) as exc:
        load_secrets(link)
    assert exc.value.code == "invalid_secret_file"
    target.chmod(0o640)
    with pytest.raises(ConfigError) as exc:
        load_secrets(target)
    assert exc.value.code == "invalid_secret_mode"


def test_plugin_config_exact_keys_and_scopes():
    config = PluginConfig.from_mapping({
        "owner_user_ids": ["11111111111111111"],
        "allowed_guild_ids": ["22222222222222222"],
        "allowed_channel_ids": {"22222222222222222": ["33333333333333333"]},
    })
    assert config.channels_for_guild("22222222222222222") == frozenset({"33333333333333333"})
    with pytest.raises(ConfigError, match="unknown_config_key"):
        PluginConfig.from_mapping({"owner_user_ids": [], "allowed_guild_ids": [], "allowed_channel_ids": {}, "oops": 1})
    with pytest.raises(ConfigError, match="invalid_owner_user_ids"):
        PluginConfig.from_mapping({
            "owner_user_ids": ["١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦٧"],
            "allowed_guild_ids": ["22222222222222222"],
            "allowed_channel_ids": {"22222222222222222": ["33333333333333333"]},
        })


def _install_fake_gateway(monkeypatch, platform=object(), user=object(),
                          chat="33333333333333333", thread=""):
    import types
    sentinel = object()
    class Var:
        def __init__(self, value): self.value = value
        def get(self): return self.value
    module = types.ModuleType("gateway.session_context")
    module._UNSET = sentinel
    module._SESSION_PLATFORM = Var(sentinel if platform is _UNSET else platform)
    module._SESSION_USER_ID = Var(sentinel if user is _UNSET else user)
    module._SESSION_CHAT_ID = Var(chat)
    module._SESSION_THREAD_ID = Var(thread)
    gateway = types.ModuleType("gateway")
    gateway.session_context = module
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    return sentinel


_UNSET = object()


def test_bound_principal_ignores_environment_fallback(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "11111111111111111")
    _install_fake_gateway(monkeypatch, platform=_UNSET, user=_UNSET)
    with pytest.raises(AuthorizationError) as exc:
        get_bound_gateway_principal()
    assert exc.value.reason == "missing_context"


def test_authorization_rechecks_owner_and_scope(monkeypatch):
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111")
    config = PluginConfig.from_mapping({
        "owner_user_ids": ["11111111111111111"],
        "allowed_guild_ids": ["22222222222222222"],
        "allowed_channel_ids": {"22222222222222222": ["33333333333333333", "44444444444444444"]},
    })
    auth = authorize_bound_request(config, guild_id="22222222222222222", channel_ids=["33333333333333333"])
    assert auth.channel_ids == frozenset({"33333333333333333"})
    with pytest.raises(AuthorizationError, match="authorization_failed") as exc:
        authorize_bound_request(config, guild_id="22222222222222222", channel_ids=["99999999999999999"])
    assert exc.value.reason == "scope_denied"


def test_authorization_binds_configured_chat_and_current_child_thread(monkeypatch):
    config = PluginConfig.from_mapping({
        "owner_user_ids": ["11111111111111111"],
        "allowed_guild_ids": ["22222222222222222"],
        "allowed_channel_ids": {"22222222222222222": ["33333333333333333"]},
    })
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat="33333333333333333", thread="55555555555555555")
    child = authorize_bound_request(
        config, guild_id="22222222222222222",
        channel_ids=["55555555555555555"],
        resolve_thread=lambda _thread: {
            "id": "55555555555555555",
            "guild_id": "22222222222222222",
            "parent_id": "33333333333333333",
            "type": 11,
        },
    )
    assert child.channel_ids == frozenset({"55555555555555555"})

    # Discord's live gateway represents an existing thread with both chat_id
    # and thread_id set to the child thread.  The resolved Discord metadata,
    # rather than an assumed parent-shaped chat_id, must prove its root scope.
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat="55555555555555555", thread="55555555555555555")
    live_child = authorize_bound_request(
        config, guild_id="22222222222222222",
        channel_ids=["55555555555555555"],
        resolve_thread=lambda _thread: {
            "id": "55555555555555555",
            "guild_id": "22222222222222222",
            "parent_id": "33333333333333333",
            "type": 11,
        },
    )
    assert live_child.channel_ids == frozenset({"55555555555555555"})

    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat="66666666666666666", thread="")
    with pytest.raises(AuthorizationError) as exc:
        authorize_bound_request(config, guild_id="22222222222222222")
    assert exc.value.reason == "scope_denied"


def test_child_thread_requires_pre_database_parent_proof(monkeypatch):
    config = PluginConfig.from_mapping({
        "owner_user_ids": ["11111111111111111"],
        "allowed_guild_ids": ["22222222222222222"],
        "allowed_channel_ids": {"22222222222222222": ["33333333333333333"]},
    })
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat="33333333333333333", thread="55555555555555555")
    with pytest.raises(AuthorizationError) as missing:
        authorize_bound_request(config, guild_id="22222222222222222",
                                channel_ids=["55555555555555555"])
    assert missing.value.reason == "scope_denied"
    with pytest.raises(AuthorizationError) as wrong_parent:
        authorize_bound_request(
            config, guild_id="22222222222222222",
            channel_ids=["55555555555555555"],
            resolve_thread=lambda _thread: {
                "id": "55555555555555555",
                "guild_id": "22222222222222222",
                "parent_id": "99999999999999999",
                "type": 11,
            },
        )
    assert wrong_parent.value.reason == "scope_denied"


@pytest.mark.parametrize("chat,metadata,requested", [
    (
        "55555555555555555",
        {"id": "66666666666666666", "guild_id": "22222222222222222",
         "parent_id": "33333333333333333", "type": 11},
        ["55555555555555555"],
    ),
    (
        "55555555555555555",
        {"id": "55555555555555555", "guild_id": "77777777777777777",
         "parent_id": "33333333333333333", "type": 11},
        ["55555555555555555"],
    ),
    (
        "55555555555555555",
        {"id": "55555555555555555", "guild_id": "22222222222222222",
         "parent_id": None, "type": 11},
        ["55555555555555555"],
    ),
    (
        "55555555555555555",
        {"id": "55555555555555555", "guild_id": "22222222222222222",
         "parent_id": "33333333333333333", "type": 0},
        ["55555555555555555"],
    ),
    (
        "88888888888888888",
        {"id": "55555555555555555", "guild_id": "22222222222222222",
         "parent_id": "33333333333333333", "type": 11},
        ["55555555555555555"],
    ),
    (
        "55555555555555555",
        {"id": "55555555555555555", "guild_id": "22222222222222222",
         "parent_id": "33333333333333333", "type": 11},
        ["66666666666666666"],
    ),
])
def test_child_thread_denies_inconsistent_metadata_context_or_requested_scope(
    monkeypatch, chat, metadata, requested,
):
    config = PluginConfig.from_mapping({
        "owner_user_ids": ["11111111111111111"],
        "allowed_guild_ids": ["22222222222222222"],
        "allowed_channel_ids": {"22222222222222222": ["33333333333333333"]},
    })
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat=chat, thread="55555555555555555")

    with pytest.raises(AuthorizationError) as exc:
        authorize_bound_request(
            config, guild_id="22222222222222222", channel_ids=requested,
            resolve_thread=lambda _thread: metadata,
        )

    assert exc.value.reason == "scope_denied"


@pytest.mark.parametrize("chat,thread", [
    ("", ""),
    ("not-a-snowflake", ""),
    ("33333333333333333", "not-a-snowflake"),
])
def test_bound_principal_rejects_empty_or_malformed_channel_context(monkeypatch, chat, thread):
    _install_fake_gateway(monkeypatch, platform="discord", user="11111111111111111",
                          chat=chat, thread=thread)
    with pytest.raises(AuthorizationError) as exc:
        get_bound_gateway_principal()
    assert exc.value.reason == "missing_context"


def test_presented_identity_survives_wrong_platform_for_hmac_audit(monkeypatch):
    _install_fake_gateway(monkeypatch, platform="telegram", user="11111111111111111",
                          chat="33333333333333333", thread="")
    assert get_presented_gateway_identity() == (True, "11111111111111111")
    with pytest.raises(AuthorizationError) as exc:
        get_bound_gateway_principal()
    assert exc.value.reason == "wrong_platform"


def test_denial_audit_is_redacted_hmac_jsonl_and_mode_0600(tmp_path):
    path = tmp_path / "logs" / "access-denied.jsonl"
    key = b"a" * 32
    append_denial_event("not_owner", platform_present=True, presented_user_id="12345678901234567", audit_hmac_key=key, path=path)
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["reason"] == "not_owner"
    assert event["user_id_hmac"] == hmac.new(key, b"12345678901234567", hashlib.sha256).hexdigest()
    assert "12345678901234567" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_denial_audit_rotates_before_bound(tmp_path, monkeypatch):
    path = tmp_path / "access-denied.jsonl"
    path.write_bytes(b"x" * 100)
    path.chmod(0o600)
    monkeypatch.setattr("discord_history.audit.DENIAL_MAX_BYTES", 100)
    append_denial_event("denied", platform_present=False, presented_user_id=None, audit_hmac_key=b"z" * 32, path=path)
    assert (tmp_path / "access-denied.jsonl.1").stat().st_size == 100
    assert path.stat().st_size < 100
    lock = tmp_path / "access-denied.jsonl.lock"
    assert lock.is_file() and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_safe_error_json_is_generic():
    assert json.loads(safe_error_json()) == {"error": "authorization_failed"}


def test_denial_audit_rejects_bad_key_mode_size_and_partial_write(tmp_path, monkeypatch, capsys):
    path = tmp_path / "access-denied.jsonl"
    with pytest.raises(AuditError):
        append_denial_event("denied", platform_present=False, presented_user_id=None,
                            audit_hmac_key=b"short", path=path)

    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(AuditError):
        append_denial_event("denied", platform_present=False, presented_user_id=None,
                            audit_hmac_key=b"k" * 32, path=path)
    assert "denial audit failed" in capsys.readouterr().err

    path.chmod(0o600)
    monkeypatch.setattr("discord_history.audit.DENIAL_MAX_BYTES", 1)
    with pytest.raises(AuditError):
        append_denial_event("denied", platform_present=False, presented_user_id=None,
                            audit_hmac_key=b"k" * 32, path=path)
    monkeypatch.setattr("discord_history.audit.DENIAL_MAX_BYTES", DENIAL_MAX_BYTES)
    monkeypatch.setattr("discord_history.audit.os.write", lambda _fd, _payload: 0)
    with pytest.raises(AuditError):
        append_denial_event("denied", platform_present=False, presented_user_id=None,
                            audit_hmac_key=b"k" * 32, path=path)


def test_denial_rotation_advances_archives_and_tolerates_unlock_error(tmp_path, monkeypatch):
    path = tmp_path / "access-denied.jsonl"
    path.write_bytes(b"a" * 100); path.chmod(0o600)
    first = tmp_path / "access-denied.jsonl.1"
    first.write_bytes(b"previous"); first.chmod(0o600)
    monkeypatch.setattr("discord_history.audit.DENIAL_MAX_BYTES", 100)
    real_flock = __import__("fcntl").flock
    def flock(fd, operation):
        if operation == __import__("fcntl").LOCK_UN:
            raise OSError("unlock failed")
        return real_flock(fd, operation)
    monkeypatch.setattr("discord_history.audit.fcntl.flock", flock)
    append_denial_event("x", platform_present=False, presented_user_id=None,
                        audit_hmac_key=b"k" * 32, path=path)
    assert (tmp_path / "access-denied.jsonl.2").read_bytes() == b"previous"
    assert (tmp_path / "access-denied.jsonl.1").read_bytes() == b"a" * 100
