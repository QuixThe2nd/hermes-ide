"""Session identity, tool observability, and unattended-session predicates."""

import contextvars
import logging
import os

from utils import is_truthy_value

logger = logging.getLogger("tools.approval")


def _ctx(name: str, default: "str | None" = "") -> contextvars.ContextVar:
    return contextvars.ContextVar(name, default=default)


# Context-local identity keeps concurrent gateway turns and their worker threads isolated.
_approval_session_key: contextvars.ContextVar[str] = _ctx("approval_session_key")
_approval_turn_id: contextvars.ContextVar[str] = _ctx("approval_turn_id")
_approval_tool_call_id: contextvars.ContextVar[str] = _ctx("approval_tool_call_id")
_approval_session_id: contextvars.ContextVar[str] = _ctx("approval_session_id")


def set_current_session_key(session_key: str) -> contextvars.Token[str]:
    return _approval_session_key.set(session_key or "")


def reset_current_session_key(token: contextvars.Token[str]) -> None:
    _approval_session_key.reset(token)


_Tokens = tuple[contextvars.Token[str], contextvars.Token[str], contextvars.Token[str]]


def set_current_observability_context(
    *, turn_id: str = "", tool_call_id: str = "", session_id: str = ""
) -> _Tokens:
    """Bind active tool correlation IDs for observers in this context."""
    return (
        _approval_turn_id.set(turn_id or ""),
        _approval_tool_call_id.set(tool_call_id or ""),
        _approval_session_id.set(session_id or ""),
    )


def reset_current_observability_context(tokens: _Tokens) -> None:
    turn_token, tool_token, session_token = tokens
    _approval_session_id.reset(session_token)
    _approval_tool_call_id.reset(tool_token)
    _approval_turn_id.reset(turn_token)


def get_current_session_key(default: str = "default") -> str:
    if session_key := _approval_session_key.get():
        return session_key
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_KEY", default)


def _session_env(name: str) -> str:
    """Read a session-scoped marker without leaking another profile's environment."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:
        return os.getenv(name, "") or ""


def _get_session_platform() -> str:
    return _session_env("HERMES_SESSION_PLATFORM")


def _is_cron_session() -> bool:
    return is_truthy_value(_session_env("HERMES_CRON_SESSION"))


_UNATTENDED_SESSION_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})


def _is_unattended_platform_session() -> bool:
    """Return whether the active platform has no interactive prompt surface."""
    return _get_session_platform() in _UNATTENDED_SESSION_PLATFORMS


def _is_single_query_session() -> bool:
    return is_truthy_value(_session_env("HERMES_SINGLE_QUERY_SESSION"))


def _get_approval_config() -> dict:
    """Read the live config block containing the retained ``deny`` rules."""
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly().get("approvals", {}) or {}
    except Exception as exc:
        logger.warning("Failed to load command deny rules: %s", exc)
        return {}
