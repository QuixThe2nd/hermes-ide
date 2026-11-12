"""Context-local session identity and tool observability correlation IDs."""

import contextvars


def _ctx(name: str, default: "str | None" = "") -> contextvars.ContextVar:
    return contextvars.ContextVar(name, default=default)


# Context-local identity keeps concurrent gateway turns and their worker threads isolated.
_tool_session_key: contextvars.ContextVar[str] = _ctx("tool_session_key")
_observability_turn_id: contextvars.ContextVar[str] = _ctx("observability_turn_id")
_observability_tool_call_id: contextvars.ContextVar[str] = _ctx("observability_tool_call_id")
_observability_session_id: contextvars.ContextVar[str] = _ctx("observability_session_id")


def set_current_session_key(session_key: str) -> contextvars.Token[str]:
    return _tool_session_key.set(session_key or "")


def reset_current_session_key(token: contextvars.Token[str]) -> None:
    _tool_session_key.reset(token)


_Tokens = tuple[contextvars.Token[str], contextvars.Token[str], contextvars.Token[str]]


def set_current_observability_context(
    *, turn_id: str = "", tool_call_id: str = "", session_id: str = ""
) -> _Tokens:
    """Bind active tool correlation IDs for observers in this context."""
    return (
        _observability_turn_id.set(turn_id or ""),
        _observability_tool_call_id.set(tool_call_id or ""),
        _observability_session_id.set(session_id or ""),
    )


def reset_current_observability_context(tokens: _Tokens) -> None:
    turn_token, tool_token, session_token = tokens
    _observability_session_id.reset(session_token)
    _observability_tool_call_id.reset(tool_token)
    _observability_turn_id.reset(turn_token)


def get_current_session_key(default: str = "default") -> str:
    if session_key := _tool_session_key.get():
        return session_key
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_KEY", default)
