"""Anthropic Claude Pro/Max OAuth runtime credential resolution for the LLM proxy.

Split out of ``hermes_cli/auth.py``; origin helpers are imported lazily per function so
``hermes_cli.auth.<helper>`` patches still intercept and no cycle forms.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hermes_cli.auth_constants import AuthError

_RELOGIN = "Run `hermes auth add anthropic --type oauth`."


def _pick_oauth_entry(entries) -> Optional[Any]:
    """Prefer Hermes-owned PKCE grants over borrowed Claude Code rows."""
    from agent.credential_pool import AUTH_TYPE_OAUTH

    usable: List[Any] = []
    for entry in entries:
        if entry.auth_type != AUTH_TYPE_OAUTH:
            continue
        access = str(entry.access_token or "").strip()
        refresh = str(entry.refresh_token or "").strip()
        if access and refresh:
            usable.append(entry)
    if not usable:
        return None
    hermes_owned = [e for e in usable if str(e.source or "").endswith("hermes_pkce")]
    candidates = hermes_owned if hermes_owned else usable
    return min(candidates, key=lambda e: (int(e.priority), str(e.id)))


def resolve_anthropic_oauth_runtime_credentials(
    *, force_refresh: bool = False, refresh_if_expiring: bool = True
) -> Dict[str, Any]:
    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    entry = _pick_oauth_entry(pool.entries())
    if entry is None:
        raise AuthError(
            f"No Anthropic OAuth credentials stored. {_RELOGIN}",
            provider="anthropic",
            code="anthropic_auth_missing",
            relogin_required=True,
        )

    if force_refresh or (refresh_if_expiring and pool._entry_needs_refresh(entry)):
        refreshed = pool._refresh_entry(entry, force=force_refresh)
        if refreshed is None or not str(refreshed.access_token or "").strip():
            raise AuthError(
                f"Anthropic OAuth refresh failed. {_RELOGIN}",
                provider="anthropic",
                code="anthropic_refresh_failed",
                relogin_required=True,
            )
        entry = refreshed

    token = str(entry.access_token or "").strip()
    if not token:
        raise AuthError(
            f"Anthropic OAuth credentials are unusable. {_RELOGIN}",
            provider="anthropic",
            code="anthropic_auth_missing",
            relogin_required=True,
        )

    base_url = str(entry.base_url or "").strip() or "https://api.anthropic.com"
    return {
        "provider": "anthropic",
        "base_url": base_url,
        "api_key": token,
        "source": entry.source,
        "expires_at_ms": entry.expires_at_ms,
        "auth_mode": "oauth_pkce",
    }
