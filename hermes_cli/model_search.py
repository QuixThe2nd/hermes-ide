"""Picker-only search aliases for model ids."""

from __future__ import annotations

# Lowercased wire id → extra tokens appended to the search haystack only.
# Brand the retired Ox Alpha preview nowhere: opaque provider ids (e.g.
# OpenCode Zen's ``x-preview-f-free``) stay searchable by their own id and
# fully usable when the provider returns them or the user types them.
_MODEL_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "k3": ("kimi-k3", "kimi"),
}

# Lowercased wire id → canonical public slug (the FIRST alias by convention), so picker dedup doesn't
# render a live bare id and its curated slug (``k3`` / ``kimi-k3``) as two rows.
_MODEL_ALIAS_CANONICAL: dict[str, str] = {
    wire_id: aliases[0].lower() for wire_id, aliases in _MODEL_SEARCH_ALIASES.items() if aliases}


def model_alias_canonical(model: str) -> str:
    """Return the canonical public slug for a bare wire-id alias."""
    key = (model or "").strip().lower()
    return _MODEL_ALIAS_CANONICAL.get(key, key)


def model_search_text(model: str) -> str:
    """Haystack for fuzzy/substring model search; never changes the wire id sent to the provider."""
    mid = (model or "").strip()
    if not mid:
        return model or ""
    aliases = _MODEL_SEARCH_ALIASES.get(mid.lower())
    return f"{mid} {' '.join(aliases)}" if aliases else mid
