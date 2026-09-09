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
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

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
    usage_complete TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_upstream_ts
    ON usage_events (upstream, ts);
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

# Usage completeness values (how authoritative the token numbers are).
USAGE_FINAL = "final"  # terminal usage event observed per provider rules
USAGE_PARTIAL = "partial"  # some genuine usage seen, no terminal event
USAGE_MISSING = "missing"  # no usage observed (provider omitted it, etc.)

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

    __slots__ = ("_buffer", "_data_lines", "_event_name", "_on_event", "_abandoned")

    def __init__(self, on_event: Callable[[str, str], None]):
        self._buffer = bytearray()
        self._data_lines: list[str] = []
        self._event_name = ""
        self._on_event = on_event
        self._abandoned = False

    @property
    def abandoned(self) -> bool:
        return self._abandoned

    def feed(self, chunk: bytes) -> None:
        if self._abandoned:
            return
        self._buffer.extend(chunk)
        if len(self._buffer) > MAX_PARSE_BYTES:
            self._abandoned = True
            self._buffer.clear()
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
        if len(self._data_lines) * 2 + len(line) > MAX_PARSE_BYTES:
            self._abandoned = True
            self._buffer.clear()
            return False
        if line.startswith("data:"):
            self._data_lines.append(line[len("data:") :].lstrip(" "))
        elif line.startswith("event:"):
            self._event_name = line[len("event:") :].strip()
        return True

    def _dispatch(self) -> None:
        if self._data_lines:
            data = "\n".join(self._data_lines)
            name = self._event_name
            self._data_lines = []
            self._event_name = ""
            self._on_event(name, data)


