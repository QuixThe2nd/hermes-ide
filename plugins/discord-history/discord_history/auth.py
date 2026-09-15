from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Mapping

from .config import PluginConfig


class AuthorizationError(RuntimeError):
    """Internal reason is audit-only; callers always see a generic error."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("authorization_failed")


@dataclass(frozen=True)
class GatewayPrincipal:
    platform: str
    user_id: str
    chat_id: str
    thread_id: str


@dataclass(frozen=True)
class AuthorizedScope:
    principal: GatewayPrincipal
    guild_id: str
    channel_ids: frozenset[str]
    root_channel_ids: frozenset[str] = frozenset()


_SNOWFLAKE = re.compile(r"[0-9]{17,20}\Z")


def _raw_gateway_context() -> tuple[tuple[Any, Any, Any, Any], Any]:
    try:
        from gateway import session_context
        unset = session_context._UNSET
        raw = (
            session_context._SESSION_PLATFORM.get(),
            session_context._SESSION_USER_ID.get(),
            session_context._SESSION_CHAT_ID.get(),
            session_context._SESSION_THREAD_ID.get(),
        )
    except (ImportError, AttributeError) as exc:
        raise AuthorizationError("missing_context") from exc
    return raw, unset


def get_presented_gateway_identity() -> tuple[bool, str | None]:
    """Return denial-audit inputs only; these values never authorize anything."""
    try:
        raw, unset = _raw_gateway_context()
    except AuthorizationError:
        return False, None
    platform, user_id, _chat_id, _thread_id = raw
    platform_present = platform is not unset and isinstance(platform, str) and bool(platform.strip())
    presented_user = None
    if user_id is not unset and isinstance(user_id, str) and user_id.strip():
        presented_user = user_id.strip()
    return platform_present, presented_user


def get_bound_gateway_principal() -> GatewayPrincipal:
    """Read Hermes' private task-local variables directly, never its env-fallback helper."""
    raw, unset = _raw_gateway_context()
    if any(value is unset for value in raw):
        raise AuthorizationError("missing_context")
    if any(not isinstance(value, str) for value in raw):
        raise AuthorizationError("missing_context")
    platform, user_id, chat_id, thread_id = raw
    platform, user_id, chat_id, thread_id = (value.strip() for value in raw)
    if not platform or not user_id or not chat_id:
        raise AuthorizationError("missing_context")
    if platform != "discord":
        raise AuthorizationError("wrong_platform")
    if not _SNOWFLAKE.fullmatch(user_id) or not _SNOWFLAKE.fullmatch(chat_id):
        raise AuthorizationError("missing_context")
    if thread_id and not _SNOWFLAKE.fullmatch(thread_id):
        raise AuthorizationError("missing_context")
    return GatewayPrincipal(platform, user_id, chat_id, thread_id)


def authorize_bound_request(
    config: PluginConfig,
    *,
    guild_id: str,
    channel_ids: Iterable[str] | None = None,
    resolve_thread: Callable[[str], Mapping[str, Any]] | None = None,
) -> AuthorizedScope:
    """Repeat principal and scope authorization at handler runtime on every call."""
    principal = get_bound_gateway_principal()
    if principal.user_id not in config.owner_user_ids:
        raise AuthorizationError("not_owner")
    if guild_id not in config.allowed_guild_ids:
        raise AuthorizationError("scope_denied")
    allowed = config.channels_for_guild(guild_id)
    direct = set(allowed)
    if principal.thread_id:
        if resolve_thread is None:
            raise AuthorizationError("scope_denied")
        try:
            metadata = resolve_thread(principal.thread_id)
            thread_type = int(metadata.get("type", -1))
            parent_id = str(metadata.get("parent_id") or "")
        except Exception as exc:
            raise AuthorizationError("scope_denied") from exc
        if (str(metadata.get("id", "")) != principal.thread_id
                or str(metadata.get("guild_id", "")) != guild_id
                or parent_id not in allowed
                or principal.chat_id not in {principal.thread_id, parent_id}
                or thread_type not in {10, 11, 12}):
            raise AuthorizationError("scope_denied")
        direct.add(principal.thread_id)
    elif principal.chat_id not in allowed:
        raise AuthorizationError("scope_denied")
    requested = allowed if channel_ids is None else frozenset(channel_ids)
    if not requested or not requested.issubset(direct):
        raise AuthorizationError("scope_denied")
    # Configured IDs are roots. Child threads are proven against Discord before
    # any database connector is constructed, so new threads remain usable
    # without turning PostgreSQL into part of the authorization boundary.
    return AuthorizedScope(principal, guild_id, requested, allowed)
