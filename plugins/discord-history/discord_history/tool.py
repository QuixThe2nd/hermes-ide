from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from . import auth
from .audit import append_denial_event, safe_error_json
from .config import PluginConfig, Secrets
from .retrieval import Request, RetrievalService, RetrievalValidationError

DISCORD_HISTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "guild_id"],
    "properties": {
        "action": {"type": "string", "enum": ["search", "get", "context", "status"]},
        "guild_id": {"type": "string", "pattern": "^[0-9]{17,20}$"},
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "message_id": {"type": "string", "pattern": "^[0-9]{17,20}$"},
        "channel_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": {"type": "string", "pattern": "^[0-9]{17,20}$"}},
        "channel_names": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        "author_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": {"type": "string", "pattern": "^[0-9]{17,20}$"}},
        "author_names": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        "after": {"type": "string", "format": "date-time"},
        "before": {"type": "string", "format": "date-time"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "context_before": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3},
        "context_after": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3},
    },
    "allOf": [
        {"if": {"properties": {"action": {"const": "search"}}}, "then": {"required": ["query"]}},
        {"if": {"properties": {"action": {"enum": ["get", "context"]}}}, "then": {"required": ["message_id"]}},
    ],
}


def _presented_principal() -> tuple[bool, str | None]:
    """Best-effort redacted-denial metadata; never authorizes a request."""
    return auth.get_presented_gateway_identity()


def _resolve_thread_metadata(thread_id: str) -> Mapping[str, Any]:
    from .service import _channel_metadata, _discord_helpers
    _request, get_token = _discord_helpers()
    token = get_token()
    if not token:
        raise RuntimeError("discord_token_missing")
    return _channel_metadata(thread_id, token)


def handle_discord_history(
    arguments: Mapping[str, Any],
    *,
    config: PluginConfig,
    secrets: Secrets,
    connector: Callable[[str], Any],
    denial_path: str | None = None,
) -> str:
    """Validate and authorize every call before the connector can be invoked."""
    try:
        request = Request.parse(arguments)
    except RetrievalValidationError as exc:
        return json.dumps({"error": str(exc)}, separators=(",", ":"))

    try:
        scope = auth.authorize_bound_request(
            config, guild_id=request.guild_id, channel_ids=request.channel_ids,
            resolve_thread=_resolve_thread_metadata,
        )
    except auth.AuthorizationError as exc:
        platform_present, user_id = _presented_principal()
        kwargs: dict[str, Any] = {}
        if denial_path is not None:
            kwargs["path"] = denial_path
        try:
            append_denial_event(exc.reason, platform_present=platform_present, presented_user_id=user_id,
                                audit_hmac_key=secrets.audit_hmac_key, **kwargs)
        except Exception:
            pass
        return safe_error_json()

    try:
        result = RetrievalService(connector, secrets.audit_hmac_key).run(
            secrets.database_url, scope, request
        )
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 100_000:
            raise RuntimeError("unbounded_result")
        return encoded
    except RetrievalValidationError as exc:
        return json.dumps({"error": str(exc)}, separators=(",", ":"))
    except Exception:
        return json.dumps({"error": "retrieval_failed"}, separators=(",", ":"))
