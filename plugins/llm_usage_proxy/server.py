#!/usr/bin/env python3
"""llm-usage-proxy — loopback reverse proxy that records real token usage.

Runs as its own process (systemd unit rendered by the plugin) and forwards
``http://127.0.0.1:<port>/p/<upstream>/<rest>`` to the upstream's configured
base URL, teeing token usage out of responses into a local SQLite DB without
buffering streams.

Deliberately stdlib-only: the unit's ExecStart runs this file under the
interpreter directly, and it must never grow a dependency on hermes_cli or
any optional extra.

Security posture:
  * Binds 127.0.0.1 only — never a LAN/Tailscale address.
  * Key-manager mode (``--manage-keys``) is opt-in. With it, the proxy holds
    the provider API keys in one root-only file and hands clients local
    caller tokens instead: a client authenticates to the proxy with a caller
    token, the proxy injects the real upstream credential, and the ledger
    records which caller made each request. Without the flag the proxy is a
    credential passthrough and never rewrites an Authorization header.
  * OAuth-based providers (Codex, xAI) have no static key to store; a route
    named in the key store's ``oauth`` section is credential-owned by the
    proxy itself: it resolves and refreshes the upstream OAuth token through
    the canonical Hermes auth store (via the ``hermes_cli.llm_proxy_oauth``
    helper and its cross-process locks) and injects it — with the account
    headers that token implies — exactly like a stored key. Clients still
    present only their caller token, and a single-use refresh token is never
    copied out of the canonical store.
  * Any client may name itself with the ``X-Usage-Caller`` label header. The
    label is attribution in the ledger and nothing more: it is validated,
    stripped before anything is forwarded upstream, and is never a credential
    — it cannot authenticate, and it never causes a refusal.
  * Hermes's own routed transports additionally name the chat a request
    serves (``X-Usage-Chat-Type`` / ``-Id`` / ``-Name``): the task-local
    session identity (platform/source, chat id, display name; ``cronjob``
    wins over the delivery channel). Same rules as the label — validated,
    ledger-only, stripped before upstream — and rows written before the
    fields existed keep NULL, shown as an explicit Unknown.
  * Never follows redirects (a 3xx Location passes through to the client),
    so a response can never pivot the proxy onto a different host.
  * Forwards only to upstreams named on the command line; ``/p/<unknown>``
    is a 404, not a fetch. Upstream URLs are validated before the server
    starts listening, before any log line or systemd argv could carry them.
  * No raw access logging: per-request request lines (which embed the query
    string, and query strings can carry keys on some providers) are never
    written. Failures are logged with credential-shaped text scrubbed by
    ``redact_text`` and never include bodies.
  * One row per HTTP attempt; a failed attempt is a row with a NULL status,
    never a silent gap and never a fabricated zero.
  * Opt-in body capture (``--capture-jev-bodies``) stores full request and
    non-streaming response bodies — but only for jev traffic (the
    openrouter-alpha route with ``typesafe/jev-*`` models), in a separate
    ``jev_bodies`` table with a bounded retention window and row cap, never
    in the usage ledger. Off by default: without the flag no body is ever
    stored and behavior is byte-identical to a proxy without the feature.
    Streaming responses are never captured (the request plus an explicit
    streamed marker only), and bodies past the buffered cap are stored
    truncated with an explicit marker rather than silently shortened.
  * Per-harness routing policies (``routing.json`` next to the ledger, see
    routing.py) let an operator pin an ordered route+model list per API
    family for authenticated POLICY callers — a distinct ``policy_callers``
    token namespace in the key store whose creation never arms the global
    caller-token gate, so legacy label-only clients are unaffected. A caller
    label or User-Agent can never arm a policy. On a 429 received before
    anything was sent downstream, the exact (route, model) earns a bounded
    persistent cooldown and the next permitted candidate is tried; auth
    failures, 400s, 5xx and network errors never retry. Requests carrying
    endpoint-owned continuation state (``previous_response_id``,
    ``conversation``) always stay on their arriving route.

Honesty rules for the accounting:
  * Usage completeness (final / partial / missing) is recorded separately
    from the request outcome (completed / aborted / upstream_error).
  * Cumulative usage snapshots overlay each other (never summed); repeated
    terminal events merge into the same single row.
  * Cached/reasoning detail fields are subsets for OpenAI-style usage and
    additive for Anthropic-style usage — normalized totals include each
    component exactly once.
  * Interrupted streams keep whatever genuine usage was observed, marked
    partial — never promoted to authoritative final.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import unquote, urlsplit

# The routing sibling is stdlib-only like this file, but the systemd unit
# executes server.py directly (sys.path[0] is this directory, not the repo
# root), so the package import has a plain-sibling fallback.
try:
    from plugins.llm_usage_proxy import routing as _routing
except ImportError:  # direct script execution: server.py run as __main__
    import routing as _routing  # type: ignore[no-redef]

logger = logging.getLogger("llm-usage-proxy")

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_PORT = 8790
BIND_HOST = "127.0.0.1"  # loopback only; the plugin never passes anything else

# Identity of this service, echoed by /health so callers can verify they are
# talking to this proxy (and this profile's proxy) before trusting it with
# bearer credentials — a generic {"ok": true} is not sufficient identity.
SERVICE_ID = "hermes-llm-usage-proxy"
PROTOCOL_VERSION = 1

# Upstream names this proxy understands by default when run standalone. The
# plugin renders the real route table (resolved from the profile's own
# provider configuration) into the unit's argv; these are only the manual-run
# fallbacks. plugins/llm_usage_proxy/routes.py mirrors this table.
DEFAULT_UPSTREAMS: dict[str, str] = {
    "zai": "https://api.z.ai/api/coding/paas/v4",
    "zai-anthropic": "https://api.z.ai/api/anthropic",
    "kimi": "https://api.kimi.com/coding",
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "xai": "https://api.x.ai/v1",
}

CONNECT_TIMEOUT_SEC = 15.0
STREAM_TIMEOUT_SEC = 600.0
CHUNK_SIZE = 65536
# Bodies/events larger than this are still forwarded but not parsed for
# usage/model — parsing gives up rather than growing without bound.
MAX_PARSE_BYTES = 8 * 1024 * 1024
# Hard cap on a buffered request body (JSON rewrite needs the whole thing).
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024

# Key-manager mode: one root-only file next to the ledger holds the provider
# keys and the caller tokens (see KeyStore).
KEYS_FILENAME = "keys.json"
# Header a client may use to present its caller token without also sending a
# bearer credential (which key-manager mode would otherwise read and replace).
CALLER_TOKEN_HEADER = "X-Usage-Caller-Token"
# Header a client may use to name itself — a free-text label such as "hermes"
# or "codex-cli". It is attribution, never a credential: it is stripped before
# upstream, and it can never authenticate a request or unlock a route key.
CALLER_LABEL_HEADER = "X-Usage-Caller"
# A label longer than this (after stripping) is ignored, not truncated, so a
# client cannot make the ledger record half a name it did not choose.
CALLER_LABEL_MAX_CHARS = 64
CALLER_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

# Headers Hermes's routed transports use to name the CHAT a request serves —
# the per-request twin of the caller label (which names the harness).  Values
# are read from the requesting task's session context
# (gateway.session_context): the type names the surface (discord, cli, … with
# "cronjob" taking precedence over any delivery channel), the id is the
# platform's durable chat id, the name is display text.  Like the caller
# label these are attribution only: validated, recorded on the row, and
# stripped before anything is forwarded upstream.  Id and name travel
# percent-encoded (UTF-8) so no header value can carry control characters.
CHAT_TYPE_HEADER = "X-Usage-Chat-Type"
CHAT_ID_HEADER = "X-Usage-Chat-Id"
CHAT_NAME_HEADER = "X-Usage-Chat-Name"
# Same wire limits hermes_cli.llm_usage_routes applies before sending —
# repeated here so the proxy enforces them itself instead of trusting the
# client.  Over-long or undecodable values are ignored, never truncated.
CHAT_TYPE_MAX_CHARS = 32
CHAT_ID_MAX_CHARS = 128
CHAT_NAME_MAX_CHARS = 128
CHAT_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    upstream TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_tokens INTEGER,
    status_code INTEGER,
    latency_ms INTEGER,
    path TEXT,
    request_id TEXT,
    outcome TEXT,
    usage_complete TEXT,
    caller TEXT,
    chat_type TEXT,
    chat_id TEXT,
    chat_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_upstream_ts
    ON usage_events (upstream, ts);
"""

# The chat index is created separately, AFTER _migrate(): on a DB written by
# an older proxy the chat columns only come into existence there, and an
# index over a missing column is a startup failure instead of a migration.
SCHEMA_CHAT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_usage_events_chat_ts"
    " ON usage_events (chat_id, ts)"
)

# Opt-in jev body capture (--capture-jev-bodies). Bodies live in a table of
# their own — never as new usage_events columns — so the ledger's shape and
# retention are untouched, and the table itself is only created when capture
# is enabled: a default-off proxy leaves the DB byte-identical to one that
# never had the feature.
JEV_BODIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS jev_bodies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    upstream TEXT NOT NULL,
    model TEXT,
    path TEXT,
    request_id TEXT,
    status_code INTEGER,
    latency_ms INTEGER,
    capture_state TEXT NOT NULL,
    request_body TEXT,
    response_body TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jev_bodies_ts ON jev_bodies (ts);
"""

# Routing decision journal: one row per policy-evaluated request saying what
# the router decided and why (selection, skips, cooldowns, failover). A table
# of its own — never new usage_events columns — so the ledger's shape is
# untouched, and it is only created when the proxy runs with key-manager mode
# (the only mode where policy callers can authenticate), mirroring the
# jev_bodies gate: a default-off proxy's DB stays byte-identical.
ROUTING_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    caller TEXT NOT NULL,
    family TEXT,
    arrived_route TEXT NOT NULL,
    arrived_model TEXT,
    selected_route TEXT,
    selected_model TEXT,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_ts ON routing_decisions (ts);
"""

# Hop-by-hop headers (RFC 2616 §13.5.1 + common additions) — never forwarded
# in either direction. Host/Content-Length are handled explicitly instead.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# Statuses that never carry a body — relay headers only.
NO_BODY_STATUSES = {204, 304}

REQUEST_ID_HEADERS = ("x-request-id", "openai-request-id", "request-id")

# Request outcome values (what happened to the HTTP attempt).
OUTCOME_COMPLETED = "completed"
OUTCOME_ABORTED = "aborted"  # client connection lost during relay
OUTCOME_UPSTREAM_ERROR = "upstream_error"  # connect/read failure upstream
OUTCOME_REJECTED = "rejected"  # refused locally (unknown caller token), no upstream attempt

# Caller recorded on a row the attribution gate refused: the request named
# nothing, and the ledger still needs a readable, label-shaped caller for it.
UNATTRIBUTED_CALLER = "unattributed"

# Upstream statuses that make key-manager mode try the route's next stored
# key — once per request, so a dead key cannot turn into a retry storm.
KEY_RETRY_STATUSES = frozenset({401, 429})

