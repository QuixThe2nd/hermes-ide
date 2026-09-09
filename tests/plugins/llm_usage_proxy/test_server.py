"""Proxy server behaviour: routing, usage extraction, tee streaming, redaction.

Everything runs against loopback fake upstreams on ephemeral ports — no live
network, no real provider credentials.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading

import pytest

from plugins.llm_usage_proxy.server import (
    DEFAULT_UPSTREAMS,
    MAX_PARSE_BYTES,
    PROTOCOL_VERSION,
    SERVICE_ID,
    UsageProxyServer,
    UsageScanner,
    maybe_inject_stream_options,
    merge_usage,
    parse_upstream_args,
    redact_text,
    usage_row_fields,
)

from conftest import (
    proxy_request,
    respond_json,
    respond_sse,
    wait_for_row_count,
)

SECRET = "sk-live-secret-token-9876543210"


def _zai(upstream) -> dict:
    return {"zai": f"http://127.0.0.1:{upstream.server_address[1]}/v4"}


# ── 1. Path routing, Host header, query preservation, redirects ──────────────


def test_path_routing_preserves_query_and_sets_host(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions?stream=true&x=1",
        body=json.dumps({"model": "glm-5", "messages": []}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
    )

    assert status == 200
    assert json.loads(body) == {"ok": True}
    sent = upstream.requests[-1]
    assert sent["method"] == "POST"
    # Base URL prefix is prepended, query string survives, origin-form only.
    assert sent["path"] == "/v4/chat/completions?stream=true&x=1"
    # Host is rewritten to the upstream, not the proxy.
    assert sent["headers"]["Host"] == f"127.0.0.1:{upstream.server_address[1]}"
    # Credentials still reach the upstream (the proxy measures, it does not
    # authenticate).
    assert sent["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_unknown_upstream_is_404_not_a_fetch(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy({"zai": f"http://127.0.0.1:{upstream.server_address[1]}"})

    status, _, body = proxy_request(proxy.server_address[1], "GET", "/p/evil/x")

    assert status == 404
    assert "unknown upstream" in json.loads(body)["error"]
    assert upstream.requests == []


def test_no_cross_host_redirect_follow(start_upstream, start_proxy):
    """A 302 to another host passes through; the proxy never follows it."""

    def _redirect(handler):
        handler.send_response(302)
        handler.send_header("Location", "http://10.99.99.99:1/evil")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    upstream = start_upstream(_redirect)
    proxy = start_proxy(_zai(upstream))

    status, headers, _ = proxy_request(
        proxy.server_address[1], "POST", "/p/zai/chat/completions", body=b"{}"
    )

    assert status == 302
    assert headers["Location"] == "http://10.99.99.99:1/evil"
    assert len(upstream.requests) == 1  # the redirect target was never fetched


def test_hop_by_hop_headers_are_stripped(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/x",
        body=b"{}",
        headers={
            "Connection": "keep-alive",
            "Transfer-Encoding": "identity",
            "X-Custom": "keep-me",
        },
    )

    sent = upstream.requests[-1]["headers"]
    assert "Connection" not in sent
    assert "Transfer-Encoding" not in sent
    assert sent["X-Custom"] == "keep-me"


# ── 2. Usage extraction: OpenAI / Anthropic / Codex Responses JSON ───────────


def test_openai_json_usage_recorded(start_upstream, start_proxy, tmp_path):
    upstream = start_upstream(
        respond_json(
            {
                "model": "glm-5",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
            headers={"x-request-id": "req-abc-123"},
        )
    )
    proxy = start_proxy(_zai(upstream))

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=json.dumps({"model": "glm-5", "messages": []}).encode(),
    )
    assert status == 200

    rows = wait_for_row_count(proxy.store.path, 1)
    row = rows[0]
    assert row["upstream"] == "zai"
    assert row["model"] == "glm-5"
    assert row["prompt_tokens"] == 12
    assert row["completion_tokens"] == 7
    assert row["cached_tokens"] == 4
    assert row["reasoning_tokens"] == 3
    assert row["total_tokens"] == 19
    assert row["status_code"] == 200
    assert row["request_id"] == "req-abc-123"
    assert row["path"] == "chat/completions"
    assert row["latency_ms"] is not None


def test_anthropic_json_usage_recorded(start_upstream, start_proxy):
    upstream = start_upstream(
        respond_json(
            {
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 55,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 12,
                },
            },
        )
    )
    proxy = start_proxy(
        {"zai-anthropic": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai-anthropic/v1/messages",
        body=json.dumps({"model": "claude-sonnet-5", "messages": []}).encode(),
    )
    assert status == 200

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["upstream"] == "zai-anthropic"
    assert row["model"] == "claude-sonnet-5"
    assert row["prompt_tokens"] == 152  # input + cache_read + cache_creation
    assert row["completion_tokens"] == 55  # output_tokens
    assert row["cached_tokens"] == 40  # cache_read_input_tokens
    assert row["cache_creation_tokens"] == 12
    assert row["reasoning_tokens"] is None
    assert row["total_tokens"] == 207  # derived when absent


def test_codex_responses_json_usage_recorded(start_upstream, start_proxy):
    upstream = start_upstream(
        respond_json(
            {
                "model": "gpt-5.2",
                "usage": {
                    "input_tokens": 31,
                    "output_tokens": 9,
                    "total_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 7},
                    "output_tokens_details": {"reasoning_tokens": 4},
                },
            },
        )
    )
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-5.2", "input": []}).encode(),
    )
    assert status == 200

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["upstream"] == "openai-codex"
    assert row["model"] == "gpt-5.2"
    assert row["prompt_tokens"] == 31  # input_tokens
    assert row["completion_tokens"] == 9  # output_tokens
    assert row["cached_tokens"] == 7
    assert row["reasoning_tokens"] == 4
    assert row["total_tokens"] == 40


def test_missing_usage_still_records_row_with_nulls(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"error": "rate_limited"}))
    proxy = start_proxy(_zai(upstream))

    status, _, _ = proxy_request(
        proxy.server_address[1], "POST", "/p/zai/chat/completions", body=b"{}"
    )
    assert status == 200

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["status_code"] == 200
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    assert row["total_tokens"] is None
    assert row["latency_ms"] is not None


def test_upstream_refused_records_row_and_returns_502(tmp_path, start_proxy):
    # Reserve a port then close it: connections are refused, fast.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    proxy = start_proxy({"zai": f"http://127.0.0.1:{port}/v4"})

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=b"{}",
        headers={"Authorization": f"Bearer {SECRET}"},
    )

    assert status == 502
    assert SECRET not in body.decode("utf-8", "replace")
    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["status_code"] is None
    assert row["latency_ms"] is not None
    assert row["upstream"] == "zai"


# ── 3. SSE tee: incremental forwarding + final usage row ────────────────────


def test_sse_bytes_stream_incrementally_and_usage_row_stored(
    start_upstream, start_proxy
):
    gate = threading.Event()
    upstream = start_upstream(
        respond_sse(
            [
                b'data: {"model":"glm-5","usage":null}\n\n',
                b'data: {"model":"glm-5","usage":{"prompt_tokens":10,"completion_tokens":5,"prompt_tokens_details":{"cached_tokens":2}}}\n\n',
                b"data: [DONE]\n\n",
            ],
            gate=gate,
        )
    )
    proxy = start_proxy(_zai(upstream))
    port = proxy.server_address[1]

    # Raw socket client: recv() must yield the first event BEFORE the gate
    # opens — proving the proxy tees instead of buffering the stream.
    client = socket.create_connection(("127.0.0.1", port), timeout=10)
    client.settimeout(10)
    request = (
        b"POST /p/zai/chat/completions HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 34\r\n\r\n"
        + json.dumps({"model": "glm-5", "stream": True}).encode()
    )
    assert len(json.dumps({"model": "glm-5", "stream": True})) == 34
    client.sendall(request)

    received = b""
    while b"usage" not in received:
        chunk = client.recv(4096)
        assert chunk, "stream closed before first event"
        received += chunk
    assert gate.is_set() is False, "first event must arrive before the gate opens"

    gate.set()
    while b"[DONE]" not in received:
        chunk = client.recv(4096)
        assert chunk, "stream closed before completion"
        received += chunk
    client.close()

    rows = wait_for_row_count(proxy.store.path, 1)
    row = rows[0]
    assert row["model"] == "glm-5"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5
    assert row["cached_tokens"] == 2
    assert row["total_tokens"] == 15
    assert row["status_code"] == 200


def test_sse_anthropic_usage_merges_across_events(start_upstream, start_proxy):
    upstream = start_upstream(
        respond_sse(
            [
                b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-5","usage":{"input_tokens":100,"cache_read_input_tokens":50,"output_tokens":1}}}\n\n',
                b'event: message_delta\ndata: {"type":"message_delta","delta":{},"usage":{"output_tokens":42}}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
        )
    )
    proxy = start_proxy(
        {"zai-anthropic": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai-anthropic/v1/messages",
        body=json.dumps({"model": "claude-sonnet-5", "stream": True}).encode(),
    )
    assert status == 200
    assert b"message_stop" in body

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["model"] == "claude-sonnet-5"
    assert row["prompt_tokens"] == 150  # input + cache_read (no cache_creation)
    assert row["completion_tokens"] == 42
    assert row["cached_tokens"] == 50
    assert row["total_tokens"] == 192


def test_sse_codex_responses_completed_event_usage(start_upstream, start_proxy):
    upstream = start_upstream(
        respond_sse(
            [
                b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"gpt-5.2","usage":{"input_tokens":31,"output_tokens":9,"input_tokens_details":{"cached_tokens":7},"output_tokens_details":{"reasoning_tokens":4}}}}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
    )
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-5.2", "stream": True}).encode(),
    )
    assert status == 200

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["model"] == "gpt-5.2"
    assert row["prompt_tokens"] == 31
    assert row["completion_tokens"] == 9
    assert row["cached_tokens"] == 7
    assert row["reasoning_tokens"] == 4


# ── 3b. responses with no Content-Type ───────────────────────────────────────


def respond_sse_no_content_type(events: list[bytes]):
    """Fake upstream that streams SSE but omits Content-Type entirely.

    chatgpt.com/backend-api/codex is observed doing exactly this: 200, chunked
    body, no ``Content-Type`` and no ``Content-Encoding``. The declared type
    therefore cannot pick the parser.
    """

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for event in events:
            handler.wfile.write(b"%x\r\n" % len(event) + event + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")

    return _respond


_CODEX_EVENTS = [
    b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n',
    b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"gpt-6-astra","usage":{"input_tokens":23,"output_tokens":5,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}}}\n\n',
    b"data: [DONE]\n\n",
]


def test_codex_sse_without_content_type_records_usage(start_upstream, start_proxy):
    upstream = start_upstream(respond_sse_no_content_type(_CODEX_EVENTS))
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, headers, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-6-astra", "stream": True}).encode(),
    )
    assert status == 200
    assert "Content-Type" not in {name.title() for name in headers}

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["model"] == "gpt-6-astra"
    assert row["prompt_tokens"] == 23
    assert row["completion_tokens"] == 5
    assert row["usage_complete"] == "final"
    assert row["status_code"] == 200


def test_no_content_type_json_usage_recorded(start_upstream, start_proxy):
    """Inference is not SSE-specific: an unlabelled JSON body parses too."""

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        body = json.dumps(
            {
                "id": "resp_1",
                "model": "gpt-6-astra",
                "usage": {"input_tokens": 11, "output_tokens": 3},
            }
        ).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    upstream = start_upstream(_respond)
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-6-astra"}).encode(),
    )
    assert status == 200

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["model"] == "gpt-6-astra"
    assert row["prompt_tokens"] == 11
    assert row["completion_tokens"] == 3
    assert row["usage_complete"] == "final"


def test_no_content_type_body_is_forwarded_byte_for_byte(start_upstream, start_proxy):
    upstream = start_upstream(respond_sse_no_content_type(_CODEX_EVENTS))
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-6-astra", "stream": True}).encode(),
    )
    assert status == 200
    assert body == b"".join(_CODEX_EVENTS)


def test_unrecognized_body_without_content_type_stays_missing(start_upstream, start_proxy):
    """A body that is neither SSE nor JSON records an honest missing row."""
    body = b"<html><body>gateway timeout</body></html>"
    upstream = start_upstream(respond_sse_no_content_type([body]))
    proxy = start_proxy(
        {"openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}"}
    )

    status, _, forwarded = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openai-codex/responses",
        body=json.dumps({"model": "gpt-6-astra", "stream": True}).encode(),
    )
    assert status == 200
    assert forwarded == body

    row = wait_for_row_count(proxy.store.path, 1)[0]
    assert row["usage_complete"] == "missing"
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None


# ── 4. stream_options.include_usage injection ────────────────────────────────


def test_inject_stream_options_rules():
    streaming = json.dumps({"model": "m", "stream": True, "messages": []}).encode()

    injected = maybe_inject_stream_options(streaming, "v1/chat/completions")
    assert injected is not None
    assert json.loads(injected)["stream_options"] == {"include_usage": True}

    # Bare path (base URL already carries /v1) counts too.
    assert maybe_inject_stream_options(streaming, "chat/completions") is not None

    # Already opted in → untouched.
    has_options = json.dumps(
        {"model": "m", "stream": True, "stream_options": {"include_usage": False}}
    ).encode()
    assert maybe_inject_stream_options(has_options, "v1/chat/completions") is None

    # Not streaming → untouched.
    assert (
        maybe_inject_stream_options(
            json.dumps({"model": "m", "stream": False}).encode(),
            "v1/chat/completions",
        )
        is None
    )

    # Anthropic and Codex Responses bodies are never touched.
    assert maybe_inject_stream_options(streaming, "v1/messages") is None
    assert maybe_inject_stream_options(streaming, "responses") is None
    assert maybe_inject_stream_options(streaming, "v1/responses") is None

    # Non-JSON → untouched.
    assert maybe_inject_stream_options(b"not json", "v1/chat/completions") is None


def test_forwarded_streaming_request_gets_injected(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    body = json.dumps({"model": "glm-5", "stream": True, "messages": []}).encode()
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/zai/chat/completions",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200

    sent = upstream.requests[-1]
    forwarded = json.loads(sent["body"])
    assert forwarded["stream_options"] == {"include_usage": True}
    # Content-Length matches the rewritten body, not the original.
    assert int(sent["headers"]["Content-Length"]) == len(sent["body"])


def test_non_openai_bodies_are_forwarded_verbatim(start_upstream, start_proxy):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(
        {
            "zai-anthropic": f"http://127.0.0.1:{upstream.server_address[1]}",
            "openai-codex": f"http://127.0.0.1:{upstream.server_address[1]}",
        }
    )
    original = json.dumps({"model": "m", "stream": True, "max_tokens": 8}).encode()

    for path in ("/p/zai-anthropic/v1/messages", "/p/openai-codex/responses"):
        proxy_request(proxy.server_address[1], "POST", path, body=original)
        sent = upstream.requests[-1]
        assert sent["body"] == original, path


# ── 5. Secret redaction ──────────────────────────────────────────────────────


def test_redact_text_scrubs_credential_shapes():
    assert SECRET not in redact_text(f"Authorization: Bearer {SECRET}")
    assert SECRET not in redact_text(f"x-api-key: {SECRET}")
    assert SECRET not in redact_text(f"connect failed with {SECRET} in flight")
    assert "api_key=lesecret" not in redact_text("GET /x?api_key=lesecret&y=1")
    # Benign detail survives.
    assert "api.z.ai" in redact_text("connect to api.z.ai refused")


def test_secrets_never_reach_logs_or_storage(start_upstream, start_proxy, caplog):
    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))

    with caplog.at_level(logging.DEBUG, logger="llm-usage-proxy"):
        proxy_request(
            proxy.server_address[1],
            "POST",
            "/p/zai/chat/completions",
            body=json.dumps({"model": "glm-5"}).encode(),
            headers={
                "Authorization": f"Bearer {SECRET}",
                "X-Api-Key": SECRET,
                "Cookie": f"session={SECRET}",
            },
        )
        # One failure path too: an upstream that dies mid-request.
        upstream.respond = lambda handler: (_ for _ in ()).throw(OSError("boom"))

    assert SECRET not in caplog.text
    rows = wait_for_row_count(proxy.store.path, 1)
    for row in rows:
        assert SECRET not in json.dumps(row)


# ── 6. /health and /usage endpoints ─────────────────────────────────────────


def test_health_endpoint(start_proxy):
    proxy = start_proxy({"zai": "https://api.invalid.example/v4"})
    status, headers, body = proxy_request(
        proxy.server_address[1], "GET", "/health"
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["service"] == SERVICE_ID
    assert payload["version"] == PROTOCOL_VERSION
    assert "identity" in payload
    assert isinstance(payload["routes"], dict)
    assert payload["routes"]["zai"] == "https://api.invalid.example/v4"
    assert "application/json" in headers["Content-Type"]


def test_usage_summary_and_recent_rows(start_upstream, start_proxy):
    upstream = start_upstream(
        respond_json(
            {
                "model": "glm-5",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
    )
    kimi = start_upstream(
        respond_json(
            {
                "model": "k2",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        )
    )
    proxy = start_proxy(
        {
            "zai": f"http://127.0.0.1:{upstream.server_address[1]}",
            "kimi": f"http://127.0.0.1:{kimi.server_address[1]}",
        }
    )
    port = proxy.server_address[1]

    for _ in range(2):
        proxy_request(
            port, "POST", "/p/zai/chat/completions", body=json.dumps({"model": "glm-5"}).encode()
        )
    proxy_request(
        port, "POST", "/p/kimi/chat/completions", body=json.dumps({"model": "k2"}).encode()
    )

    wait_for_row_count(proxy.store.path, 3)

    status, _, body = proxy_request(port, "GET", "/usage/summary")
    summary = json.loads(body)
    assert status == 200
    assert summary["window_hours"] == 24
    assert summary["overall"]["requests"] == 3
    assert summary["overall"]["prompt_tokens"] == 120
    assert summary["overall"]["completion_tokens"] == 60
    assert summary["by_upstream"]["zai"]["requests"] == 2
    assert summary["by_upstream"]["kimi"]["prompt_tokens"] == 100

    status, _, body = proxy_request(port, "GET", "/usage?limit=2")
    rows = json.loads(body)["rows"]
    assert status == 200
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]  # newest first
    assert set(rows[0]) >= {
        "id",
        "ts",
        "upstream",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "status_code",
        "latency_ms",
        "path",
        "request_id",
    }


def test_unknown_local_path_is_404(start_proxy):
    proxy = start_proxy({"zai": "https://api.invalid.example/v4"})
    status, _, _ = proxy_request(proxy.server_address[1], "GET", "/nope")
    assert status == 404


# ── units ────────────────────────────────────────────────────────────────────


def test_db_file_mode_is_0600(start_upstream, start_proxy):
    import os
    import stat

    upstream = start_upstream(respond_json({"ok": True}))
    proxy = start_proxy(_zai(upstream))
    proxy_request(
        proxy.server_address[1], "POST", "/p/zai/x", body=b"{}"
    )
    wait_for_row_count(proxy.store.path, 1)
    mode = stat.S_IMODE(os.stat(proxy.store.path).st_mode)
    assert mode == 0o600


def test_merge_usage_overlays_and_skips_nulls():
    merged = merge_usage(None, {"input_tokens": 5, "output_tokens": None})
    assert merged == {"input_tokens": 5}
    merged = merge_usage(merged, {"output_tokens": 9})
    assert merged == {"input_tokens": 5, "output_tokens": 9}


def test_usage_row_fields_covers_all_families():
    openai = usage_row_fields(
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
    )
    assert (openai["prompt_tokens"], openai["completion_tokens"]) == (1, 2)
    assert openai["cached_tokens"] == 3
    assert openai["reasoning_tokens"] == 4
    assert openai["total_tokens"] == 3

    anthropic = usage_row_fields(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 6,
        }
    )
    assert anthropic["prompt_tokens"] == 21  # input + cache_read + cache_creation
    assert anthropic["completion_tokens"] == 20
    assert anthropic["cached_tokens"] == 5
    assert anthropic["cache_creation_tokens"] == 6
    assert anthropic["total_tokens"] == 41

    # Missing base input stays unknown: 0 + cache components would report a
    # measured prompt/total that is really just the cache detail.
    cache_only = usage_row_fields(
        {
            "output_tokens": 20,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 12,
        }
    )
    assert cache_only["prompt_tokens"] is None
    assert cache_only["total_tokens"] is None
    assert cache_only["completion_tokens"] == 20
    assert cache_only["cached_tokens"] == 40
    assert cache_only["cache_creation_tokens"] == 12


def test_default_upstreams_and_argv_parsing():
    assert DEFAULT_UPSTREAMS["openai-codex"] == "https://chatgpt.com/backend-api/codex"
    assert DEFAULT_UPSTREAMS["zai-anthropic"] == "https://api.z.ai/api/anthropic"
    assert parse_upstream_args(["zai=https://a/v1", "kimi=https://b"]) == {
        "zai": "https://a/v1",
        "kimi": "https://b",
    }
    with pytest.raises(ValueError):
        parse_upstream_args(["no-slash"])
    with pytest.raises(ValueError):
        UsageProxyServer(
            port=0, db_path=":memory:", upstreams={"Bad Name": "https://x"}
        )


# ── Parser boundary regressions: fragmented sniffing, per-event memory ─────

_RESP_SSE = (
    'event: response.created\n'
    'data: {"type":"response.created","response":{"model":"gpt-6","usage":{"input_tokens":3}}}\n\n'
    'event: response.completed\n'
    'data: {"type":"response.completed","response":{"model":"gpt-6","usage":{"input_tokens":23,"output_tokens":5,"total_tokens":28}}}\n\n'
).encode()

_OAI_SSE = (
    'data: {"id":"1","model":"gpt","choices":[{"delta":{"content":"a"}}]}\n\n'
    'data: {"id":"1","model":"gpt","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}\n\n'
    'data: [DONE]\n\n'
).encode()


def _scan(content_type: str, payload: bytes, step: int) -> UsageScanner:
    scanner = UsageScanner(content_type)
    for i in range(0, len(payload), step):
        scanner.feed(payload[i : i + step])
    scanner.finish()
    return scanner


def test_sniff_no_content_type_sse_survives_every_byte_split():
    # A first chunk shorter than the SSE keyword (b"e", b"ev", b"dat", ...) is
    # a proper prefix of a supported field marker: undecidable, not garbage.
    for step in range(1, len(b"event:") + 1):
        scanner = _scan("", _RESP_SSE, step)
        fields = usage_row_fields(scanner.usage or {})
        assert not scanner._parse_abandoned, f"split={step}"
        assert fields["prompt_tokens"] == 23, f"split={step}"
        assert fields["completion_tokens"] == 5, f"split={step}"
        assert scanner.completeness == "final", f"split={step}"


def test_sniff_no_content_type_data_first_survives_every_byte_split():
    for step in range(1, len(b"data:") + 1):
        scanner = _scan("", _OAI_SSE, step)
        fields = usage_row_fields(scanner.usage or {})
        assert not scanner._parse_abandoned, f"split={step}"
        assert fields["prompt_tokens"] == 100, f"split={step}"
        assert fields["completion_tokens"] == 50, f"split={step}"
        assert scanner.completeness == "final", f"split={step}"


def test_sniff_no_content_type_comment_id_retry_lines_parse():
    body = (
        b": keep-alive\n"
        b"id: 42\n"
        b"retry: 1000\n"
        + _OAI_SSE
    )
    for step in (1, 2, 3):
        scanner = _scan("", body, step)
        fields = usage_row_fields(scanner.usage or {})
        assert not scanner._parse_abandoned, f"split={step}"
        assert fields["prompt_tokens"] == 100, f"split={step}"
        assert scanner.completeness == "final", f"split={step}"


def test_sniff_no_content_type_json_and_garbage_unchanged():
    body = b'{"model":"gpt","usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}'
    for step in (1, 7, len(body)):
        scanner = _scan("", body, step)
        fields = usage_row_fields(scanner.usage or {})
        assert fields["prompt_tokens"] == 11 and fields["completion_tokens"] == 4
        assert scanner.completeness == "final", f"split={step}"
    # A bounded prefix that can never decide (whitespace past the sniff
    # budget) still abandons honestly instead of buffering forever.
    scanner = UsageScanner("")
    scanner.feed(b" " * 70)
    scanner.feed(_OAI_SSE)
    scanner.finish()
    assert scanner._parse_abandoned
    assert scanner.usage is None
    # Non-SSE, non-JSON first byte still abandons at once.
    scanner = _scan("", b"<html>nope</html>", 1)
    assert scanner._parse_abandoned
    assert scanner.usage is None


def test_multiline_event_memory_bounded_during_feed():
    # Many sub-budget data: lines in ONE event, no terminating blank line:
    # the parser must abandon mid-event, before any dispatch, and release
    # what it retained.
    line = b"data: " + b"x" * 500_000 + b"\n"
    scanner = UsageScanner("text/event-stream")
    peak_held = 0
    for _ in range(64):  # 32MB of event data if never bounded
        scanner.feed(line)
        peak_held = max(peak_held, sum(len(d) for d in scanner._sse._data_lines))
        if scanner._sse.abandoned:
            break
    assert scanner._sse.abandoned, "cumulative event bytes exceeded budget mid-feed"
    assert peak_held <= MAX_PARSE_BYTES, f"peak_held={peak_held}"
    assert not scanner._sse._data_lines, "retained buffers cleared on abandonment"


def test_single_oversized_line_still_abandons():
    scanner = UsageScanner("text/event-stream")
    huge = b"data: " + b"z" * (MAX_PARSE_BYTES + 10) + b"\n\n"
    for i in range(0, len(huge), 65536):
        scanner.feed(huge[i : i + 65536])
    scanner.finish()
    assert scanner._sse.abandoned
    assert scanner.usage is None


# ── 11. Managed-service reconcile crosses the real port probe ────────────────
#
# Regression: reconcile_service used to call probe_port_state with an
# unsupported ``bind=`` keyword, so `llm_usage_proxy enable/disable` died with
# TypeError before installing anything. These tests drive reconcile_service
# into the REAL probe (sockets + HTTP, no probe mock) on both control paths;
# only systemctl and the install scope are faked, inside a temp HERMES_HOME.


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _FakeSystemctl:
    """Records systemctl argv and answers state queries from a tiny model."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.enabled = False
        self.active = False

    def __call__(self, args):
        argv = [str(part) for part in args]
        self.calls.append(argv)
        tail = [part for part in argv if part not in ("systemctl", "--user")]
        action = tail[0] if tail else ""
        if action == "is-enabled":
            return (0, "enabled\n", "") if self.enabled else (3, "disabled\n", "")
        if action == "is-active":
            return (0, "active\n", "") if self.active else (3, "inactive\n", "")
        if action == "enable":
            self.enabled = True
            if "--now" in argv:
                self.active = True
        elif action == "disable":
            self.enabled = False
        elif action == "stop":
            self.active = False
        elif action == "restart":
            self.active = True
        return (0, "", "")


