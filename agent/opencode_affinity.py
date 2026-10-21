"""``x-opencode-session`` — OpenCode relay session-affinity header.

OpenCode (opencode.ai Zen/Go relay) pins requests that share an
``x-opencode-session`` value to the same upstream backend, which is what
keeps its prompt cache warm across the turns of one conversation. The value
only has to be opaque and consistent per conversation, so it is derived the
same way as the other conversation-affinity hints Hermes already sends
(OpenRouter's sticky ``session_id``, xAI's ``x-grok-conv-id``): the
host-declared routing scope first, then the ambient conversation root, then
the physical session id — normalized through ``_cache_scope_from_session_id``
so cron fires of one job share a scope.

Every OpenCode request — main turn on any transport, auxiliary calls
(compression, titles, vision, MoA) — goes through :func:`opencode_session_headers`
so the header cannot drift per code path. :func:`opencode_transport` is the
matching per-model wire-format decision for the auxiliary client.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

OPENCODE_SESSION_HEADER = "x-opencode-session"


def opencode_transport(provider: Optional[str], model: Optional[str], base_url: Optional[str]) -> tuple[Optional[str], str]:
    """``(api_mode, base_url)`` re-derived per model for an OpenCode relay target; ``(None, base_url)`` otherwise.

    OpenCode Zen/Go serve Responses-only (``gpt-*``, ``grok-*``, ``muse-spark``), Anthropic-wire
    (``minimax-*``, ``qwen*``, ``claude-*``) and chat/completions models behind one provider, so a
    provider-level or persisted ``api_mode`` is wrong for every model but the one it was saved for.
    The main runtime (``hermes_cli/runtime_provider.py``) always re-derives from the effective model;
    auxiliary resolution must agree or ``gpt-5.6-luna`` compression 500s on /chat/completions (#98799).
    Built-in families, custom entries named after one (``opencode-go-bridge``, #85589) and opencode.ai
    hosts all count.
    """
    from hermes_cli.models import normalize_opencode_base_url, normalize_opencode_model_id, opencode_model_api_mode
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider, _opencode_family_for_custom

    url = str(base_url or "")
    family = _opencode_family_for_custom(str(provider or ""), url)
    if family is None:
        return None, url
    # A custom entry that declares its own api_mode keeps it, exactly like the main runtime
    # (_resolve_named_custom_runtime only re-derives when the entry has none).
    if (_get_named_custom_provider(str(provider or "")) or {}).get("api_mode"):
        return None, url
    # ``<provider>/<model>`` config ids are stripped against the entry name before the family lookup.
    api_mode = opencode_model_api_mode(family, normalize_opencode_model_id(provider, model))
    return api_mode, normalize_opencode_base_url(provider, api_mode, url)


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    """True when *provider* or *base_url* addresses the OpenCode relay.

    Matches the built-in opencode-zen/go providers, custom
    ``opencode-<family>-*`` providers, and any base_url hosted on opencode.ai.
    """
    try:
        from hermes_cli.models import opencode_provider_family

        if opencode_provider_family(provider) is not None:
            return True
    except Exception:
        pass
    try:
        from agent.anthropic_endpoints import _is_opencode_endpoint

        return _is_opencode_endpoint(str(base_url or ""))
    except Exception:
        return False


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{"x-opencode-session": <key>}`` for OpenCode targets, else ``{}``."""
    if not is_opencode_target(provider, base_url):
        return {}
    try:
        from agent.portal_tags import get_affinity_scope, get_conversation_context
        from agent.transports.codex import _cache_scope_from_session_id

        key = _cache_scope_from_session_id(
            # Top-level session_id → OpenRouter's sticky routing key. Per their prompt-caching docs it is
            # used directly as the routing key instead of hashing the opening messages, and it activates
            # stickiness on the first successful request rather than only after a cache hit. Resolve it from
            # the declared routing scope first (set only by a host that names its own conversation, #96811),
            # then the ambient conversation contextvar, with the explicit argument as fallback. The gap this
            # closes is the auxiliary call sites — compression, title generation, vision, web_extract,
            # session_search, MoA slots — which funnel through ``agent.auxiliary_client``. That module has
            # no session handle and passes no ``session_id``, so those calls sent NO sticky key at all and
            # each routed independently of the conversation it belonged to (#70820). Mirrors the Nous Portal
            # profile, which resolves the same way (f2f4df064d). The ambient value is the session-lineage
            # ROOT, so it also stays stable for installs that opt out of the default ``compression.in_place:
            # true`` and across delegate-subagent trees.
            get_affinity_scope() or get_conversation_context() or session_id
        )
    except Exception:
        key = str(session_id or "")
    if not key:
        # Stateless one-shot requests (commit messages, summaries, standalone prompts outside
        # a session) lack an ambient conversation or session id. OpenCode Go strictly requires
        # x-opencode-session on every request (HTTP 400 MissingSessionID if absent, #105841)
        # so generate an ephemeral session id fallback.
        key = f"oneshot-{uuid.uuid4().hex[:16]}"
    return {OPENCODE_SESSION_HEADER: key}


def merge_opencode_session_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Merge the affinity header into ``kwargs["extra_headers"]`` (in place).

    Existing per-request headers win, so a caller-pinned value is preserved.
    Non-OpenCode targets are left untouched.
    """
    headers = opencode_session_headers(provider, base_url, session_id)
    if headers:
        existing = kwargs.get("extra_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in headers.items():
            merged.setdefault(key, value)
        kwargs["extra_headers"] = merged
    return kwargs
