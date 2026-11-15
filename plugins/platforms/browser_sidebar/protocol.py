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
