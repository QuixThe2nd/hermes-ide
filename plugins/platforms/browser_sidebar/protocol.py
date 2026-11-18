"""Shared wire-validation helpers for the browser sidebar transport."""

import re
from typing import Any

DEFAULT_PORT = 8796
LOCK_SCOPE = "browser_sidebar_token"

# RFC 7230 token (tchar): the subprotocol header cannot carry anything else.
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def is_valid_token(token: Any) -> bool:
    """Return whether *token* can be the sole RFC 7230 subprotocol token."""
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


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
            chat_id="brave",
            chat_type="dm",
            user_id="brave-sidebar",
            message_id=message_id or None,
            # The authenticated socket IS the user (token subprotocol).
            role_authorized=True,
        ),
        message_id=message_id or None,
    )
