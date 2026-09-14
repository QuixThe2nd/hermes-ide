from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

# psycopg is imported lazily inside load_secrets() so the plugin package
# (and its tool schema) stays importable on hosts without the driver
# installed — the check_fn gate hides the tool there anyway.

from .paths import secrets_path
_SECRET_KEYS = frozenset({"DISCORD_HISTORY_DATABASE_URL", "DISCORD_HISTORY_AUDIT_HMAC_KEY"})
_CONFIG_KEYS = frozenset({"owner_user_ids", "allowed_guild_ids", "allowed_channel_ids"})
_MAX_SECRET_BYTES = 64 * 1024
_cache: dict[tuple[str, int, int, int, int], "Secrets"] = {}


class ConfigError(RuntimeError):
    """A redacted, stable configuration failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Secrets:
    database_url: str = field(repr=False)
    audit_hmac_key: bytes = field(repr=False)


def _open_secret_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise ConfigError("invalid_secret_file") from exc


def load_secrets(path: str | os.PathLike[str] | None = None) -> Secrets:
    """Load the fixed secret file, with *path* available only as an explicit test override."""
    secret_path = secrets_path() if path is None else Path(path)
    fd = _open_secret_file(secret_path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise ConfigError("invalid_secret_file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError("invalid_secret_mode")
        if info.st_size > _MAX_SECRET_BYTES:
            raise ConfigError("secret_file_too_large")
        cache_key = (str(secret_path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached
        raw = os.read(fd, _MAX_SECRET_BYTES + 1)
    finally:
        os.close(fd)

    if b"\x00" in raw:
        raise ConfigError("nul_byte")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("invalid_encoding") from exc

    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError("malformed_line")
        key, value = line.split("=", 1)
        if key not in _SECRET_KEYS:
            raise ConfigError("unknown_key")
        if key in values:
            raise ConfigError("duplicate_key")
        if not value:
            raise ConfigError("missing_secret")
        values[key] = value
    if set(values) != _SECRET_KEYS:
        raise ConfigError("missing_secret")

    dsn = values["DISCORD_HISTORY_DATABASE_URL"]
    try:
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(dsn)
    except ImportError:
        raise ConfigError("psycopg_not_installed")
    except Exception as exc:
        raise ConfigError("invalid_database_url") from exc
    if not parsed.get("dbname"):
        raise ConfigError("invalid_database_url")
    try:
        audit_key = base64.b64decode(values["DISCORD_HISTORY_AUDIT_HMAC_KEY"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ConfigError("invalid_audit_hmac_key") from exc
    if len(audit_key) != 32:
        raise ConfigError("invalid_audit_hmac_key")

    result = Secrets(dsn, audit_key)
    _cache.clear()
    _cache[cache_key] = result
    return result


def _ids(value: Any, code: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(code)
    result = frozenset(value)
    if len(result) != len(value) or any(not isinstance(v, str) or not v.isascii() or not v.isdecimal() or not 17 <= len(v) <= 20 for v in result):
        raise ConfigError(code)
    return result


@dataclass(frozen=True)
class PluginConfig:
    owner_user_ids: frozenset[str]
    allowed_guild_ids: frozenset[str]
    allowed_channel_ids: Mapping[str, frozenset[str]]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PluginConfig":
        if not isinstance(value, Mapping):
            raise ConfigError("invalid_plugin_config")
        unknown = set(value) - _CONFIG_KEYS
        if unknown:
            raise ConfigError("unknown_config_key")
        if set(value) != _CONFIG_KEYS:
            raise ConfigError("missing_config_key")
        owners = _ids(value["owner_user_ids"], "invalid_owner_user_ids")
        guilds = _ids(value["allowed_guild_ids"], "invalid_allowed_guild_ids")
        channels_value = value["allowed_channel_ids"]
        if not isinstance(channels_value, Mapping) or set(channels_value) != set(guilds):
            raise ConfigError("invalid_allowed_channel_ids")
        channels = {guild: _ids(ids, "invalid_allowed_channel_ids") for guild, ids in channels_value.items()}
        return cls(owners, guilds, MappingProxyType(channels))

    def channels_for_guild(self, guild_id: str) -> frozenset[str]:
        return self.allowed_channel_ids.get(guild_id, frozenset())