@pytest.fixture
def reconcile_harness(hermes_home, tmp_path, monkeypatch):
    """reconcile_service with real probe, fake systemctl, temp unit dir.

    Route discovery's credential-pool read is pinned to empty so a developer
    machine's real pool cannot leak machine-dependent routes into the table;
    the probe itself is NOT mocked — it does real socket and /health work.
    """
    from plugins.auto_update.platform import InstallScope
    from plugins.llm_usage_proxy import routes as routes_mod
    from plugins.llm_usage_proxy import systemd as systemd_mod

    monkeypatch.setattr(systemd_mod, "platform_supported", lambda: True)
    monkeypatch.setattr(routes_mod, "_pool_entry_bases", lambda provider_id: [])

    scope = InstallScope(
        system=False,
        unit_dir=tmp_path / "units",
        systemctl_prefix=("systemctl", "--user"),
    )
    systemctl = _FakeSystemctl()
    return systemd_mod, scope, systemctl, hermes_home, tmp_path


def test_reconcile_enable_free_port_crosses_real_probe(reconcile_harness):
    systemd_mod, scope, systemctl, hermes_home, _ = reconcile_harness
    cfg = {"port": _free_loopback_port()}

    result = systemd_mod.reconcile_service(
        cfg, enabled=True, run_systemctl=systemctl, scope=scope, environ={}
    )

    assert result.supported
    assert result.port.status == "free"
    assert result.unit_installed
    assert result.enabled and result.service_active
    unit_path = systemd_mod.service_unit_path(scope, hermes_home)
    assert unit_path.is_file()
    unit = systemd_mod.service_name(hermes_home)
    enable_calls = [c for c in systemctl.calls if "enable" in c]
    assert enable_calls and all(unit in c for c in enable_calls)