# OAuth-managed routes (keys.json "oauth" section: route → provider id).
OAUTH_REFRESH_SKEW_SEC = 60.0
OAUTH_FALLBACK_TTL_SEC = 240.0
OAUTH_HELPER_TIMEOUT_SEC = 75.0

# Policy routing: a 429 answered before anything was sent downstream marks a
# bounded persistent cooldown on exactly (route, model) and the next
# permitted candidate is tried. The attempt bound counts ACTUAL HTTP SENDS,
# alternate-key rotation included: at most MAX_POLICY_ATTEMPTS candidates are
# planned, each costs one send plus at most one legacy key-rotation resend,
# and MAX_POLICY_SENDS hard-caps the total so a long policy can never hammer
# the fleet on one request.
POLICY_FAILOVER_STATUS = 429
MAX_POLICY_ATTEMPTS = 4
MAX_POLICY_SENDS = 2 * MAX_POLICY_ATTEMPTS

# Headers that name an account/credential boundary rather than a request.
# When a policy moves a request across routes they are ALWAYS stripped (the
# target route's own stored key is injected instead): forwarding e.g. a Codex
# OAuth ``ChatGPT-Account-Id`` to an unrelated provider would leak the source
# account across the credential boundary.
ACCOUNT_BOUND_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "chatgpt-account-id",
        "openai-organization",
        "openai-project",
    }
)

# Usage completeness values (how authoritative the token numbers are).
USAGE_FINAL = "final"  # terminal usage event observed per provider rules
USAGE_PARTIAL = "partial"  # some genuine usage seen, no terminal event
USAGE_MISSING = "missing"  # no usage observed (provider omitted it, etc.)

# Jev body capture scope: only this route AND this model family is captured,
# so the flag can never quietly widen into a general body recorder.
JEV_CAPTURE_UPSTREAM = "openrouter-alpha"
JEV_MODEL_PREFIX = "typesafe/jev-"

# Capture state values (what a jev_bodies row actually holds).
CAPTURE_COMPLETE = "complete"  # whole buffered response body
CAPTURE_STREAMED = "streamed"  # SSE: request only, response never captured
CAPTURE_INCOMPLETE = "incomplete"  # relay died / decode failed mid-body
CAPTURE_TRUNCATED = "truncated"  # body exceeded the cap, stored prefix only

# Retention for captured bodies: pruned opportunistically on insert (no timer
# thread — the store is the single writer), plus a hard row cap for disk.
CAPTURE_RETENTION_HOURS = 24.0
CAPTURE_MAX_ROWS = 5000

# Markers appended into stored bodies so a row can never present itself as
# more (or less) than what was actually captured.
CAPTURE_STREAMED_NOTE = (
    "[response was streamed (SSE) and not captured — request body only]"
)
CAPTURE_INCOMPLETE_NOTE = (
    "[capture incomplete: the response ended before its body finished]"
)
CAPTURE_DECODE_BROKEN_NOTE = (
    "[capture incomplete: the response's compression stream failed mid-body]"
)


def _capture_truncation_note(cap: int) -> str:
    return f"[capture truncated at the {cap}-byte buffered cap — prefix only]"

# ── Secret redaction ─────────────────────────────────────────────────────────

_REDACT_PATTERNS = (
    # Whole credential header values: "Authorization: Bearer xyz…"
    re.compile(r"(?i)\b(authorization|proxy-authorization)\s*:\s*\S[^\r\n]*"),
    re.compile(r"(?i)\b(x-api-key|api-key)\s*:\s*\S[^\r\n]*"),
    re.compile(r"(?i)\bcookie\s*:\s*\S[^\r\n]*"),
    # Bearer/basic schemes anywhere (exception strings, URLs)
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    # Provider key shapes
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{6,}\b"),
    # Query-string credentials: ?api_key=…&token=…
    re.compile(r"(?i)[?&](api_key|apikey|key|token|access_token|signature)=[^&\s]+"),
)
_REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    """Scrub credential-shaped substrings so no secret reaches logs or errors."""
    out = str(text)
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


# ── Usage extraction ─────────────────────────────────────────────────────────


def _first_int(*values: Any) -> Optional[int]:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _details(usage: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        nested = usage.get(key)
        if isinstance(nested, Mapping):
            return nested
    return {}


def merge_usage(prev: Optional[dict], new: Mapping[str, Any]) -> dict:
    """Overlay ``new`` onto ``prev`` — never sum.

    OpenAI streaming emits ``"usage": null`` on every chunk until the final
    one; Anthropic splits its counts between ``message_start`` (input side)
    and ``message_delta`` (final output); the Responses API emits cumulative
    snapshots. Overlaying non-null numeric fields is correct for all three:
    earlier fields survive, later events refine them, and cumulative
    snapshots replace instead of accumulating. Summing would double-count.
    """
    merged = dict(prev or {})
    for key, value in new.items():
        if isinstance(value, Mapping):
            merged[key] = merge_usage(
                merged.get(key) if isinstance(merged.get(key), dict) else None,
                value,
            )
        elif value is not None:
            merged[key] = value
    return merged


def usage_row_fields(usage: Mapping[str, Any]) -> dict[str, Optional[int]]:
    """Map any provider's usage object onto the SQLite row columns.

    Family rules matter because providers disagree on whether the detail
    fields are subsets or additions:

    * OpenAI chat/completions: ``prompt_tokens``/``completion_tokens`` are
      the totals; ``prompt_tokens_details.cached_tokens`` and
      ``completion_tokens_details.reasoning_tokens`` are SUBSETS of them —
      never added into the totals.
    * OpenAI Responses: ``input_tokens``/``output_tokens`` are the totals;
      the nested ``*_details`` are subsets.
    * Anthropic: ``input_tokens`` EXCLUDES the cache components — the
      normalized prompt total must add ``cache_read_input_tokens`` and
      ``cache_creation_input_tokens`` exactly once, and those components
      stay preserved in their own columns.

    Missing fields stay None (unknown ≠ measured zero).
    """
    prompt_details = _details(usage, "prompt_tokens_details", "input_tokens_details")
    completion_details = _details(
        usage, "completion_tokens_details", "output_tokens_details"
    )
    anthropic_style = (
        "cache_read_input_tokens" in usage
        or "cache_creation_input_tokens" in usage
    )

    prompt = _first_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
    completion = _first_int(
        usage.get("completion_tokens"), usage.get("output_tokens")
    )
    cache_read = _first_int(
        usage.get("cache_read_input_tokens"),
        # Responses nests its subset-style cached count here.
        prompt_details.get("cached_tokens") if not anthropic_style else None,
    )
    cache_creation = _first_int(
        usage.get("cache_creation_input_tokens"),
        prompt_details.get("cache_creation") if not anthropic_style else None,
    )
    if anthropic_style and prompt is not None:
        # Additive components: include exactly once in the normalized total.
        # A missing base input stays missing — substituting 0 would report a
        # measured prompt/total that is really just the cache components.
        prompt = prompt + (cache_read or 0) + (cache_creation or 0)
    reasoning = _first_int(
        completion_details.get("reasoning_tokens"),
        usage.get("reasoning_tokens"),
    )
    total = _first_int(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cache_read,
        "reasoning_tokens": reasoning,
        "cache_creation_tokens": cache_creation,
        "total_tokens": total,
    }


def parse_json_usage(payload: bytes) -> tuple[Optional[dict], Optional[str]]:
    """Extract ``(usage, model)`` from a complete JSON response body.

    Returns ``(None, model_or_None)`` when the body has no usage object —
    callers still record the row with null token fields.
    """
    model: Optional[str] = None
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    raw_model = obj.get("model")
    if isinstance(raw_model, str) and raw_model:
        model = raw_model
    usage = obj.get("usage")
    if not isinstance(usage, Mapping):
        # Codex Responses nests the whole response under "response" in some
        # error/terminal payloads.
        nested = obj.get("response")
        if isinstance(nested, Mapping):
            usage = nested.get("usage")
            raw_model = nested.get("model")
            if model is None and isinstance(raw_model, str) and raw_model:
                model = raw_model
    if isinstance(usage, Mapping):
        return dict(usage), model
    return None, model


# SSE event types that terminate a Responses-API stream (usage carried on the
# terminal event is authoritative).
_RESPONSES_TERMINAL_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
}


class _SSEEventParser:
    """Byte-accurate incremental SSE event splitter.

    Buffers raw bytes and splits on newlines only, so a UTF-8 sequence or an
    SSE line split across TCP chunks is never decoded mid-character. Events
    with multiple ``data:`` lines are joined with ``\\n`` per the SSE spec.
    Per-event memory is bounded: an event larger than MAX_PARSE_BYTES
    abandons parsing (forwarding continues untouched).
    """

    __slots__ = (
        "_buffer",
        "_data_lines",
        "_data_bytes",
        "_event_name",
        "_on_event",
        "_abandoned",
    )

    def __init__(self, on_event: Callable[[str, str], None]):
        self._buffer = bytearray()
        self._data_lines: list[str] = []
        self._data_bytes = 0
        self._event_name = ""
        self._on_event = on_event
        self._abandoned = False

    @property
    def abandoned(self) -> bool:
        return self._abandoned

    def _abandon(self) -> bool:
        """Stop parsing and release everything retained so far."""
        self._abandoned = True
        self._buffer.clear()
        self._data_lines = []
        self._data_bytes = 0
        self._event_name = ""
        return False

    def feed(self, chunk: bytes) -> None:
        if self._abandoned:
            return
        self._buffer.extend(chunk)
        if len(self._buffer) > MAX_PARSE_BYTES:
            self._abandon()
            return
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if not self._handle_line(line):
                return

    def finish(self) -> None:
        """Dispatch a trailing event that arrived without a final newline."""
        if self._abandoned:
            return
        if self._buffer:
            line = bytes(self._buffer)
            self._buffer.clear()
            self._handle_line(line)
        if self._data_lines or self._event_name:
            self._dispatch()

    def _handle_line(self, raw: bytes) -> bool:
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if line == "":
            self._dispatch()
            return not self._abandoned
        if line.startswith(":"):
            return True  # SSE comment / keep-alive
        if self._data_bytes + len(line) > MAX_PARSE_BYTES:
            return self._abandon()
        if line.startswith("data:"):
            data = line[len("data:") :].lstrip(" ")
            self._data_lines.append(data)
            self._data_bytes += len(data)
        elif line.startswith("event:"):
            self._event_name = line[len("event:") :].strip()
        return True

    def _dispatch(self) -> None:
        if self._data_lines:
            data = "\n".join(self._data_lines)
            name = self._event_name
            self._data_lines = []
            self._data_bytes = 0
            self._event_name = ""
            self._on_event(name, data)


def should_capture_jev_body(
    capture_enabled: bool, upstream_name: str, model: Optional[str]
) -> bool:
    """Narrow gate for opt-in body capture: the jev route AND the jev family.

    The model prefix match is case-insensitive; anything else — other routes,
    other model families, an unparseable request body — is never captured,
    even with the flag on.
    """
    if not capture_enabled:
        return False
    if upstream_name != JEV_CAPTURE_UPSTREAM:
        return False
    return isinstance(model, str) and model.lower().startswith(JEV_MODEL_PREFIX)


