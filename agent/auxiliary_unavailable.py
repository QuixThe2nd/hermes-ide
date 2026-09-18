"""Why an auxiliary client could not be built — and the Nous credential failure behind it.

``_resolve_nous_runtime_api`` must swallow the resolver's ``AuthError`` and return None so the
ladder can still fall back (stored token, fallback_chain). Swallowing it at DEBUG left the goal
loop reporting ``judge error: RuntimeError`` while the real cause was an ``invalid_grant`` refresh
(#42177). This module remembers the latest failure so the ladder's raise and the goal judge can
name it, and warns ONCE per distinct message so operators see it without debug logging.
"""

import contextlib
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class AuxiliaryClientUnavailable(RuntimeError):
    """No auxiliary client could be built for the task (missing credentials / provider)."""


_lock = threading.Lock()
_last_nous_detail: Optional[str] = None
_warned_nous_details: set[str] = set()


def _quarantined_nous_error(exc: BaseException) -> BaseException:
    """Prefer the persisted terminal quarantine marker over *exc*.

    The pool rung swallows the real ``invalid_grant`` and wipes the dead tokens, so by the time the
    auth-store resolver runs it can only say "No access token found"; the marker it wrote still
    carries the message and code the user needs (#42177).
    """
    from hermes_cli.auth import AuthError, get_provider_auth_state
    from hermes_cli.auth_nous import _terminal_quarantine_marker

    with contextlib.suppress(Exception):
        marker = _terminal_quarantine_marker(get_provider_auth_state("nous") or {})
        if marker and marker.get("message"):
            return AuthError(str(marker["message"]).rstrip(".") + ".", provider="nous", code=marker.get("code"),
                             relogin_required=True)
    return exc


def record_nous_credential_failure(exc: BaseException) -> str:
    """Remember *exc* as the latest Nous credential failure; WARN the first time each message is seen."""
    from hermes_cli.auth import format_auth_error

    exc = _quarantined_nous_error(exc)
    message = format_auth_error(exc) if isinstance(exc, Exception) else str(exc)
    code = getattr(exc, "code", None)
    if code and str(code) not in message:
        message = f"{message} (code: {code})"
    detail = f"Nous Portal runtime credentials unavailable: {message.strip() or type(exc).__name__}"
    global _last_nous_detail
    with _lock:
        _last_nous_detail = detail
        first_time = detail not in _warned_nous_details
        _warned_nous_details.add(detail)
    if first_time:
        logger.warning("Auxiliary Nous client unavailable: %s", detail)
    return detail


def clear_nous_credential_failure() -> None:
    global _last_nous_detail
    with _lock:
        _last_nous_detail = None


def nous_credential_failure_detail() -> Optional[str]:
    """The latest recorded Nous credential failure, or None when the last resolution succeeded."""
    with _lock:
        return _last_nous_detail