def test_reconcile_crosses_real_probe_with_matching_listener(reconcile_harness):
    """Enabled and disabled paths both reach the real probe carrying this
    profile's identity and route table, and a verifying listener is neither
    adopted, rewritten, nor stopped."""
    systemd_mod, scope, systemctl, hermes_home, tmp_path = reconcile_harness
    from plugins.llm_usage_proxy.routes import build_route_table

    cfg: dict = {"port": 0}
    routes = build_route_table(cfg, environ={})
    server = UsageProxyServer(
        port=0,
        db_path=str(tmp_path / "usage.sqlite"),
        upstreams=routes,
        identity=systemd_mod.profile_identity(hermes_home),
    )
    port = server.server_address[1]
    cfg["port"] = port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Enabled path: probe verifies the listener as ours (identity +
        # routes match), so reconcile stands down instead of installing a
        # competing unit or adopting the running one.
        enabled = systemd_mod.reconcile_service(
            cfg, enabled=True, run_systemctl=systemctl, scope=scope, environ={}
        )
        assert enabled.port.status == "healthy"
        assert enabled.port.occupied
        assert any("already running" in w for w in enabled.warnings)
        assert not systemd_mod.service_unit_path(scope, hermes_home).is_file()
        assert not any("enable" in c for c in systemctl.calls)
        assert not any("stop" in c for c in systemctl.calls)

        # Disabled path: crosses the same verified probe, then stops/disables
        # only this profile's unit — the verifying listener keeps serving.
        systemctl.calls.clear()
        disabled = systemd_mod.reconcile_service(
            cfg, enabled=False, run_systemctl=systemctl, scope=scope, environ={}
        )
        assert disabled.port.status == "healthy"
        assert not disabled.enabled
        assert not disabled.service_active
        unit = systemd_mod.service_name(hermes_home)
        stopped = [c for c in systemctl.calls if "stop" in c]
        disabled_calls = [c for c in systemctl.calls if "disable" in c]
        assert stopped and all(unit in c for c in stopped)
        assert disabled_calls and all(unit in c for c in disabled_calls)

        # The listener itself was never mutated by either control path.
        status, _, body = proxy_request(port, "GET", "/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["identity"] == systemd_mod.profile_identity(hermes_home)
        assert payload["routes"] == routes
    finally:
        server.shutdown()
        server.server_close()
        server.store.close()