class JevBodyCapture:
    """Per-request accumulator for the opt-in jev body capture.

    Exists only for requests that passed :func:`should_capture_jev_body`, so
    every other request pays nothing for the feature. Holds the decoded
    response bytes up to the buffered-body cap; bytes past the cap are
    dropped and the row says so explicitly rather than storing a silently
    shortened body.
    """

    __slots__ = (
        "_cap",
        "_chunks",
        "_held",
        "_streamed",
        "_truncated",
        "_decode_broken",
    )

    def __init__(self, cap: int = MAX_REQUEST_BODY_BYTES) -> None:
        self._cap = int(cap)
        self._chunks: list[bytes] = []
        self._held = 0
        self._streamed = False
        self._truncated = False
        self._decode_broken = False

    def feed(self, text: bytes) -> None:
        """Accumulate already-decoded response bytes (bounded by the cap)."""
        if self._streamed or not text or self._held >= self._cap:
            return
        room = self._cap - self._held
        if len(text) > room:
            text = text[:room]
            self._truncated = True
        self._chunks.append(text)
        self._held += len(text)

    def mark_streamed(self) -> None:
        """SSE detected: hold no bytes, the row says streamed, never half."""
        self._streamed = True
        self._chunks.clear()
        self._held = 0

    def mark_decode_broken(self) -> None:
        """The decompressor failed mid-body: what is held may be short."""
        self._decode_broken = True

    def state(self, outcome: str) -> str:
        if self._streamed:
            return CAPTURE_STREAMED
        if self._decode_broken or outcome != OUTCOME_COMPLETED:
            return CAPTURE_INCOMPLETE
        if self._truncated:
            return CAPTURE_TRUNCATED
        return CAPTURE_COMPLETE

    def request_text(self, body: Optional[bytes]) -> str:
        """Text form of the forwarded request body, cap-marked when shortened."""
        if body is None:
            return ""
        if len(body) > self._cap:
            return (
                body[: self._cap].decode("utf-8", "replace")
                + "\n"
                + _capture_truncation_note(self._cap)
            )
        return body.decode("utf-8", "replace")

    def response_text(self, outcome: str = OUTCOME_COMPLETED) -> str:
        if self._streamed:
            return CAPTURE_STREAMED_NOTE
        text = b"".join(self._chunks).decode("utf-8", "replace")
        if self._truncated:
            text += "\n" + _capture_truncation_note(self._cap)
        if self._decode_broken:
            text += "\n" + CAPTURE_DECODE_BROKEN_NOTE
        elif outcome != OUTCOME_COMPLETED:
            text += "\n" + CAPTURE_INCOMPLETE_NOTE
        return text


class UsageScanner:
    """Incremental tee that watches response bytes go by.

    Fed every forwarded chunk; never holds the stream open. JSON bodies are
    accumulated (bounded) for one parse at stream end; SSE bodies are parsed
    event-by-event as they pass so usage is captured even when the client
    disconnects mid-stream. A parse error abandons parsing only — the
    forwarded payload is untouched and completeness stays honest.
    """

    def __init__(
        self,
        content_type: str,
        content_encoding: str = "",
        capture: Optional[JevBodyCapture] = None,
    ):
        declared = (content_type or "").lower()
        self._events = "event-stream" in declared
        self._json = not self._events and "json" in declared
        # Some upstreams (observed on chatgpt.com/backend-api/codex) answer 200
        # with no Content-Type at all. The declared type can no longer pick a
        # parser, so the first bytes of the body decide instead — see
        # _sniff_format.
        self._format_unknown = not self._events and not self._json
        self._decoder: Optional[zlib.Decompress] = None
        encoding = (content_encoding or "").lower().strip()
        if encoding in {"gzip", "x-gzip"}:
            self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._decoder = zlib.decompressobj(zlib.MAX_WBITS)
        self._json_buffer = bytearray()
        self._sniff_buffer = bytearray()
        self._decompressed_total = 0
        self._parse_abandoned = False
        self.usage: Optional[dict] = None
        self.model: Optional[str] = None
        self._terminal_seen = False
        self._sse = _SSEEventParser(self._on_sse_event)
        # Optional capture tee: when present (a captured jev request), decoded
        # response bytes are also accumulated for the jev_bodies row — even
        # after usage parsing gives up. With None the scanner behaves exactly
        # as it did before capture existed.
        self._capture = capture
        if capture is not None and self._events:
            capture.mark_streamed()

    @property
    def completeness(self) -> str:
        if self.usage is None:
            return USAGE_MISSING
        if self._terminal_seen and not self._parse_abandoned:
            return USAGE_FINAL
        return USAGE_PARTIAL

    def _note_model(self, value: Any) -> None:
        if self.model is None and isinstance(value, str) and value:
            self.model = value

    def _on_sse_event(self, event_name: str, data: str) -> None:
        if self._parse_abandoned or not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        self._note_model(obj.get("model"))
        event_type = obj.get("type") or event_name or ""
        # Codex Responses SSE nests the response object inside typed events;
        # Anthropic nests it under "message".
        usage: Any = obj.get("usage")
        if not isinstance(usage, Mapping):
            for container_key in ("response", "message"):
                container = obj.get(container_key)
                if isinstance(container, Mapping):
                    self._note_model(container.get("model"))
                    candidate = container.get("usage")
                    if isinstance(candidate, Mapping):
                        usage = candidate
                        if event_type in _RESPONSES_TERMINAL_TYPES:
                            event_type = container.get("type") or event_type
                        break
        if isinstance(usage, Mapping):
            self.usage = merge_usage(self.usage, usage)
        # Terminal-event rules: mark the usage authoritative only when the
        # provider's own terminal signal arrived. OpenAI chat chunks have no
        # "type" and carry a non-null usage exactly once, on the final chunk.
        if event_type in _RESPONSES_TERMINAL_TYPES or event_type == "message_stop":
            self._terminal_seen = True
        elif not obj.get("type") and isinstance(usage, Mapping):
            self._terminal_seen = True

    def _ingest_text(self, text: bytes) -> None:
        if self._format_unknown:
            self._sniff_buffer.extend(text)
            if not self._sniff_format():
                return
            text = bytes(self._sniff_buffer)
            self._sniff_buffer = bytearray()
        if self._events:
            self._sse.feed(text)
        elif self._json:
            self._json_buffer.extend(text)
            if len(self._json_buffer) > MAX_PARSE_BYTES:
                self._parse_abandoned = True
                self._json_buffer.clear()

    # Format inference reads only the leading bytes of the body. A buffered
    # prefix that is a proper prefix of an SSE field/comment marker (b"e",
    # b"ev", b"dat", ...) is undecidable, so it keeps buffering within the
    # sniff budget; anything that can never decide abandons parsing rather
    # than guessing, forwarded untouched with usage left honestly missing.
    _SNIFF_LIMIT = 64
    _SSE_LINE_PREFIXES = (b"event:", b"data:", b"id:", b"retry:", b":")

    def _sniff_format(self) -> bool:
        """Decide SSE vs JSON from the buffered prefix; True once decided."""
        head = self._sniff_buffer.lstrip()
        if not head:
            if len(self._sniff_buffer) > self._SNIFF_LIMIT:
                self._parse_abandoned = True
            return False
        if head.startswith(self._SSE_LINE_PREFIXES):
            self._events = True
            if self._capture is not None:
                self._capture.mark_streamed()
        elif head[:1] in (b"{", b"["):
            self._json = True
        elif any(p.startswith(head) for p in self._SSE_LINE_PREFIXES):
            # Proper prefix of an SSE marker (e.g. b"e", b"dat"): wait for
            # more bytes, bounded by the sniff budget so a prefix that never
            # resolves still stops parsing honestly.
            if len(self._sniff_buffer) > self._SNIFF_LIMIT:
                self._parse_abandoned = True
            return False
        else:
            self._parse_abandoned = True
            return False
        self._format_unknown = False
        return True

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._parse_abandoned and self._capture is None:
            return
        if self._decoder is not None:
            try:
                text = self._decoder.decompress(chunk)
            except zlib.error:
                self._parse_abandoned = True
                if self._capture is not None:
                    self._capture.mark_decode_broken()
                return
            self._decompressed_total += len(text)
            if self._decompressed_total > MAX_PARSE_BYTES:
                # Bounded decoding: a compressed stream that expands beyond
                # the parse budget is forwarded but no longer parsed.
                self._parse_abandoned = True
        else:
            text = chunk
        if self._capture is not None:
            self._capture.feed(text)
        if self._parse_abandoned or not text:
            return
        self._ingest_text(text)

    def finish(self) -> None:
        """Flush trailing bytes (final SSE line without newline, gzip footer)."""
        if self._decoder is not None:
            try:
                text = self._decoder.flush()
            except zlib.error:
                text = b""
                if self._capture is not None:
                    self._capture.mark_decode_broken()
        else:
            text = b""
        if self._capture is not None and text:
            self._capture.feed(text)
        if self._parse_abandoned:
            return
        if text:
            self._ingest_text(text)
        if self._events:
            self._sse.finish()
            return
        if self._json and self._json_buffer:
            usage, model = parse_json_usage(bytes(self._json_buffer))
            self._note_model(model)
            if usage is not None:
                self.usage = merge_usage(self.usage, usage)
                self._terminal_seen = True
            self._json_buffer.clear()


def looks_like_chat_completions(path: str) -> bool:
    return path.rstrip("/").endswith("chat/completions")


def maybe_inject_stream_options(body: bytes, path: str) -> Optional[bytes]:
    """Add ``stream_options.include_usage`` to OpenAI-style streaming requests.

    Only for chat/completions bodies that stream without stream_options — the
    OpenAI API then reports usage on the final SSE chunk. Anthropic and Codex
    Responses bodies are returned untouched (their APIs reject the field or
    report usage natively). Returns ``None`` when nothing changed.
    """
    if not looks_like_chat_completions(path) or not body:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("stream") is not True:
        return None
    if isinstance(obj.get("stream_options"), dict):
        return None
    obj["stream_options"] = {"include_usage": True}
    rewritten = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return rewritten if rewritten != body else None


def extract_request_model(body: bytes) -> Optional[str]:
    if not body or len(body) > MAX_PARSE_BYTES:
        return None
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict):
        model = obj.get("model")
        if isinstance(model, str) and model:
            return model
    return None


# ── SQLite store ─────────────────────────────────────────────────────────────


def default_db_path() -> str:
    hermes_home = os.environ.get("HERMES_HOME")
    root = hermes_home or os.path.expanduser("~/.hermes")
    return os.path.join(root, "usage-proxy", "usage.sqlite")




def _ensure_mode_0600(path: str) -> None:
    """Chmod *path* (and its WAL/SHM siblings) to 0600; best effort."""
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            os.chmod(candidate, 0o600)
        except OSError:
            pass


