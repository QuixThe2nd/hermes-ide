"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


# ── Retired Ox Alpha preview route ─────────────────────────────────────
# openrouter/stealth/ox-alpha was a one-week experiment whose server-side
# preview has ended: the route still resolves but every request now fails.
# The v41 config migration scrubs it from config.yaml, and the
# session-resume paths consult the same identity so a route persisted while
# it was live cannot be restored over the migrated config. One source of
# truth for both — the exact route, nothing looser.
RETIRED_OX_ALPHA_PROVIDER = "openrouter"
RETIRED_OX_ALPHA_MODEL = "stealth/ox-alpha"


def is_retired_ox_alpha_route(
    provider: object, model: object, base_url: object = ""
) -> bool:
    """True only for the exact retired ``openrouter/stealth/ox-alpha`` route.

    Provider and model are case/whitespace normalized. An explicit
    ``openrouter`` provider always names the retired route. A bare or
    ``auto`` provider infers OpenRouter for this vendor-namespaced model id
    — unless a custom ``base_url`` is configured, which the runtime resolves
    as a custom endpoint before any OpenRouter inference (mirroring the
    resolve_runtime_provider host gate); only a base_url ON openrouter.ai
    keeps the inferred route. Any other named provider serving the same
    model id is a different, still-valid route and never matches.
    """
    if str(model or "").strip().lower() != RETIRED_OX_ALPHA_MODEL:
        return False
    normalized = str(provider or "").strip().lower()
    if normalized == RETIRED_OX_ALPHA_PROVIDER:
        return True
    if normalized not in ("", "auto"):
        return False
    url = str(base_url or "").strip()
    if not url:
        return True
    from utils import base_url_host_matches

    return base_url_host_matches(url, "openrouter.ai")


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain
