"""Shared-owner mode: routing through a central proxy this profile does not own.

A profile with ``llm_usage_proxy.owner_endpoint`` set installs and manages **no**
proxy service of its own. Instead it names one central proxy — endpoint *and*
expected identity digest, both explicit in this profile's own config, never
inherited from another profile — verifies that listener's ``/health`` (service
id, protocol version, identity, key-manager mode), adopts the route table the
owner reports, and sends every routed model call there. Provider credentials
never enter this profile: clients present only the profile's own *caller token*
(``HERMES_USAGE_PROXY_CALLER_TOKEN`` in this profile's ``.env``), which the
owner swaps for the real upstream credential — a stored static key, or an
OAuth token the owner alone obtains and refreshes through the canonical auth
store (see ``hermes_cli.llm_proxy_oauth``).

With ``enforce: true`` the routing seam fails closed: inference that cannot be
served through the verified owner errors clearly instead of going direct.

Kept free of heavy imports at module load (config values only); network and
Hermes-side imports happen inside the functions that need them.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from plugins.llm_usage_proxy.config import (
    CONFIG_SECTION,
    load_llm_usage_proxy_config,
)

# Profile .env variable holding this profile's caller token for the shared
# owner proxy. A secret, so it lives in .env (never config.yaml); each profile
# holds only its own token — there is no cross-profile inheritance.
CALLER_TOKEN_ENV = "HERMES_USAGE_PROXY_CALLER_TOKEN"


def owner_policy(raw: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The shared-owner policy for this profile, or None in per-profile mode.

    Returns ``{"endpoint", "identity", "enforce"}``. ``identity`` may be empty
    only in the sense of "misconfigured": callers treat a policy without an
    identity as unverifiable and stand routing down (fail closed), because a
    shared owner you cannot name is not an owner at all.
    """
    if raw is None:
        cfg = load_llm_usage_proxy_config()
    else:
        cfg = load_llm_usage_proxy_config(raw)
    endpoint = str(cfg.get("owner_endpoint") or "").strip().rstrip("/")
    if not endpoint:
        return None
    return {
        "endpoint": endpoint,
        "identity": str(cfg.get("owner_identity") or "").strip(),
        "enforce": bool(cfg.get("enforce")),
    }


def owner_endpoint_parts(endpoint: str) -> Optional[tuple[str, int]]:
    """``(host, port)`` for an owner endpoint, or None when it is unusable.

    The shared owner is held to the same posture as a per-profile proxy:
    loopback only, explicit port, no path/query/fragment — anything else is
    not an owner this profile may trust with caller tokens.
    """
    split = urlsplit(str(endpoint or "").strip())
    host = (split.hostname or "").lower()
    if host not in ("127.0.0.1", "::1", "localhost"):
        return None
    try:
        port = split.port
    except ValueError:
        return None
    if port is None:
        return None
    if split.path.rstrip("/") or split.query or split.fragment:
        return None
    if split.scheme not in ("http", "https"):
        return None
    return host, int(port)


def caller_token() -> str:
    """This profile's caller token for the shared owner (scope-aware, or "").

    Read through the secret scope so that under the multiplexed gateway the
    value comes from *this* profile's ``.env`` — never from a sibling
    profile's environment (fail-closed scoping is the existing behavior of
    ``agent.secret_scope.get_secret_str``).
    """
    try:
        from agent.secret_scope import get_secret_str

        return str(get_secret_str(CALLER_TOKEN_ENV, "") or "").strip()
    except Exception:
        return ""


def verify_owner(
    endpoint: str,
    *,
    expect_identity: str,
    timeout: float = 2.0,
) -> tuple[Optional[dict[str, Any]], str]:
    """Verified ``/health`` payload of the shared owner, or ``(None, detail)``.

    Verified means: reachable, this service's id and protocol version, exactly
    the configured identity digest, and key-manager mode on (a shared owner
    that does not manage keys cannot honor caller tokens, so routing through
    it would silently pass them upstream).
    """
    from plugins.llm_usage_proxy.probe import _health_payload, _identity_mismatch_reason

    parts = owner_endpoint_parts(endpoint)
    if parts is None:
        return None, f"owner endpoint {endpoint!r} is not a loopback origin with a port"
    host, port = parts
    payload, detail = _health_payload(port, host=host, timeout=timeout)
    if payload is None:
        return None, detail
    mismatch = _identity_mismatch_reason(payload, expect_identity=expect_identity)
    if mismatch:
        return None, mismatch
    if not payload.get("manage_keys"):
        return None, "owner proxy is not running with key-manager mode"
    return payload, ""


def describe_policy(policy: Mapping[str, Any]) -> str:
    """One human-readable status line for a policy mapping (no secrets)."""
    enforce = "enforced (fail-closed)" if policy.get("enforce") else "fail-open"
    return f"{policy.get('endpoint', '')} identity={policy.get('identity', '')} ({enforce})"


__all__ = [
    "CALLER_TOKEN_ENV",
    "CONFIG_SECTION",
    "caller_token",
    "describe_policy",
    "owner_endpoint_parts",
    "owner_policy",
    "verify_owner",
]