class UsageStore:
    """Tiny locked SQLite sink — one row per proxied request attempt."""

    _MIGRATION_COLUMNS = (
        "outcome TEXT",
        "usage_complete TEXT",
        "caller TEXT",
        "chat_type TEXT",
        "chat_id TEXT",
        "chat_name TEXT",
    )

    def __init__(
        self,
        path: str,
        *,
        capture_jev_bodies: bool = False,
        capture_retention_hours: float = CAPTURE_RETENTION_HOURS,
        capture_max_rows: int = CAPTURE_MAX_ROWS,
        routing_journal: bool = False,
    ):
        self.path = path
        self._capture_enabled = bool(capture_jev_bodies)
        self._capture_retention_hours = max(0.0, float(capture_retention_hours))
        self._capture_max_rows = max(1, int(capture_max_rows))
        self._routing_journal = bool(routing_journal)
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(SCHEMA_CHAT_INDEX)
            if self._capture_enabled:
                self._conn.executescript(JEV_BODIES_SCHEMA)
            if self._routing_journal:
                self._conn.executescript(ROUTING_DECISIONS_SCHEMA)
        _ensure_mode_0600(self.path)

    def _migrate(self) -> None:
        """Add columns introduced after an older DB was created in place.

        ``caller`` is NULL for every row written before key-manager mode —
        those requests were never attributed, and inventing a caller for them
        would be worse than an honest unknown.  The same honesty rule covers
        ``chat_type``/``chat_id``/``chat_name``: rows written before chat
        attribution stay NULL, which the dashboard shows as an explicit
        Unknown chat rather than inferring one.  Every migration is an
        additive ALTER TABLE — existing rows and ids are preserved as-is.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(usage_events)")
        }
        for column in self._MIGRATION_COLUMNS:
            name = column.split()[0]
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE usage_events ADD COLUMN {column}"
                )

    def insert(
        self,
        *,
        upstream: str,
        model: Optional[str],
        usage: Optional[Mapping[str, Any]],
        status_code: Optional[int],
        latency_ms: int,
        path: Optional[str],
        request_id: Optional[str],
        outcome: str,
        usage_complete: str,
        caller: Optional[str] = None,
        chat_type: Optional[str] = None,
        chat_id: Optional[str] = None,
        chat_name: Optional[str] = None,
    ) -> None:
        fields = usage_row_fields(usage or {})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_events (ts, upstream, model, prompt_tokens,"
                " completion_tokens, cached_tokens, reasoning_tokens,"
                " cache_creation_tokens, total_tokens, status_code, latency_ms,"
                " path, request_id, outcome, usage_complete, caller,"
                " chat_type, chat_id, chat_name)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    upstream,
                    model,
                    fields["prompt_tokens"],
                    fields["completion_tokens"],
                    fields["cached_tokens"],
                    fields["reasoning_tokens"],
                    fields["cache_creation_tokens"],
                    fields["total_tokens"],
                    status_code,
                    latency_ms,
                    path,
                    request_id,
                    outcome,
                    usage_complete,
                    caller,
                    chat_type,
                    chat_id,
                    chat_name,
                ),
            )
        _ensure_mode_0600(self.path)

    def insert_jev_body(
        self,
        *,
        ts: str,
        upstream: str,
        model: Optional[str],
        path: Optional[str],
        request_id: Optional[str],
        status_code: Optional[int],
        latency_ms: int,
        capture_state: str,
        request_body: Optional[str],
        response_body: Optional[str],
    ) -> None:
        """Append one captured jev body pair, pruning retention and the row cap.

        Pruning rides the insert's locked transaction (the store is the
        single writer; no timer thread): first the retention window, then the
        row cap keeping the newest rows. Ordering insert-before-prune is what
        makes the cap hold after the insert itself, not just before it.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=self._capture_retention_hours)).isoformat(
            timespec="milliseconds"
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jev_bodies (ts, upstream, model, path, request_id,"
                " status_code, latency_ms, capture_state, request_body,"
                " response_body, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    upstream,
                    model,
                    path,
                    request_id,
                    status_code,
                    latency_ms,
                    capture_state,
                    request_body,
                    response_body,
                    now.isoformat(timespec="milliseconds"),
                ),
            )
            self._conn.execute("DELETE FROM jev_bodies WHERE ts < ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM jev_bodies WHERE id NOT IN"
                " (SELECT id FROM jev_bodies ORDER BY id DESC LIMIT ?)",
                (self._capture_max_rows,),
            )
        _ensure_mode_0600(self.path)

    def insert_routing_decision(
        self,
        *,
        caller: str,
        family: Optional[str],
        arrived_route: str,
        arrived_model: Optional[str],
        selected_route: Optional[str],
        selected_model: Optional[str],
        reason: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """One routing-decision row: what the router did and why.

        ``detail`` carries the attempt/skip list as JSON — route names,
        models, statuses, cooldown seconds, skip reasons; never request
        bodies, headers, or credentials. A no-op unless the journal table
        exists (key-manager mode).
        """
        if not self._routing_journal:
            return
        detail_text = json.dumps(dict(detail), sort_keys=True) if detail else None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO routing_decisions (ts, caller, family, arrived_route,"
                " arrived_model, selected_route, selected_model, reason, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    caller,
                    family,
                    arrived_route,
                    arrived_model,
                    selected_route,
                    selected_model,
                    reason,
                    detail_text,
                ),
            )
        _ensure_mode_0600(self.path)

    def recent_routing_decisions(self, limit: int) -> list[dict[str, Any]]:
        if not self._routing_journal:
            return []
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM routing_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, window_hours: int = 24) -> dict[str, Any]:
        """Totals by upstream and by caller, with explicit unknown/partial counts.

        Sums only cover rows that reported the field — a provider that omits
        usage contributes to ``requests`` and ``usage_missing`` but never to
        the token sums, so unknown traffic is never presented as a measured
        zero.
        """
        since = datetime.now(timezone.utc).timestamp() - window_hours * 3600
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        by_upstream = self._grouped_totals("upstream", since_iso)
        by_caller = self._grouped_totals("caller", since_iso)
        keys = (
            "requests",
            "usage_final",
            "usage_partial",
            "usage_missing",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cache_creation_tokens",
            "total_tokens",
        )
        # Upstream groups are disjoint, so summing them is the overall window.
        overall = {key: 0 for key in keys}
        for row in by_upstream.values():
            for key in keys:
                overall[key] += row[key]
        return {
            "window_hours": window_hours,
            "since": since_iso,
            "overall": overall,
            "by_upstream": by_upstream,
            "by_caller": by_caller,
        }

    def _grouped_totals(self, column: str, since_iso: str) -> dict[str, dict[str, Any]]:
        """Per-``column`` totals over the window; NULL groups stay explicit.

        *column* is always a literal owned by this module, never request data.
        """
        totals_sql = (
            f"SELECT {column} AS group_key, COUNT(*) AS requests,"
            " SUM(CASE WHEN usage_complete = 'final' THEN 1 ELSE 0 END)"
            "   AS usage_final,"
            " SUM(CASE WHEN usage_complete = 'partial' THEN 1 ELSE 0 END)"
            "   AS usage_partial,"
            " SUM(CASE WHEN usage_complete IS NULL OR usage_complete = 'missing'"
            "   THEN 1 ELSE 0 END) AS usage_missing,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
            " COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,"
            " COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens"
            f" FROM usage_events WHERE ts >= ? GROUP BY {column} ORDER BY {column}"
        )
        with self._lock:
            rows = self._conn.execute(totals_sql, (since_iso,)).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row["group_key"]
            # NULL caller = traffic that predates attribution or arrived while
            # no caller tokens were configured. Named, never silently dropped.
            label = key if isinstance(key, str) and key else "(unattributed)"
            totals = dict(row)
            totals.pop("group_key", None)
            grouped[label] = totals
        return grouped

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── Key-manager mode: provider keys + caller tokens ──────────────────────────


def default_keys_path() -> str:
    """Same root-only directory as the ledger, one file for all secrets."""
    hermes_home = os.environ.get("HERMES_HOME")
    root = hermes_home or os.path.expanduser("~/.hermes")
    return os.path.join(root, "usage-proxy", KEYS_FILENAME)


def key_fingerprint(secret: str) -> str:
    """Short stable digest shown by the CLI instead of a key or token."""
    return "sha256:" + hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()[:12]


ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def is_route_name(name: str) -> bool:
    """Route names share the shape the upstream table already enforces."""
    return bool(ROUTE_NAME_RE.match(name or ""))


def is_caller_name(name: str) -> bool:
    return bool(CALLER_NAME_RE.match(name or ""))


def sanitize_caller_label(value: Any) -> Optional[str]:
    """The caller label a request arrived with, or None when it is unusable.

    A label is client-supplied display text, so only the first
    ``CALLER_LABEL_MAX_CHARS`` characters are considered and they must match
    ``CALLER_LABEL_RE`` in full — any other character anywhere in them, or an
    empty or non-string value, means the label is ignored rather than
    sanitized into a name the client did not send.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()[:CALLER_LABEL_MAX_CHARS]
    if not candidate or not CALLER_LABEL_RE.match(candidate):
        return None
    return candidate


def sanitize_chat_type(value: Any) -> Optional[str]:
    """The chat-type header value, or None when it is unusable.

    A closed character set (platform/source identifiers such as ``discord``,
    ``cli``, ``cronjob``) rather than free text: anything outside it, or an
    empty or non-string value, is ignored — the row keeps no chat type
    instead of a rewritten one the client did not send.  The transport sends
    the type un-encoded because it has already passed this same charset.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > CHAT_TYPE_MAX_CHARS:
        return None
    if not CHAT_TYPE_RE.match(candidate):
        return None
    return candidate


