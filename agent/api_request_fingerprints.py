"""Bounded request-shape metadata for the per-API lifecycle hooks.

Pure, agent-free helper used by the ``pre_api_request`` owner
(``agent/turn_api_request.py``) so subscribers can correlate request shape,
component stability and observed cache usage WITHOUT receiving prompt text,
tool arguments or response content. Everything here operates on the RAW
``api_kwargs`` BEFORE ``_sanitize_hook_payload`` truncation — a truncated
preview is never hashed as if it were the complete component.

The fingerprints describe the canonical client-side, pre-transport
representation (the dict Hermes hands to the provider SDK), NOT the exact
wire bytes the provider receives (SDK serialization, proxies and
normalization upstream are all unobserved) and NOT tokens.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

# Per-message fingerprint bound. Beyond this the tail is aggregated into the
# whole-component hash only; ``per_message_truncated`` says so explicitly.
_MAX_PER_MESSAGE = 512

#: Label stamped on every record so consumers never mistake these measures
#: for wire bytes or token counts.
REPRESENTATION = "canonical-client-pre-transport"


def _canonical_bytes(value: Any) -> Optional[bytes]:
    """UTF-8 bytes of the canonical JSON encoding, or ``None`` if unserializable.

    Key order is PRESERVED (``sort_keys=False``): component order is part of
    the request identity a cache sees.
    """
    try:
        # No ``default`` fallback: an object json cannot represent natively
        # (arbitrary class instances, sets, ...) must FAIL serialization so the
        # caller reports partial/unavailable coverage — stringifying it with
        # str()/repr() would stamp a lossy representation as "complete".
        # ``allow_nan=False``: NaN/Infinity are not valid JSON, so a request
        # carrying them is UNSUPPORTED here, never "complete" canonical JSON.
        return json.dumps(
            value, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        return None


def _component_fingerprint(value: Any) -> Dict[str, Any]:
    """SHA256 + byte/item measures for one ordered request component.

    ``coverage`` is ``"complete"`` only when the whole component serialized;
    a component that only serializes per-item is reported ``"partial"``
    (never silently hashed from a subset), a missing one ``"unavailable"``.
    """
    if value is None:
        return {"coverage": "unavailable"}
    data = _canonical_bytes(value)
    if data is not None:
        items = len(value) if isinstance(value, (list, tuple, dict)) else 1
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "utf8_bytes": len(data),
            "items": items,
            "coverage": "complete",
        }
    # Whole-component serialization failed: hash per-item when possible and
    # say the coverage is partial.
    if isinstance(value, (list, tuple)):
        parts = [_canonical_bytes(item) for item in value]
        if all(p is not None for p in parts):
            joined = b"[" + b",".join(parts) + b"]" if parts else b"[]"
            return {
                "sha256": hashlib.sha256(joined).hexdigest(),
                "items": len(parts),
                "coverage": "partial",
                "partial_reason": "whole-component serialization failed; per-item only",
            }
    return {"coverage": "unavailable"}


def _messages_fingerprint(value: Any) -> Dict[str, Any]:
    """Component fingerprint plus bounded per-message hashes (prefix-stable).

    Per-message hashes let a consumer find the FIRST changed message between
    consecutive requests (cache-prefix stability) without any plaintext.
    Ordering is significant; the list mirrors the request order exactly.
    """
    out = _component_fingerprint(value)
    if not isinstance(value, list):
        return out
    per_message: List[str] = []
    for idx, item in enumerate(value):
        if idx >= _MAX_PER_MESSAGE:
            out["per_message_truncated"] = True
            break
        data = _canonical_bytes(item)
        if data is None:
            out.setdefault("per_message_gaps", []).append(idx)
            continue
        per_message.append(hashlib.sha256(data).hexdigest()[:16])
    if per_message or value:
        out["per_message"] = per_message
    return out


def fingerprint_api_request(api_kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Bounded request-shape metadata for one outgoing API attempt.

    Never raises and never includes prompt/tool/message content — only
    hashes, byte counts, item counts and coverage labels.
    """
    kwargs = api_kwargs if isinstance(api_kwargs, dict) else {}
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        messages = kwargs.get("input")
    system = kwargs.get("system")
    if system is None:
        system = kwargs.get("instructions")
    return {
        "representation": REPRESENTATION,
        "tools": _component_fingerprint(kwargs.get("tools")),
        "system": _component_fingerprint(system),
        "messages": _messages_fingerprint(messages),
        **_embedded_role_subtotals(messages),
    }


_EMBEDDED_ROLES = ("system", "developer")


def _embedded_role_subtotals(messages: Any) -> Dict[str, Any]:
    """Subtotal fingerprints for system/developer messages carried inside ``messages``.

    Chat Completions puts the system prompt in ``messages[role=system]`` rather
    than a separate ``system`` kwarg; the nested labels (``source:
    "messages[role=system]"``) mark these as the SAME bytes already counted in
    the ``messages`` total, so a consumer adding components never double
    counts. Empty when the request carries no such messages.
    """
    if not isinstance(messages, list):
        return {}
    embedded: Dict[str, Any] = {}
    for role in _EMBEDDED_ROLES:
        subset = [m for m in messages if isinstance(m, dict) and m.get("role") == role]
        if not subset:
            continue
        fp = _messages_fingerprint(subset)
        fp["source"] = f"messages[role={role}]"
        embedded[role] = fp
    if not embedded:
        return {}
    return {"messages_embedded": embedded}