class UsageScanner:
    """Incremental tee that watches response bytes go by.

    Fed every forwarded chunk; never holds the stream open. JSON bodies are
    accumulated (bounded) for one parse at stream end; SSE bodies are parsed
    event-by-event as they pass so usage is captured even when the client
    disconnects mid-stream. A parse error abandons parsing only — the
    forwarded payload is untouched and completeness stays honest.
    """

    def __init__(self, content_type: str, content_encoding: str = ""):
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

    # Format inference reads only the leading bytes of the body, and an
    # undecidable prefix abandons parsing rather than guessing: SSE and JSON
    # bodies are already distinct at their first non-whitespace byte, and
    # anything else is forwarded untouched with usage left honestly missing.
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
        elif head[:1] in (b"{", b"["):
            self._json = True
        else:
            self._parse_abandoned = True
            return False
        self._format_unknown = False
        return True

    def feed(self, chunk: bytes) -> None:
        if self._parse_abandoned or not chunk:
            return
        if self._decoder is not None:
            try:
                text = self._decoder.decompress(chunk)
            except zlib.error:
                self._parse_abandoned = True
                return
            self._decompressed_total += len(text)
            if self._decompressed_total > MAX_PARSE_BYTES:
                # Bounded decoding: a compressed stream that expands beyond
                # the parse budget is forwarded but no longer parsed.
                self._parse_abandoned = True
                return
        else:
            text = chunk
        if text:
            self._ingest_text(text)

    def finish(self) -> None:
        """Flush trailing bytes (final SSE line without newline, gzip footer)."""
        if self._parse_abandoned:
            return
        if self._decoder is not None:
            try:
                text = self._decoder.flush()
            except zlib.error:
                text = b""
        else:
            text = b""
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

    _MIGRATION_COLUMNS = ("outcome TEXT", "usage_complete TEXT")

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
        _ensure_mode_0600(self.path)

    def _migrate(self) -> None:
        """Add columns introduced after an older DB was created in place."""
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
    ) -> None:
        fields = usage_row_fields(usage or {})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_events (ts, upstream, model, prompt_tokens,"
                " completion_tokens, cached_tokens, reasoning_tokens,"
                " cache_creation_tokens, total_tokens, status_code, latency_ms,"
                " path, request_id, outcome, usage_complete)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
        _ensure_mode_0600(self.path)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, window_hours: int = 24) -> dict[str, Any]:
        """Totals by upstream, with explicit counts for unknown/partial usage.

        Sums only cover rows that reported the field — a provider that omits
        usage contributes to ``requests`` and ``usage_missing`` but never to
        the token sums, so unknown traffic is never presented as a measured
        zero.
        """
        since = datetime.now(timezone.utc).timestamp() - window_hours * 3600
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        totals_sql = (
            "SELECT upstream, COUNT(*) AS requests,"
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
            " FROM usage_events WHERE ts >= ? GROUP BY upstream ORDER BY upstream"
        )
        with self._lock:
            rows = self._conn.execute(totals_sql, (since_iso,)).fetchall()
        by_upstream = {row["upstream"]: dict(row) for row in rows}
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
        overall = {key: sum(row[key] for row in rows) for key in keys}
        return {
            "window_hours": window_hours,
            "since": since_iso,
            "overall": overall,
            "by_upstream": by_upstream,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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


def _upstream_request_target(base_url: str, rest: str, query: str) -> str:
    """Origin-form request-target for the upstream (path + query only).

    http.client sends absolute-form when handed a full URL, which is proxy
    dialect; origin servers expect origin-form, so the base's path prefix is
    prepended to the routed rest here and the netloc never appears in it. An
    empty *rest* (the request was for the base path itself) keeps the base
    path exactly — no invented trailing slash.
    """
    split = urlsplit(base_url)
    if rest:
        target = (split.path or "").rstrip("/") + "/" + rest.lstrip("/")
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
        if route.startswith("/p/"):
            self._proxy()
            return
        self._send_json(404, {"error": "not found"})

    def _health_payload(self) -> dict[str, Any]:
        server = self.server  # type: ignore[attr-defined]
        return {
            "ok": True,
            "service": SERVICE_ID,
            "version": PROTOCOL_VERSION,
            "identity": getattr(server, "identity", "") or "",
            "routes": dict(self.upstreams),
        }

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

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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

    def _forward_headers(self, upstream_netloc: str) -> list[tuple[str, str]]:
        """Request headers minus hop-by-hop and Connection-nominated headers."""
        connection_tokens = {
            token.strip().lower()
            for token in (self.headers.get("Connection") or "").split(",")
            if token.strip()
        }
        skip = HOP_BY_HOP | connection_tokens | {"host", "content-length"}
        forwarded = []
        for name, value in self.headers.items():
            if name.lower() in skip:
                continue
            forwarded.append((name, value))
        forwarded.append(("Host", upstream_netloc))
        return forwarded

    def _proxy(self) -> None:
        started = time.monotonic()
        upstream_name, rest, query = _split_proxy_path(self.path)
        if not upstream_name or upstream_name not in self.upstreams:
            # Close rather than drain an unread body off a keep-alive socket.
            self.close_connection = True
            self._send_json(404, {"error": f"unknown upstream '{upstream_name or ''}'"})
            return
        base_url = self.upstreams[upstream_name]
        split_base = urlsplit(base_url)
        target = _upstream_request_target(base_url, rest, query)
        # The routed path only (never the query — it can carry keys on some
        # providers) is what we store.
        routed_path = rest

        body = None
        try:
            body = self._read_request_body()
        except (HTTPException, OSError, ValueError) as exc:
            self.close_connection = True
            self._send_json(400, {"error": redact_text(str(exc))})
            return

        model = extract_request_model(body) if body else None
        if body:
            rewritten = maybe_inject_stream_options(body, routed_path)
            if rewritten is not None:
                body = rewritten

        conn: Optional[HTTPConnection] = None
        status_code: Optional[int] = None
        request_id: Optional[str] = None
        scanner: Optional[UsageScanner] = None
        outcome = OUTCOME_UPSTREAM_ERROR
        headers_sent = False
        try:
            if split_base.scheme == "https":
                conn = HTTPSConnection(
                    split_base.netloc,
                    timeout=CONNECT_TIMEOUT_SEC,
                    context=ssl.create_default_context(),
                )
            else:
                conn = HTTPConnection(split_base.netloc, timeout=CONNECT_TIMEOUT_SEC)
            headers = self._forward_headers(split_base.netloc)
            body_for_request = body if body is not None else (
                b"" if self.command in ("POST", "PUT", "PATCH") else None
            )
            if body is not None:
                headers.append(("Content-Length", str(len(body))))
            # http.client applies the socket timeout to connects AND reads;
            # stretch it once connected so long streams are not cut short.
            conn.request(self.command, target, body=body_for_request, headers=dict(headers))
            if conn.sock is not None:
                conn.sock.settimeout(STREAM_TIMEOUT_SEC)
            resp = conn.getresponse()

            status_code = resp.status
            for header in REQUEST_ID_HEADERS:
                value = resp.getheader(header)
                if value:
                    request_id = value
                    break

            # Response hop-by-hop set: static list plus any header the
            # upstream's Connection header nominates for dropping.
            resp_connection_tokens = {
                token.strip().lower()
                for token in (resp.getheader("Connection") or "").split(",")
                if token.strip()
            }
            resp_headers = []
            raw_length = resp.getheader("Content-Length")
            try:
                content_length: Optional[int] = (
                    int(raw_length) if raw_length is not None else None
                )
            except ValueError:
                content_length = None
            chunked_upstream = (
                "chunked"
                in (resp.getheader("Transfer-Encoding") or "").lower()
            )
            for name, value in resp.getheaders():
                lowered = name.lower()
                if lowered in HOP_BY_HOP or lowered in resp_connection_tokens:
                    continue
                if lowered == "content-length":
                    continue
                resp_headers.append((name, value))

            content_type = resp.getheader("Content-Type") or ""
            content_encoding = resp.getheader("Content-Encoding") or ""
            scanner = UsageScanner(content_type, content_encoding)

            self.send_response_only(resp.status, resp.reason)
            for name, value in resp_headers:
                self.send_header(name, value)
            framing_known = content_length is not None and not chunked_upstream
            if framing_known:
                self.send_header("Content-Length", str(content_length))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            headers_sent = True

            relay_body = (
                self.command != "HEAD"
                and resp.status >= 200
                and resp.status not in NO_BODY_STATUSES
            )
            if not relay_body:
                resp.close()
                outcome = OUTCOME_COMPLETED
            else:
                outcome = self._relay_response(resp, scanner, framing_known)
        except _ClientGoneError:
            outcome = OUTCOME_ABORTED
            self.close_connection = True
        except (OSError, HTTPException, ssl.SSLError, ValueError) as exc:
            outcome = OUTCOME_UPSTREAM_ERROR
            # Upstream failure: keep the detail out of logs unless scrubbed —
            # exception text routinely embeds full URLs (which can carry
            # userinfo on misconfigured upstreams).
            detail = redact_text(f"{type(exc).__name__}: {exc}")
            logger.warning("upstream %s failed: %s", upstream_name, detail)
            if not headers_sent:
                try:
                    self._send_json(502, {"error": f"upstream unavailable: {detail}"})
                except OSError:
                    self.close_connection = True
            else:
                # Mid-stream failure after headers were sent: the client
                # connection can no longer be framed honestly; hang it up so
                # the truncation is visible instead of silently "complete".
                self.close_connection = True
        finally:
            if conn is not None:
                conn.close()
            if scanner is not None:
                # Flush the parser even when the relay died mid-stream: a
                # partial event whose usage already arrived is real usage.
                scanner.finish()
            latency_ms = int((time.monotonic() - started) * 1000)
            # Whatever usage the scanner genuinely observed before the
            # failure is kept — errors must not discard real numbers.
            usage = scanner.usage if scanner is not None else None
            completeness = (
                scanner.completeness if scanner is not None else USAGE_MISSING
            )
            if outcome != OUTCOME_COMPLETED and completeness == USAGE_FINAL:
                # An interrupted stream's numbers are real but not
                # authoritative — never promote them to final.
                completeness = USAGE_PARTIAL
            try:
                self.store.insert(
                    upstream=upstream_name,
                    model=(scanner.model if scanner else None) or model,
                    usage=usage,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    path=routed_path or "/",
                    request_id=request_id,
                    outcome=outcome,
                    usage_complete=completeness,
                )
            except sqlite3.Error as exc:
                logger.error("failed to record usage row: %s", redact_text(str(exc)))

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
    ):
        self.store = UsageStore(db_path or default_db_path())
        resolved = dict(DEFAULT_UPSTREAMS)
        for name, base in (upstreams or {}).items():
            resolved[name] = base
        self.upstreams = _validate_upstreams(resolved, port=port)
        self.identity = str(identity or "")
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


def compute_identity(value: str) -> str:
    """Stable short identity digest for /health (never the raw value)."""
    import hashlib

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
        "--upstream",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="Override/add an upstream base URL (repeatable)",
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
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(
            f"llm-usage-proxy: startup failed: {redact_text(str(exc))}",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "llm-usage-proxy listening on %s:%d (db=%s, upstreams=%s)",
        BIND_HOST,
        args.port,
        args.db,
        ",".join(sorted(server.upstreams)),
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