def decode_chat_field(value: Any, max_chars: int) -> Optional[str]:
    """Decode a percent-encoded chat header value, or None when unusable.

    Ids and names are platform text (a Matrix room id carries ``!`` and
    ``:``; a chat name can be any Unicode), so they travel UTF-8
    percent-encoded and are decoded here with ``errors="strict"`` — a value
    that does not decode cleanly, decodes to control characters, exceeds
    *max_chars*, or decodes to nothing is ignored, never repaired: the
    ledger would rather record an honest Unknown than half a name.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > max_chars * 9 + 16:
        return None
    try:
        decoded = unquote(candidate, errors="strict")
    except UnicodeDecodeError:
        return None
    decoded = decoded.strip()
    if not decoded or len(decoded) > max_chars:
        return None
    for ch in decoded:
        if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            return None
    return decoded


# The Claude Code CLI names itself "claude-cli/<version> …" on its
# connectivity probes — requests that bypass the Anthropic SDK client and so
# cannot carry X-Usage-Caller — its bundled axios names itself "axios/<version>"
# on the same kind of probes, and Node's own fetch names itself the bare
# "node" (or "node/<version>"). Those are the only UAs the proxy recognizes.
UA_LABEL_RE = re.compile(r"(?:claude-cli|axios)/|node(?:/|$)")


def caller_label_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """Attribute the well-known CLI probe UAs to their harness, else None.

    The harness — not the CLI version — is the caller the ledger wants, so
    every ``claude-cli/…``, ``axios/…``, and ``node``/``node/…`` string maps
    to the one label "claude-code". Anything else (including no UA at all)
    is not attribution.
    """
    if not user_agent:
        return None
    return "claude-code" if UA_LABEL_RE.match(user_agent.strip()) else None


def _ensure_mode_0700(path: str) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class KeyStore:
    """One root-only file holding provider keys and caller tokens.

    ``keys.json`` (0600, in a 0700 directory) maps a route name to the
    provider API keys that may be injected for it — several keys means
    rotation — and a caller name to the local token that names it in the
    ledger. Clients hold caller tokens only; a caller token is never a
    provider credential, and the proxy never hands a provider key back out.

    A second caller namespace, ``policy_callers``, holds the tokens that arm
    per-harness routing policies. It exists so an operator can authenticate
    routing clients WITHOUT turning on the global caller-token gate: the
    ``callers`` map alone drives that gate (``caller_count``), so minting the
    first policy caller never changes what legacy label-only clients must
    present. A policy token is still a real credential — ``caller_for_token``
    accepts it — it simply lives in a namespace whose creation cannot break
    old clients.

    Reads re-read the file when it changed on disk, so ``hermes
    llm_usage_proxy keys set`` and ``callers create`` take effect on a running
    proxy without a restart. Writes rewrite the file atomically: a reader
    either sees the old map or the new one, never a truncated file.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.abspath(str(path or default_keys_path()))
        self._lock = threading.Lock()
        self._routes: dict[str, tuple[str, ...]] = {}
        self._callers: dict[str, str] = {}
        self._policy_callers: dict[str, str] = {}
        self._oauth: dict[str, str] = {}
        self._stamp: Optional[tuple[int, int]] = None
        self._load()

    # ── reads ────────────────────────────────────────────────────────────

    def route_keys(self, route: str) -> tuple[str, ...]:
        with self._lock:
            self._maybe_reload()
            return self._routes.get(route, ())

    def caller_count(self) -> int:
        """Tokens that arm the global caller gate — the ``callers`` map ONLY.

        Policy callers deliberately do not count: minting one must never
        change what legacy label-only clients have to present.
        """
        with self._lock:
            self._maybe_reload()
            return len(self._callers)

    def caller_for_token(self, token: str) -> Optional[str]:
        """The caller name a token belongs to, or None when it is unknown.

        Both namespaces authenticate — a policy token is a real credential
        the operator minted — so a policy caller also satisfies the global
        attribution gate on deployments where that gate is armed.
        """
        if not token:
            return None
        # Bytes, not str: compare_digest rejects non-ASCII *str* outright, and
        # a header value is client-controlled input, not something to 500 on.
        presented = token.encode("utf-8", "replace")
        with self._lock:
            self._maybe_reload()
            for store in (self._callers, self._policy_callers):
                for name, stored in store.items():
                    if hmac.compare_digest(stored.encode("utf-8"), presented):
                        return name
        return None

    def policy_caller_for_token(self, token: str) -> Optional[str]:
        """The POLICY caller a token belongs to — the routing credential.

        This is the only lookup that arms policy routing: a label or a
        User-Agent can never match here, so a forged attribution header can
        never steer a request through someone's routing policy.
        """
        if not token:
            return None
        presented = token.encode("utf-8", "replace")
        with self._lock:
            self._maybe_reload()
            for name, stored in self._policy_callers.items():
                if hmac.compare_digest(stored.encode("utf-8"), presented):
                    return name
        return None

    def policy_caller_count(self) -> int:
        with self._lock:
            self._maybe_reload()
            return len(self._policy_callers)

    def oauth_provider_for(self, route: str) -> Optional[str]:
        with self._lock:
            self._maybe_reload()
            return self._oauth.get(route)

    def oauth_routes(self) -> dict[str, str]:
        with self._lock:
            self._maybe_reload()
            return dict(self._oauth)

    def describe(self) -> dict[str, dict[str, object]]:
        """Route/caller names with fingerprints only — never secret values.

        The shape is exactly ``{"routes": ..., "callers": ...}`` for the
        existing consumers; policy callers are reported separately by
        :meth:`describe_policy_callers`.
        """
        with self._lock:
            self._maybe_reload()
            described: dict[str, dict[str, object]] = {
                "routes": {
                    name: [key_fingerprint(key) for key in keys]
                    for name, keys in sorted(self._routes.items())
                },
                "callers": {
                    name: key_fingerprint(token)
                    for name, token in sorted(self._callers.items())
                },
            }
            if self._oauth:
                described["oauth"] = dict(sorted(self._oauth.items()))
            return described

    def describe_policy_callers(self) -> dict[str, str]:
        """Policy-caller names with token fingerprints only."""
        with self._lock:
            self._maybe_reload()
            return {
                name: key_fingerprint(token)
                for name, token in sorted(self._policy_callers.items())
            }

    # ── writes ───────────────────────────────────────────────────────────

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """Reload→mutate→save under a cross-PROCESS flock.

        The proxy, the CLI and any future admin server are separate
        processes sharing this one file; the instance threading.Lock cannot
        stop a read/update/os.replace here from overwriting another
        writer's key/caller change. Same proven discipline as RoutingStore:
        an exclusive flock on a sibling lock file wraps an UNCONDITIONAL
        reload (the mtime shortcut is not trusted mid-write) and the save.
        """
        with _routing.file_mutex(self.path + ".lock"):
            with self._lock:
                self._load()
                yield

    def set_route_keys(self, route: str, keys: list[str]) -> tuple[str, ...]:
        """Replace a route's key list (order is the rotation order)."""
        if not is_route_name(route):
            raise ValueError(f"invalid route name: {route!r}")
        cleaned: list[str] = []
        for key in keys:
            key = str(key or "").strip()
            if key and key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            raise ValueError(f"no keys given for route {route!r}")
        with self._mutation():
            self._routes[route] = tuple(cleaned)
            self._save()
            return self._routes[route]

    def remove_route(self, route: str) -> bool:
        """Drop a route and all of its keys. True when something was removed."""
        if not is_route_name(route):
            raise ValueError(f"invalid route name: {route!r}")
        with self._mutation():
            if route not in self._routes:
                return False
            del self._routes[route]
            self._save()
            return True

    def remove_route_key(self, route: str, index: int) -> Optional[str]:
        """Remove the 1-based key *index* shown by ``keys list``."""
        with self._mutation():
            keys = self._routes.get(route)
            if not keys or not 1 <= index <= len(keys):
                return None
            removed = keys[index - 1]
            kept = keys[: index - 1] + keys[index:]
            if kept:
                self._routes[route] = kept
            else:
                del self._routes[route]
            self._save()
            return removed

    def create_caller(self, name: str) -> tuple[str, bool]:
        """Mint a caller token; replaces the token when *name* already exists."""
        if not is_caller_name(name):
            raise ValueError(f"invalid caller name: {name!r}")
        token = secrets.token_urlsafe(24)
        with self._mutation():
            rotated = name in self._callers
            self._callers[name] = token
            self._save()
            return token, rotated

    def remove_caller(self, name: str) -> bool:
        with self._mutation():
            if name not in self._callers:
                return False
            del self._callers[name]
            self._save()
            return True

    def create_policy_caller(self, name: str) -> tuple[str, bool]:
        """Mint a POLICY caller token; replaces the token when *name* exists.

        Unlike :meth:`create_caller` this never arms the global caller-token
        gate: the token authenticates routing policy only (and, where the
        gate is already armed, satisfies attribution like any credential).
        """
        if not is_caller_name(name):
            raise ValueError(f"invalid caller name: {name!r}")
        token = secrets.token_urlsafe(24)
        with self._mutation():
            rotated = name in self._policy_callers
            self._policy_callers[name] = token
            self._save()
            return token, rotated

    def remove_policy_caller(self, name: str) -> bool:
        with self._mutation():
            if name not in self._policy_callers:
                return False
            del self._policy_callers[name]
            self._save()
            return True

    def set_oauth_route(self, route: str, provider: str) -> str:
        """Mark *route* as OAuth-managed by *provider* (e.g. openai-codex)."""
        if not is_route_name(route):
            raise ValueError(f"invalid route name: {route!r}")
        provider = str(provider or "").strip()
        if not is_route_name(provider):
            raise ValueError(f"invalid OAuth provider id: {provider!r}")
        with self._mutation():
            self._oauth[route] = provider
            self._save()
            return provider

    def remove_oauth_route(self, route: str) -> bool:
        """Drop a route's OAuth management. True when something was removed."""
        with self._mutation():
            if route not in self._oauth:
                return False
            del self._oauth[route]
            self._save()
            return True

    # ── file plumbing ────────────────────────────────────────────────────

    def _stamp_file(self, path: str) -> Optional[tuple[int, int]]:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _maybe_reload(self) -> None:
        """Caller holds the lock. Pick up edits made by the CLI meanwhile."""
        if self._stamp_file(self.path) != self._stamp:
            self._load()

    def _load(self) -> None:
        path = self.path
        self._stamp = self._stamp_file(path)
        if self._stamp is None:
            self._routes, self._callers, self._policy_callers, self._oauth = {}, {}, {}, {}
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError (a subclass) and a
            # UnicodeDecodeError from a file that is not text at all. Treat a
            # broken store as empty rather than refusing to proxy — but say so.
            logger.error(
                "key store %s unreadable (%s); no keys or callers loaded",
                path,
                redact_text(str(exc)),
            )
            self._routes, self._callers, self._policy_callers, self._oauth = {}, {}, {}, {}
            return
        routes: dict[str, tuple[str, ...]] = {}
        callers: dict[str, str] = {}
        policy_callers: dict[str, str] = {}
        oauth: dict[str, str] = {}
        if isinstance(raw, Mapping):
            raw_routes = raw.get("routes")
            if isinstance(raw_routes, Mapping):
                for name, keys in raw_routes.items():
                    if not is_route_name(str(name)) or not isinstance(keys, list):
                        continue
                    cleaned = tuple(
                        key for key in (str(k).strip() for k in keys) if key
                    )
                    if cleaned:
                        routes[str(name)] = cleaned
            raw_callers = raw.get("callers")
            if isinstance(raw_callers, Mapping):
                for name, token in raw_callers.items():
                    if is_caller_name(str(name)) and isinstance(token, str) and token:
                        callers[str(name)] = token
            raw_policy = raw.get("policy_callers")
            if isinstance(raw_policy, Mapping):
                for name, token in raw_policy.items():
                    if is_caller_name(str(name)) and isinstance(token, str) and token:
                        policy_callers[str(name)] = token
            raw_oauth = raw.get("oauth")
            if isinstance(raw_oauth, Mapping):
                for name, provider in raw_oauth.items():
                    if (
                        is_route_name(str(name))
                        and isinstance(provider, str)
                        and is_route_name(provider.strip())
                    ):
                        oauth[str(name)] = provider.strip()
        self._routes, self._callers, self._policy_callers, self._oauth = (
            routes,
            callers,
            policy_callers,
            oauth,
        )
        _ensure_mode_0600(path)  # a store written loose by an older version

    def _save(self) -> None:
        """Caller holds the lock. Atomic replace keeps readers on one version."""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _ensure_mode_0700(directory)
        payload = {
            "version": 1,
            "routes": {name: list(keys) for name, keys in sorted(self._routes.items())},
            "callers": dict(sorted(self._callers.items())),
            "policy_callers": dict(sorted(self._policy_callers.items())),
            "oauth": dict(sorted(self._oauth.items())),
        }
        handle_fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".keys-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _ensure_mode_0600(self.path)
        self._stamp = self._stamp_file(self.path)


