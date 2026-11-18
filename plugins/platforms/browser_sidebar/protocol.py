"""Shared wire-validation helpers for the browser sidebar transport."""

import re
from typing import Any
DEFAULT_PORT = 8796
LOCK_SCOPE = "browser_sidebar_token"

# Chat id of the single pre-per-tab conversation. Older extension builds send
# no usable tab id, and every non-tab chat id (CLI probes, tests) maps here.
LEGACY_CHAT_ID = "brave"

_TAB_CHAT_ID_RE = re.compile(r"^tab-(\d+)$")

# RFC 7230 token (tchar): the subprotocol header cannot carry anything else.
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def is_valid_token(token: Any) -> bool:
    """Return whether *token* can be the sole RFC 7230 subprotocol token."""
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))



def tab_id_from_frame(frame: Any) -> int | None:
    """Best-effort integer tab id from a chat frame, else ``None``.

    Accepts an int or a digit-string (JSON round-trips are untyped); rejects
    booleans (``isinstance(True, int)``), floats, junk strings, and missing
    tabs so nothing but a real number ever forks a session.
    """
    if not isinstance(frame, dict):
        return None
    tab = frame.get("tab")
    if not isinstance(tab, dict):
        return None
    tab_id = tab.get("id")
    if isinstance(tab_id, bool):
        return None
    if isinstance(tab_id, int):
        return tab_id
    if isinstance(tab_id, str) and tab_id.isdigit():
        return int(tab_id)
    return None


def chat_id_for_frame(frame: Any) -> str:
    """Session chat id for a chat frame: per-tab, or the legacy conversation."""
    tab_id = tab_id_from_frame(frame)
    return f"tab-{tab_id}" if tab_id is not None else LEGACY_CHAT_ID


def tab_id_from_chat_id(chat_id: Any) -> int | None:
    """Inverse of ``chat_id_for_frame``: ``"tab-7"`` → ``7``, else ``None``."""
    if not isinstance(chat_id, str):
        return None
    match = _TAB_CHAT_ID_RE.match(chat_id)
    return int(match.group(1)) if match else None


def session_key_for_chat_id(base_key: str, chat_id: str) -> str:
    """Per-tab session key from the attach-shipped base key.

    Session keys end with the chat id (``<ns>:<platform>:dm:<chat_id>``), so
    the hub reuses the key the adapter attached with and rewrites only the
    last colon segment. A base key without a colon is passed through
    unchanged — unexpected shapes keep legacy single-key routing instead of
    being mangled.
    """
    if not isinstance(base_key, str) or ":" not in base_key:
        return base_key
    ns, _, _ = base_key.rpartition(":")
    return f"{ns}:{chat_id}"


def tab_id_for_session_key(session_key: Any) -> int | None:
    """Tab id for a session key's trailing chat-id segment, if it is a tab.

    ``run_tab_js`` resolves WHICH tab it belongs to from the turn's ambient
    session key; non-sidebar keys never end in ``tab-<id>`` and return
    ``None`` (the extension then falls back to the focused tab).
    """
    if not isinstance(session_key, str) or ":" not in session_key:
        return None
    return tab_id_from_chat_id(session_key.rpartition(":")[2])


def chat_frame_to_event(frame: Any, platform: Any = None):
    """Build the MessageEvent a chat frame stands for, hub-side or adapter-side.
    Shared so the hub's router and the adapter's local fallback produce the
    byte-identical session source; missing/untrusted tab fields degrade to
    "?" — never crash on page data. ``platform`` defaults to the dynamic
    browser_sidebar Platform member.
    """
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource

    tab = frame.get("tab") if isinstance(frame, dict) else None
    if not isinstance(tab, dict):
        tab = {}
    title = str(tab.get("title") or "?")
    url = str(tab.get("url") or "?")
    text = str(frame.get("text") or "")
    message_id = str(frame.get("id") or "")
    return MessageEvent(
        text=f"[browser tab: {title} — {url}]\n\n{text}",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=platform or Platform("browser_sidebar"),
            chat_id=chat_id_for_frame(frame),
            user_id="brave-sidebar",
            message_id=message_id or None,
            # The authenticated socket IS the user (token subprotocol).
            role_authorized=True,
        ),
        message_id=message_id or None,
    )
