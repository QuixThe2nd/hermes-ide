"""Opt-in Claude Code system prefix for custom providers on codex_responses (e.g. LiteLLM → Anthropic OAuth plan)."""

from __future__ import annotations

from typing import Any, List, Optional

_TRUTHY = frozenset({"true", "yes", "1", "on"})


def _coerce_plan_identity_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in _TRUTHY:
        return True
    return False


def _entry_matches_provider(entry: dict, provider: str) -> bool:
    provider_norm = (provider or "").strip().lower()
    if not provider_norm or provider_norm == "auto":
        return True
    from hermes_cli.config import coerce_provider_id

    entry_keys = {
        coerce_provider_id(entry.get("provider_key")),
        coerce_provider_id(entry.get("name")),
    }
    entry_keys.discard("")
    if provider_norm.startswith("custom:"):
        slug = provider_norm.partition(":")[2].strip()
        return slug in entry_keys
    if provider_norm == "custom":
        return True
    return provider_norm in entry_keys


def resolve_claude_code_plan_identity(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
    custom_providers: Optional[List[Any]],
) -> bool:
    """True when config opts this runtime into the Claude Code plan identity prefix."""
    if (api_mode or "") != "codex_responses" or not (model or "").strip() or not (base_url or "").strip():
        return False
    from hermes_cli.config_providers import _entries_for_route, _route_model_cfg

    for entry in _entries_for_route(base_url, custom_providers, None):
        if not isinstance(entry, dict) or not _entry_matches_provider(entry, provider):
            continue
        if _coerce_plan_identity_flag(entry.get("claude_code_plan_identity")):
            return True
        model_cfg = _route_model_cfg(entry, model)
        if model_cfg is not None and _coerce_plan_identity_flag(model_cfg.get("claude_code_plan_identity")):
            return True
    return False


def split_claude_code_plan_identity_instructions(instructions: str) -> tuple[str, str]:
    """Return (wire instructions, relocated Hermes system text).

    Anthropic OAuth plan billing on Responses routes keys on ``instructions`` being
    *exactly* the Claude Code prefix — any suffix in that field misses plan tier.
    Hermes' real system prompt is relocated into the input stream instead.
    """
    from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX

    body = (instructions or "").strip()
    if body == _CLAUDE_CODE_SYSTEM_PREFIX:
        return _CLAUDE_CODE_SYSTEM_PREFIX, ""
    if body.startswith(_CLAUDE_CODE_SYSTEM_PREFIX):
        remainder = body[len(_CLAUDE_CODE_SYSTEM_PREFIX) :].lstrip("\n").strip()
        return _CLAUDE_CODE_SYSTEM_PREFIX, remainder
    return _CLAUDE_CODE_SYSTEM_PREFIX, body


def prepend_claude_code_plan_identity_to_instructions(instructions: str) -> str:
    """Legacy helper: exact-prefix wire value only (no Hermes suffix in ``instructions``)."""
    wire, _ = split_claude_code_plan_identity_instructions(instructions)
    return wire


def apply_claude_code_plan_identity_flag(agent, custom_providers: Optional[List[Any]] = None) -> None:
    providers = custom_providers
    if providers is None:
        providers = getattr(agent, "_custom_providers", None)
    agent._claude_code_plan_identity = resolve_claude_code_plan_identity(
        provider=getattr(agent, "provider", "") or "",
        model=getattr(agent, "model", "") or "",
        base_url=getattr(agent, "base_url", "") or "",
        api_mode=getattr(agent, "api_mode", "") or "",
        custom_providers=providers,
    )