class KeyRotator:
    """Per-route round-robin cursor over the keys a route is configured with.

    ``issue`` hands out the next key and advances the cursor, so concurrent
    requests spread across a route's keys. ``step`` names the key after one
    already used *without* moving the cursor: a retry must not cost the next
    request its own turn.
    """

    def __init__(self, store: KeyStore):
        self._store = store
        self._lock = threading.Lock()
        self._cursors: dict[str, int] = {}

    def issue(self, route: str) -> tuple[Optional[str], int]:
        keys = self._store.route_keys(route)
        if not keys:
            return None, -1
        with self._lock:
            position = self._cursors.get(route, 0) % len(keys)
            self._cursors[route] = (position + 1) % len(keys)
        return keys[position], position

    def step(self, route: str, position: int) -> tuple[Optional[str], int]:
        keys = self._store.route_keys(route)
        if not keys or position < 0:
            return None, -1
        with self._lock:
            nxt = (position + 1) % len(keys)
        return keys[nxt], nxt


class OAuthHelperError(Exception):
    """The OAuth helper could not produce a usable upstream credential."""


def _repo_root() -> str:
    """Repository root that contains ``hermes_cli`` (this file's grandparent)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_oauth_helper(provider: str) -> dict:
    """One fresh upstream OAuth credential for *provider* from the canonical store.

    Spawns ``python -m hermes_cli.llm_proxy_oauth <provider>`` — the Hermes-side
    helper that resolves (and, when expiring, refreshes) the OAuth token through
    the existing auth machinery and its cross-process store locks. This process
    stays stdlib-only; the helper is where ``hermes_cli`` may be imported. The
    helper's stdout is the credential channel and is never logged; its stderr is
    logged only after :func:`redact_text`.
    """
    root = _repo_root()
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root + (os.pathsep + pythonpath if pythonpath else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.llm_proxy_oauth", provider],
            capture_output=True,
            timeout=OAUTH_HELPER_TIMEOUT_SEC,
            env=env,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        raise OAuthHelperError(
            f"OAuth helper for {provider!r} timed out after"
            f" {OAUTH_HELPER_TIMEOUT_SEC:.0f}s"
        ) from None
    except OSError as exc:
        raise OAuthHelperError(
            f"OAuth helper for {provider!r} could not start: {exc}"
        ) from None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace").strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if proc.returncode != 0 or not isinstance(payload, dict) or not payload.get(
        "access_token"
    ):
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or "").strip()
        if not detail:
            detail = proc.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = f"helper exited with status {proc.returncode}"
        raise OAuthHelperError(redact_text(detail)[:500])
    return payload


class OAuthCredentialProvider:
    """Per-route cache of upstream OAuth credentials the proxy owns.

    For a route mapped in the key store's ``oauth`` section, the proxy — not
    any client — obtains and refreshes the upstream credential. Tokens are
    cached until shortly before their expiry; a ``401`` from the upstream
    invalidates the cache entry so the request can be retried once with a
    freshly resolved token (single-use refresh tokens stay safe: the refresh
    itself happens inside the canonical auth store's cross-process lock in
    the helper, never from a copied token here).
    """

    def __init__(
        self,
        store: KeyStore,
        *,
        helper: Callable[[str], dict] = run_oauth_helper,
    ):
        self._store = store
        self._helper = helper
        self._guard = threading.Lock()
        self._route_locks: dict[str, threading.Lock] = {}
        # route → (access_token, extra_headers, expires_at_epoch)
        self._cache: dict[str, tuple[str, dict[str, str], float]] = {}

    def _route_lock(self, route: str) -> threading.Lock:
        with self._guard:
            lock = self._route_locks.get(route)
            if lock is None:
                lock = self._route_locks[route] = threading.Lock()
            return lock

    def credential_for(self, route: str) -> Optional[tuple[str, dict[str, str]]]:
        """``(access_token, extra_headers)`` for *route*, or None.

        None means either the route is not OAuth-managed or the credential
        could not be resolved — the caller fails the request clearly rather
        than passing anything through.
        """
        provider = self._store.oauth_provider_for(route)
        if not provider:
            return None
        with self._route_lock(route):
            cached = self._cache.get(route)
            now = time.time()
            if cached is not None and cached[2] - OAUTH_REFRESH_SKEW_SEC > now:
                return cached[0], dict(cached[1])
            try:
                payload = self._helper(provider)
            except Exception as exc:
                logger.warning(
                    "OAuth credential for route %s unavailable: %s",
                    route,
                    redact_text(str(exc)),
                )
                # A still-valid cached token beats failing the request: the
                # skew window exists precisely so this is rare, and a 401
                # would invalidate below and refetch.
                if cached is not None and cached[2] > now:
                    return cached[0], dict(cached[1])
                return None
            token = str(payload.get("access_token") or "").strip()
            if not token:
                logger.warning(
                    "OAuth helper for route %s returned no access token", route
                )
                return None
            headers = {
                str(name): str(value)
                for name, value in (payload.get("headers") or {}).items()
                if isinstance(name, str) and isinstance(value, str) and name and value
            }
            try:
                expires_at = float(payload.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if expires_at <= now:
                expires_at = now + OAUTH_FALLBACK_TTL_SEC
            self._cache[route] = (token, headers, expires_at)
            return token, dict(headers)

    def invalidate(self, route: str) -> None:
        """Drop the cached credential (an upstream 401 proved it unusable)."""
        with self._route_lock(route):
            self._cache.pop(route, None)


def apply_upstream_auth(
    headers: list[tuple[str, str]], key: str
) -> list[tuple[str, str]]:
    """Swap the client's credential headers for *key*.

    The client's own choice of credential header is the signal for which API
    dialect the upstream expects, so an existing Authorization stays an
    Authorization and an existing x-api-key stays an x-api-key — only the
    value changes. A request carrying no credential at all gets a Bearer
    header, the OpenAI-compatible default.
    """
    replaced: list[tuple[str, str]] = []
    saw_credential = False
    for name, value in headers:
        lowered = name.lower()
        if lowered == "authorization":
            saw_credential = True
            replaced.append((name, f"Bearer {key}"))
        elif lowered == "x-api-key":
            saw_credential = True
            replaced.append((name, key))
        else:
            replaced.append((name, value))
    if not saw_credential:
        replaced.append(("Authorization", f"Bearer {key}"))
    return replaced


def bearer_token(value: Optional[str]) -> Optional[str]:
    """The credential in a ``Bearer …`` header value, else None."""
    if not value:
        return None
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


# Headers the OAuth helper may supply that must never override the credential
# apply_upstream_auth just injected (belt and braces — the helper only returns
# account/workspace headers derived from the same token).
_OAUTH_HEADER_BLOCKLIST = frozenset(
    {"authorization", "x-api-key", "host", "content-length"} | HOP_BY_HOP
)


def apply_oauth_headers(
    headers: list[tuple[str, str]], extra: Mapping[str, str]
) -> list[tuple[str, str]]:
    """Merge helper-supplied account headers into a forwarded request.

    The headers come from the OAuth token the proxy is injecting (e.g.
    ``ChatGPT-Account-ID`` and the residency header the Codex backend derives
    from the JWT), so they must describe *that* token: an existing header of
    the same name is replaced, credential/transport headers are never
    touched.
    """
    if not extra:
        return headers
    wanted = {
        name: value
        for name, value in extra.items()
        if name.lower() not in _OAUTH_HEADER_BLOCKLIST
    }
    if not wanted:
        return headers
    lowered = {name.lower() for name in wanted}
    merged = [(name, value) for name, value in headers if name.lower() not in lowered]
    merged.extend(wanted.items())
    return merged


# ── Proxy handler ────────────────────────────────────────────────────────────


def _split_proxy_path(raw_path: str) -> tuple[Optional[str], str, str]:
    """``/p/<upstream>/<rest>?<query>`` → ``(upstream, rest, query)``."""
    split = urlsplit(raw_path)
    path = split.path
    query = split.query
    if not path.startswith("/p/"):
        return None, "", query
    remainder = path[len("/p/") :]
    if "/" in remainder:
        upstream, rest = remainder.split("/", 1)
    else:
        upstream, rest = remainder, ""
    return (upstream or None), rest, query


_POLICY_CHAT_COMPLETIONS_CANONICAL_REST = "v1/chat/completions"


def _canonical_policy_chat_completions_rest(rest: str) -> str:
    """OpenAI-style ``v1/chat/completions`` tail for policy cross-route reroutes."""
    if (rest or "").lstrip("/") == _POLICY_CHAT_COMPLETIONS_CANONICAL_REST:
        return "chat/completions"
    return rest


def _upstream_request_target(
    base_url: str,
    rest: str,
    query: str,
    *,
    policy_cross_route_chat_completions: bool = False,
) -> str:
    """Origin-form request-target for the upstream (path + query only).

    http.client sends absolute-form when handed a full URL, which is proxy
    dialect; origin servers expect origin-form, so the base's path prefix is
    prepended to the routed rest here and the netloc never appears in it. An
    empty *rest* (the request was for the base path itself) keeps the base
    path exactly — no invented trailing slash.

    When ``policy_cross_route_chat_completions`` is set, a routed rest of
    ``v1/chat/completions`` is rewritten to ``chat/completions`` before the
    base prefix is applied — Kimi- and Z.AI-style bases already carry their own
    version segment. This applies only on policy cross-route Chat Completions
    reroutes; native and same-route paths pass ``rest`` through unchanged.
    """
    effective_rest = rest
    if policy_cross_route_chat_completions:
        effective_rest = _canonical_policy_chat_completions_rest(rest)
    split = urlsplit(base_url)
    if effective_rest:
        target = (split.path or "").rstrip("/") + "/" + effective_rest.lstrip("/")
    else:
        target = split.path or "/"
    if query:
        target += "?" + query
    return target


class UsageProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llm-usage-proxy/1.0"

    @property
    def store(self) -> UsageStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def upstreams(self) -> dict[str, str]:
        return self.server.upstreams  # type: ignore[attr-defined]

    @property
    def manage_keys(self) -> bool:
        """True only when the unit was started with --manage-keys."""
        return bool(getattr(self.server, "manage_keys", False))

    @property
    def key_store(self) -> Optional[KeyStore]:
        return getattr(self.server, "key_store", None)

    @property
    def rotator(self) -> Optional[KeyRotator]:
        return getattr(self.server, "rotator", None)

    @property
    def oauth_provider(self) -> Optional[OAuthCredentialProvider]:
        return getattr(self.server, "oauth_provider", None)

    @property
    def capture_jev_bodies(self) -> bool:
        """True only when the unit was started with --capture-jev-bodies."""
        return bool(getattr(self.server, "capture_jev_bodies", False))

    @property
    def routing_store(self) -> Optional[_routing.RoutingStore]:
        return getattr(self.server, "routing_store", None)

    @property
    def route_families(self) -> Mapping[str, frozenset]:
        """Route name → API families the route's endpoint actually serves.

        Computed once at server construction from the built-in endpoint
        capability table plus the operator's declared overrides
        (``--route-capability`` / ``llm_usage_proxy.route_capabilities``);
        see :func:`routing.route_family_map`. Compatibility is a property of
        the endpoint, never guessed from a route's name.
        """
        return getattr(self.server, "route_families", {})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Raw access logging is disabled outright: the default request line
        # embeds the full path *with query string*, and query strings can
        # carry credentials on some providers. Redaction is a backstop, not
        # the mechanism. Only explicitly-logged, redacted diagnostics appear.
        return

    # ── local endpoints ──────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/health":
            self._send_json(200, self._health_payload())
            return
        if route == "/usage/summary":
            self._send_json(200, self.store.summary())
            return
        if route == "/usage":
            self._send_recent_usage()
            return
        if route == "/routing/policies":
            self._send_routing_policies()
            return
        if route == "/routing/cooldowns":
            self._send_routing_cooldowns()
            return
        if route == "/routing/decisions":
            self._send_routing_decisions()
            return
        if route.startswith("/p/"):
            self._proxy()
            return
        self._send_json(404, {"error": "not found"})

    def _health_payload(self) -> dict[str, Any]:
        server = self.server  # type: ignore[attr-defined]
        payload = {
            "ok": True,
            "service": SERVICE_ID,
            "version": PROTOCOL_VERSION,
            "identity": getattr(server, "identity", "") or "",
            "routes": dict(self.upstreams),
            # Part of the verified identity: a unit running without
            # --manage-keys must never be mistaken for one running with it,
            # because the two disagree about who is allowed to call them.
            "manage_keys": self.manage_keys,
        }
        key_store = self.key_store
        if self.manage_keys and key_store is not None:
            # OAuth-managed routes are part of the verified identity surface:
            # a client checking whether the proxy owns upstream OAuth for a
            # route should not have to guess.
            payload["oauth_routes"] = key_store.oauth_routes()
        return payload

    def _send_recent_usage(self) -> None:
        query = urlsplit(self.path).query
        limit = 50
        for part in query.split("&"):
            if part.startswith("limit="):
                try:
                    limit = int(part[len("limit=") :])
                except ValueError:
                    self._send_json(400, {"error": "limit must be an integer"})
                    return
        self._send_json(200, {"rows": self.store.recent(limit)})

    # ── routing read endpoints ───────────────────────────────────────────
    #
    # Loopback readbacks of the routing state: policies, live cooldowns, and
    # the decision journal. None of these carry secrets — route names, model
    # names, and caller names only (the same caller names the usage ledger
    # already publishes on /usage). Writes stay file-side (CLI/webui write
    # routing.json; this process reloads on mtime), so there is no
    # unauthenticated mutation surface here at all.

    def _send_routing_policies(self) -> None:
        store = self.routing_store
        key_store = self.key_store if self.manage_keys else None
        key_counts = (
            {
                name: (
                    len(key_store.route_keys(name))
                    or (1 if key_store.oauth_provider_for(name) else 0)
                )
                for name in self.upstreams
            }
            if key_store is not None
            else {}
        )
        policies = store.policies_snapshot() if store is not None else {}
        route_families = self.route_families
        now = time.time()
        enriched: dict[str, Any] = {}
        for caller, policy in policies.items():
            entries = []
            for entry in policy["entries"]:
                cooldown = (
                    store.active_cooldown(entry["route"], entry["model"], now)
                    if store is not None
                    else None
                )
                usability = None
                if entry["route"] not in self.upstreams:
                    usability = "unknown_route"
                elif entry["family"] not in route_families.get(
                    entry["route"], frozenset()
                ):
                    # The endpoint does not serve this entry's API family (or
                    # declares no capabilities at all) — planning will skip
                    # it; say so here instead of presenting it as usable.
                    usability = "incompatible_family"
                elif cooldown is not None:
                    usability = "cooldown"
                entries.append(
                    {
                        **entry,
                        "usable": usability is None,
                        "unusable_reason": usability,
                        "cooldown_until": cooldown.get("until") if cooldown else None,
                    }
                )
            enriched[caller] = {"enabled": policy["enabled"], "entries": entries}
        self._send_json(
            200,
            {
                "manage_keys": self.manage_keys,
                "policy_callers": (
                    sorted(key_store.describe_policy_callers())
                    if key_store is not None
                    else []
                ),
                "route_key_counts": key_counts,
                "policies": enriched,
            },
        )

    def _send_routing_cooldowns(self) -> None:
        store = self.routing_store
        cooldowns = store.cooldowns_snapshot() if store is not None else []
        self._send_json(200, {"cooldowns": cooldowns})

    def _send_routing_decisions(self) -> None:
        query = urlsplit(self.path).query
        limit = 50
        for part in query.split("&"):
            if part.startswith("limit="):
                try:
                    limit = int(part[len("limit=") :])
                except ValueError:
                    self._send_json(400, {"error": "limit must be an integer"})
                    return
        self._send_json(200, {"rows": self.store.recent_routing_decisions(limit)})

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in headers or ():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── proxying ─────────────────────────────────────────────────────────

    def _proxy_methods(self) -> None:
        if self.path.startswith("/p/"):
            self._proxy()
        else:
            self._send_json(404, {"error": "not found"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _proxy_methods
    do_HEAD = do_GET  # health/usage headers; /p/ HEAD proxies without body

    def _read_request_body(self) -> Optional[bytes]:
        """Read the request body, decoding chunked framing when present."""
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    raise HTTPException("truncated chunked request body")
                try:
                    size = int(line.strip().split(b";")[0], 16)
                except ValueError:
                    raise HTTPException("malformed chunk size")
                if size == 0:
                    # Trailers: consume through the blank line.
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return b"".join(chunks)
                total += size
                if total > MAX_REQUEST_BODY_BYTES:
                    raise HTTPException("request body too large")
                chunks.append(self._read_exact(size))
                self._read_exact(2)  # CRLF after each chunk
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return None
        try:
            length = int(length_header)
        except ValueError:
            return None
        if length <= 0:
            return b""
        if length > MAX_REQUEST_BODY_BYTES:
            raise HTTPException("request body too large")
        return self._read_exact(length)

    def _read_exact(self, n: int) -> bytes:
        parts = []
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                raise HTTPException("truncated request body")
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def _forward_headers(
        self,
        upstream_netloc: str,
        *,
        drop_authorization: bool = False,
        drop_x_api_key: bool = False,
        drop_account_bound: bool = False,
    ) -> list[tuple[str, str]]:
        """Request headers minus hop-by-hop and Connection-nominated headers.

        Both caller headers are dropped too: the token is a credential for
        *this* proxy and the label names the calling harness — neither is
        something a provider should ever see.  The chat-identity headers are
        dropped for the same reason: they describe *who is chatting with the
        harness*, ledger-only attribution a provider has no business seeing.
        Every drop is by lowercased name, so a malformed or undecodable value
        is stripped exactly like a valid one.

        ``drop_authorization`` removes the Authorization header because its
        bearer was CONSUMED as a local caller token: a credential minted for
        this proxy must never reach an upstream. ``drop_x_api_key`` is the
        same withholding for an x-api-key whose VALUE matched a local
        credential — whichever header authenticated the request, the local
        token itself never crosses. ``drop_account_bound``
        (cross-route policy moves) strips every account/credential-bound
        header (ACCOUNT_BOUND_HEADERS — an OAuth account id valid for the
        source provider means nothing to, and must never leak to, an
        unrelated one); the caller then injects the target route's own key.
        """
        connection_tokens = {
            token.strip().lower()
            for token in (self.headers.get("Connection") or "").split(",")
            if token.strip()
        }
        skip = (
            HOP_BY_HOP
            | connection_tokens
            | {
                "host",
                "content-length",
                CALLER_TOKEN_HEADER.lower(),
                CALLER_LABEL_HEADER.lower(),
                CHAT_TYPE_HEADER.lower(),
                CHAT_ID_HEADER.lower(),
                CHAT_NAME_HEADER.lower(),
            }
        )
        forwarded = []
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in skip:
                continue
            if drop_account_bound and lowered in ACCOUNT_BOUND_HEADERS:
                continue
            if drop_authorization and lowered == "authorization":
                continue
            if drop_x_api_key and lowered == "x-api-key":
                continue
            forwarded.append((name, value))
        forwarded.append(("Host", upstream_netloc))
        return forwarded

    def _presented_caller_token(self) -> Optional[str]:
        """The caller token this request arrived with, if any.

        The dedicated header wins, because key-manager mode otherwise reads
        the Authorization header — which may hold a caller token *or* a
        credential the client still wants passed through untouched. An
        ``x-api-key`` value counts only when it *is* a known caller token
        (Anthropic-protocol clients have no Authorization header to carry
        one); a real provider key in ``x-api-key`` is left to the
        label/User-Agent attribution gate and the passthrough path, exactly
        as before.
        """
        explicit = self.headers.get(CALLER_TOKEN_HEADER)
        if explicit and explicit.strip():
            return explicit.strip()
        bearer = bearer_token(self.headers.get("Authorization"))
        if bearer:
            return bearer
        api_key = self.headers.get("x-api-key")
        key_store = self.key_store
        if api_key and api_key.strip() and key_store is not None:
            candidate = api_key.strip()
            if key_store.caller_for_token(candidate) is not None:
                return candidate
        return None

    def _presented_caller_label(self) -> Optional[str]:
        """The harness label this request arrived with, if it parses.

        Read independently of authentication: a label is a name a client
        gives itself, so it is recorded whether or not the proxy is in
        key-manager mode, and it never decides whether a request is allowed.
        """
        return sanitize_caller_label(self.headers.get(CALLER_LABEL_HEADER))

    def _presented_chat_identity(
        self,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """``(chat_type, chat_id, chat_name)`` this request arrived with.

        Unusable values are dropped field-by-field (an honest Unknown per
        field), independently of the caller resolution: a chat identity is
        recorded whether or not key-manager mode is on, and never gates the
        request — it only names the row.
        """
        chat_type = sanitize_chat_type(self.headers.get(CHAT_TYPE_HEADER))
        chat_id = decode_chat_field(
            self.headers.get(CHAT_ID_HEADER), CHAT_ID_MAX_CHARS
        )
        chat_name = decode_chat_field(
            self.headers.get(CHAT_NAME_HEADER), CHAT_NAME_MAX_CHARS
        )
        return chat_type, chat_id, chat_name

    def _authenticate_caller(
        self, key_store: KeyStore
    ) -> tuple[Optional[str], Optional[str]]:
        """``(caller, error)`` — exactly one of the two is None.

        Caller tokens gate the proxy only once one has been issued: a request
        must then name a known caller, because the ledger has to say who made
        it and an unattributable request cannot be told apart from a forged
        one. A presented-but-unknown token, or a bare label with no token, is
        refused here. Two tokenless requests pass instead: one carrying the
        well-known harness User-Agent (the CLI's probes cannot send custom
        headers, so their UA is the only name they can give), and one that
        names nothing at all — the attribution gate in ``_proxy`` is what
        refuses that one, with the sentinel caller on its row. With no caller
        tokens configured everything passes this check; the gate is what then
        requires a name.
        """
        presented = self._presented_caller_token()
        if not key_store.caller_count():
            return None, None
        name = key_store.caller_for_token(presented or "")
        if name is not None:
            return name, None
        if presented:
            return None, "unknown caller token"
        if caller_label_from_user_agent(self.headers.get("User-Agent")) is not None:
            return None, None
        if self._presented_caller_label() is not None:
            return None, "missing caller token"
        return None, None

    def _record_rejection(
        self,
        *,
        upstream: str,
        path: str,
        model: Optional[str],
        status: int,
        started: float,
        caller: Optional[str] = None,
        chat_type: Optional[str] = None,
        chat_id: Optional[str] = None,
        chat_name: Optional[str] = None,
    ) -> None:
        """Ledger row for a request that never left the proxy.

        The attempt happened, so it is a row — with usage honestly missing
        rather than a fabricated zero, and never a silent gap.
        """
        try:
            self.store.insert(
                upstream=upstream,
                model=model,
                usage=None,
                status_code=status,
                latency_ms=int((time.monotonic() - started) * 1000),
                path=path or "/",
                request_id=None,
                outcome=OUTCOME_REJECTED,
                usage_complete=USAGE_MISSING,
                caller=caller,
                chat_type=chat_type,
                chat_id=chat_id,
                chat_name=chat_name,
            )
        except sqlite3.Error as exc:
            logger.error("failed to record usage row: %s", redact_text(str(exc)))

    def _record_routing_decision(
        self,
        *,
        plan: "_routing.RoutingPlan",
        arrived_route: str,
        arrived_model: Optional[str],
        selected_route: Optional[str],
        selected_model: Optional[str],
        reason: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        """Decision-journal row for a policy-evaluated request.

        Route names, model names, statuses and reasons only — never bodies,
        headers, or credentials. A journal failure must never fail the
        request it describes.
        """
        detail: Optional[dict[str, Any]] = None
        if attempts or plan.skips:
            detail = {"attempts": attempts, "skips": plan.skips}
        try:
            self.store.insert_routing_decision(
                caller=plan.policy_caller or "",
                family=plan.family,
                arrived_route=arrived_route,
                arrived_model=arrived_model,
                selected_route=selected_route,
                selected_model=selected_model,
                reason=reason,
                detail=detail,
            )
        except sqlite3.Error as exc:
            logger.error(
                "failed to record routing decision: %s", redact_text(str(exc))
            )

    def _proxy(self) -> None:
        from plugins.llm_usage_proxy.server_policy import execute_proxy_request

        execute_proxy_request(self)

    def _relay_response(
        self, resp: Any, scanner: UsageScanner, framing_known: bool
    ) -> str:
        """Stream the upstream body through, teeing into the scanner.

        ``read1`` returns whatever has arrived rather than waiting for a full
        buffer, so a small SSE frame is forwarded the moment it lands (the
        old ``read(CHUNK_SIZE)`` could sit on a framed response until 64 KiB
        accumulated or the stream ended).
        """
        try:
            while True:
                chunk = resp.read1(CHUNK_SIZE)
                if not chunk:
                    break
                scanner.feed(chunk)
                if framing_known:
                    self.wfile.write(chunk)
                else:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            if not framing_known:
                # Only the terminating chunk is written after a clean EOF —
                # a truncated stream never gets a synthetic terminator.
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            return OUTCOME_COMPLETED
        except (BrokenPipeError, ConnectionResetError):
            raise _ClientGoneError() from None


class _ClientGoneError(Exception):
    """The client connection died during relay (distinct from upstream errors)."""


class UsageProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        db_path: Optional[str] = None,
        upstreams: Optional[Mapping[str, str]] = None,
        identity: str = "",
        manage_keys: bool = False,
        keys_path: Optional[str] = None,
        capture_jev_bodies: bool = False,
        routing_path: Optional[str] = None,
        route_capabilities: Optional[Mapping[str, Any]] = None,
    ):
        resolved_db_path = db_path or default_db_path()
        # The routing decision journal lives in the ledger only in key-manager
        # mode — the only mode where a policy caller can authenticate, so the
        # only mode where decisions exist. A passthrough proxy's DB stays
        # byte-identical to one that never had the feature.
        self.store = UsageStore(
            resolved_db_path,
            capture_jev_bodies=capture_jev_bodies,
            routing_journal=bool(manage_keys),
        )
        resolved = dict(DEFAULT_UPSTREAMS)
        for name, base in (upstreams or {}).items():
            resolved[name] = base
        self.upstreams = _validate_upstreams(resolved, port=port)
        # Endpoint API capabilities: which API families each route's endpoint
        # genuinely serves (built-in table + operator overrides). A route with
        # no declared capabilities cannot be targeted by a policy — validation
        # rejects the entry at write time and planning skips it — rather than
        # guessing compatibility from the route's name. Invalid overrides fail
        # startup here, not mid-request.
        self.route_families = _routing.route_family_map(
            self.upstreams, route_capabilities
        )
        self.identity = str(identity or "")
        # Key-manager mode is opt-in per process: without the flag no caller is
        # ever refused and no Authorization header is ever rewritten.
        self.manage_keys = bool(manage_keys)
        # Body capture is opt-in per process too: without the flag no body is
        # ever stored and the jev_bodies table is never even created.
        self.capture_jev_bodies = bool(capture_jev_bodies)
        self.key_store = KeyStore(keys_path) if self.manage_keys else None
        self.rotator = KeyRotator(self.key_store) if self.key_store else None
        self.oauth_provider = (
            OAuthCredentialProvider(self.key_store) if self.key_store else None
        )
        # Routing policies/cooldowns: one file next to the ledger, reloaded on
        # mtime. Policy routing arms only for authenticated policy callers
        # (which requires key-manager mode); the store itself is always
        # present so the readback endpoints answer consistently.
        self.routing_store = _routing.RoutingStore(
            routing_path or _routing.default_routing_path(resolved_db_path)
        )
        super().__init__((BIND_HOST, int(port)), UsageProxyHandler)


def _validate_upstreams(
    upstreams: Mapping[str, str], *, port: Optional[int] = None
) -> dict[str, str]:
    """Reject malformed upstream definitions at startup, not mid-request.

    Runs before the server binds (so before any log line or systemd argv
    could carry a bad URL): http(s) only, a host, no userinfo, no query or
    fragment, a valid port, and never this proxy's own loopback origin.
    """
    name_re = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    self_origin = f"{BIND_HOST}:{int(port)}" if port else None
    validated: dict[str, str] = {}
    for name, base in upstreams.items():
        if not name_re.match(name or ""):
            raise ValueError(f"invalid upstream name: {name!r}")
        raw = str(base or "")
        split = urlsplit(raw)
        if split.scheme not in ("http", "https") or not split.netloc:
            raise ValueError(f"invalid upstream base URL for {name!r}: {raw!r}")
        if "@" in split.netloc or split.username or split.password:
            raise ValueError(f"credentials in upstream URL for {name!r} are not allowed")
        if split.query or split.fragment:
            raise ValueError(
                f"query strings/fragments in upstream URL for {name!r} are not allowed"
            )
        try:
            upstream_port = split.port
        except ValueError:
            raise ValueError(f"invalid port in upstream URL for {name!r}") from None
        if upstream_port is None and split.scheme == "http":
            upstream_port = 80
        if upstream_port is None:
            upstream_port = 443
        if (
            self_origin
            and split.scheme == "http"
            and split.hostname in (BIND_HOST, "localhost")
            and upstream_port == int(port)
        ):
            raise ValueError(f"upstream {name!r} targets the proxy itself")
        validated[name] = raw.rstrip("/")
    return validated


def parse_upstream_args(pairs: list[str]) -> dict[str, str]:
    """Parse ``name=url`` entries from repeated ``--upstream`` arguments."""
    upstreams: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--upstream expects name=url, got {pair!r}")
        name, _, base = pair.partition("=")
        upstreams[name.strip()] = base.strip()
    return upstreams


def parse_route_capability_args(pairs: list[str]) -> dict[str, str]:
    """Parse ``name=family[,family]`` from repeated ``--route-capability``.

    The values stay comma-separated strings: :func:`routing.route_family_map`
    validates them (unknown family names fail startup, loudly) when the map
    is built.
    """
    capabilities: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(
                f"--route-capability expects name=family[,family], got {pair!r}"
            )
        name, _, families = pair.partition("=")
        capabilities[name.strip()] = families.strip()
    return capabilities


def compute_identity(value: str) -> str:
    """Stable short identity digest for /health (never the raw value)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-usage-proxy",
        description="Loopback reverse proxy that records real LLM token usage.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=default_db_path())
    parser.add_argument(
        "--identity",
        default="",
        help="Opaque identity token echoed by /health (profile binding)",
    )
    parser.add_argument(
        "--manage-keys",
        action="store_true",
        help=(
            "Inject provider keys from the key store and require caller tokens;"
            " without it the proxy is a credential passthrough"
        ),
    )
    parser.add_argument(
        "--keys-path",
        default=default_keys_path(),
        help="Path to the key store (default: <HERMES_HOME>/usage-proxy/keys.json)",
    )
    parser.add_argument(
        "--capture-jev-bodies",
        action="store_true",
        help=(
            "Opt-in: store full request/response bodies for jev traffic"
            " (openrouter-alpha + typesafe/jev-* models) in a separate"
            " jev_bodies table with bounded retention; default off"
        ),
    )
    parser.add_argument(
        "--routing-path",
        default=None,
        help=(
            "Path to the routing state file (per-harness policies + 429"
            " cooldowns; default: routing.json next to --db)"
        ),
    )
    parser.add_argument(
        "--upstream",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="Override/add an upstream base URL (repeatable)",
    )
    parser.add_argument(
        "--route-capability",
        action="append",
        default=[],
        metavar="NAME=FAMILY[,FAMILY]",
        help=(
            "Declare the API families a route's endpoint serves"
            " (chat_completions, anthropic_messages, responses; repeatable)."
            " Overrides the built-in endpoint capability table; a route with"
            " no declared capabilities cannot be targeted by routing policies"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    # DB/WAL/SHM must be 0600 from creation even outside the systemd unit's
    # UMask — apply before the store opens the file.
    os.umask(0o077)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        upstreams = parse_upstream_args(args.upstream)
        server = UsageProxyServer(
            port=args.port,
            db_path=args.db,
            upstreams=upstreams,
            identity=args.identity,
            manage_keys=args.manage_keys,
            keys_path=args.keys_path,
            capture_jev_bodies=args.capture_jev_bodies,
            routing_path=args.routing_path,
            route_capabilities=parse_route_capability_args(args.route_capability),
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(
            f"llm-usage-proxy: startup failed: {redact_text(str(exc))}",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "llm-usage-proxy listening on %s:%d (db=%s, upstreams=%s, manage-keys=%s,"
        " capture-jev-bodies=%s)",
        BIND_HOST,
        args.port,
        args.db,
        ",".join(sorted(server.upstreams)),
        "yes" if args.manage_keys else "no",
        "yes" if args.capture_jev_bodies else "no",
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
