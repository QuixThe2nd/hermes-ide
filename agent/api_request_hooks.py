"""Lifecycle-hook payloads for ``AIAgent`` API requests.

JSON-safe coercion, secret-key redaction, size caps, and the ``api_request_error`` hook dispatch.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""
import json
import math
import os
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from agent.usage_pricing import normalize_usage

_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", "proxy_authorization", "cookie", "set_cookie"}


def _model_dump(value: Any) -> Any:
    """``value.model_dump(mode="json")`` with graceful degradation for older pydantic signatures.

    warnings=False: pydantic UserWarnings on generic-union SDK models would leak to the terminal.
    """
    try:
        return value.model_dump(mode="json", warnings=False)
    except TypeError:
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()


class ApiRequestHooksMixin:
    """Hook payload sanitising + ``api_request_error`` dispatch (see module docstring)."""

    def _usage_summary_for_api_request_hook(self, response: Any) -> Optional[Dict[str, Any]]:
        """Token buckets for ``post_api_request`` plugins (no raw ``response`` object)."""
        if response is None:
            return None
        raw_usage = getattr(response, "usage", None)
        if not raw_usage:
            return None
        from dataclasses import asdict

        cu = normalize_usage(raw_usage, provider=self.provider, api_mode=self.api_mode)
        summary = asdict(cu)
        summary.pop("raw_usage", None)
        summary["prompt_tokens"] = cu.prompt_tokens
        summary["total_tokens"] = cu.total_tokens
        return summary

    @staticmethod
    def _usage_cache_presence_for_hook(response: Any) -> Dict[str, Any]:
        """Raw client-usage cache-field presence for ``post_api_request`` plugins.

        Preserves what the CLIENT reported BEFORE Hermes normalization so
        subscribers can tell absent (unknown — the client never sent the
        field) apart from null / explicit zero / positive / invalid. Proxy
        normalization upstream of the client stays unobservable; absence is
        never reported as zero.
        """
        if response is None:
            raw_usage = None
        elif isinstance(response, dict):
            raw_usage = response.get("usage")
        else:
            raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            return {"raw_usage_present": False, "cache_fields": {}}
        # Cache-field candidates across client shapes (OpenAI cached_tokens,
        # Anthropic cache_read/creation_input_tokens, DeepSeek/Kimi
        # prompt_cache_hit/miss_tokens, nested prompt_tokens_details for
        # Chat Completions and input_tokens_details for Responses).
        top_level = (
            "cached_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        )
        nested = ("prompt_tokens_details", "input_tokens_details")
        fields: Dict[str, Any] = {}

        def _field(container: Any, name: str) -> Tuple[bool, Any]:
            """Presence + value of one field on a dict, pydantic model, or plain object.

            Presence must distinguish ABSENT (the client never sent the field)
            from an EXPLICIT null/zero sent by the server. Dicts answer by key
            membership; pydantic models answer via ``model_fields_set`` — an
            unset default is absent even when the attribute exists; anything
            else (SimpleNamespace et al.) falls back to ``hasattr``.
            """
            if isinstance(container, dict):
                return (name in container), container.get(name)
            fields_set = getattr(container, "model_fields_set", None)
            if not hasattr(container, name):
                return False, None
            if isinstance(fields_set, (set, frozenset, tuple, list)) and name not in fields_set:
                return False, None  # pydantic default, not server-sent: absent
            return True, getattr(container, name)

        def _classify(name: str, value: Any) -> None:
            # Invalid values NEVER carry their content: cache fields come from
            # untrusted provider responses, so an arbitrary string/dict (or a
            # NaN/Infinity float) is reported as a fixed reason only — persisting
            # the raw value would leak untrusted content and emit nonfinite JSON.
            if value is None:
                fields[name] = {"state": "null"}
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                fields[name] = {"state": "invalid", "reason": "non_numeric"}
            elif isinstance(value, float) and not math.isfinite(value):
                fields[name] = {"state": "invalid", "reason": "non_finite"}
            elif value < 0:
                # A negative count is unusable but finite: keep the number
                # (still strict JSON) yet classify invalid, never as zero.
                fields[name] = {"state": "invalid", "reason": "negative", "value": value}
            else:
                state = "zero" if value == 0 else "positive"
                fields[name] = {"state": state, "value": value}

        for name in top_level:
            present, value = _field(raw_usage, name)
            if present:
                _classify(name, value)
        for details_name in nested:
            present, details = _field(raw_usage, details_name)
            if not present or details is None:
                continue
            cached_present, cached = _field(details, "cached_tokens")
            if cached_present:
                _classify(f"{details_name}.cached_tokens", cached)
        return {"raw_usage_present": True, "cache_fields": fields}

    @staticmethod
    def _hook_payload_max_chars() -> int:
        raw = os.getenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "50000")
        try:
            return max(1000, int(raw))
        except (TypeError, ValueError):
            return 50000

    @staticmethod
    def _is_sensitive_hook_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower().replace("-", "_")
        return lowered in _SENSITIVE_HOOK_KEYS or lowered.endswith("_api_key")

    @classmethod
    def _hook_jsonable(
        cls, value: Any, *, depth: int = 0, max_depth: int = 8, max_string: int = 8000,
        max_sequence: int = 200,
    ) -> Any:
        if depth > max_depth:
            return f"<{type(value).__name__} depth limit>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) > max_string:
                return value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"

        def recurse(item):
            return cls._hook_jsonable(
                item, depth=depth + 1, max_depth=max_depth, max_string=max_string,
                max_sequence=max_sequence,
            )

        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for idx, (key, item) in enumerate(value.items()):
                if idx >= max_sequence:
                    out["_truncated_items"] = len(value) - max_sequence
                    break
                str_key = str(key)
                out[str_key] = "<redacted>" if cls._is_sensitive_hook_key(str_key) else recurse(item)
            return out
        if isinstance(value, (list, tuple, set)):
            seq = list(value)
            out = [recurse(item) for item in seq[:max_sequence]]
            if len(seq) > max_sequence:
                out.append({"_truncated_items": len(seq) - max_sequence})
            return out
        with suppress(Exception):
            if hasattr(value, "model_dump"):
                return recurse(_model_dump(value))
        with suppress(Exception):
            from dataclasses import asdict, is_dataclass
            if is_dataclass(value):
                return recurse(asdict(value))
        if isinstance(value, SimpleNamespace):
            return recurse(vars(value))
        if hasattr(value, "__dict__"):
            with suppress(Exception):
                return recurse({k: v for k, v in vars(value).items() if not str(k).startswith("_")})
        return str(value)[:max_string]

    @classmethod
    def _sanitize_hook_payload(cls, value: Any) -> Any:
        """JSON-able payload under the size cap: full → reduced caps → truncated preview."""
        limit = cls._hook_payload_max_chars()
        encoded = ""
        for caps in ({}, {"max_string": 1000, "max_sequence": 50}):
            payload = cls._hook_jsonable(value, **caps)
            try:
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                return str(payload)[:limit]
            if len(encoded) <= limit:
                return payload
        return {
            "_truncated": True, "original_type": type(value).__name__, "preview": encoded[:limit]
        }

    def _api_request_payload_for_hook(self, api_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        body = {
            key: value
            for key, value in (api_kwargs or {}).items()
            if key not in {"timeout", "http_client"}
        }
        return self._sanitize_hook_payload({"method": "POST", "body": body})

    def _api_response_payload_for_hook(
        self, response: Any, assistant_message: Any, *, finish_reason: Optional[str]
    ) -> Dict[str, Any]:
        # Raw provider SDK tool_call objects are handed to the sanitizer on purpose; `_hook_jsonable` must
        # keep normalising them (model_dump / __dict__ / dataclass) or subscribers get str() blobs.
        tool_calls = getattr(assistant_message, "tool_calls", None) or []
        return self._sanitize_hook_payload(
            {
                "model": getattr(response, "model", None),
                "finish_reason": finish_reason,
                "assistant_message": {
                    "role": getattr(assistant_message, "role", "assistant"),
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                },
                "usage": self._usage_summary_for_api_request_hook(response),
            }
        )

    def _invoke_api_request_error_hook(
        self, *, task_id: str, turn_id: str, api_request_id: str, api_call_count: int,
        api_start_time: float, api_kwargs: Optional[Dict[str, Any]], error_type: str,
        error_message: str, status_code: Optional[int] = None, retry_count: Optional[int] = None,
        max_retries: Optional[int] = None, retryable: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> None:
        # Lazy module import (not from-import) so tests can replace lifecycle dispatch at this call site.
        with suppress(Exception):
            from hermes_cli import lifecycle as _lifecycle
            if not _lifecycle.has_hook("api_request_error"):
                return
            ended_at = time.time()
            _lifecycle.invoke_hook(
                "api_request_error",
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                session_id=self.session_id or "",
                platform=self.platform or "",
                model=self.model,
                provider=self.provider,
                base_url=self.base_url,
                api_mode=self.api_mode,
                api_call_count=api_call_count,
                api_duration=ended_at - api_start_time,
                started_at=api_start_time,
                ended_at=ended_at,
                status_code=status_code,
                retry_count=retry_count,
                max_retries=max_retries,
                retryable=retryable,
                reason=reason,
                error={"type": error_type, "message": error_message},
                request=self._api_request_payload_for_hook(api_kwargs),
            )
