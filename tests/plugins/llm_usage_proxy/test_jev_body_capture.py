"""Opt-in jev body capture: gate, sink, store retention, end-to-end, viewer.

The invariant under test: with ``--capture-jev-bodies`` on, every proxied
request to the jev route with a ``typesafe/jev-*`` model is stored with its
full request JSON and its complete non-streaming response body; with the flag
off (the default) nothing is captured and not even the table exists.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from plugins.llm_usage_proxy.server import (
    CAPTURE_COMPLETE,
    CAPTURE_DECODE_BROKEN_NOTE,
    CAPTURE_INCOMPLETE,
    CAPTURE_INCOMPLETE_NOTE,
    CAPTURE_STREAMED,
    CAPTURE_STREAMED_NOTE,
    CAPTURE_TRUNCATED,
    JEV_CAPTURE_UPSTREAM,
    JevBodyCapture,
    OUTCOME_ABORTED,
    OUTCOME_COMPLETED,
    OUTCOME_UPSTREAM_ERROR,
    UsageStore,
    should_capture_jev_body,
)

from conftest import proxy_request, respond_json, respond_sse, wait_for_row_count

JEV_MODEL = "typesafe/jev-1.13-20260917"


def _openrouter(upstream) -> dict[str, str]:
    """Route table naming the fake upstream as the jev route."""
    return {
        JEV_CAPTURE_UPSTREAM: f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    }


def _jev_body(model: str = JEV_MODEL, **extra) -> bytes:
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def _jev_rows(db_path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM jev_bodies")]
    finally:
        conn.close()


def _wait_for_jev_rows(db_path, count: int, timeout: float = 5.0) -> list[dict]:
    """Poll until *count* jev_bodies rows exist (the insert happens in the
    request thread's finally — see wait_for_row_count for the same race)."""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while True:
        if Path(db_path).is_file() and "jev_bodies" in _table_names(db_path):
            rows = _jev_rows(db_path)
        if len(rows) >= count or time.monotonic() >= deadline:
            return rows
        time.sleep(0.05)


# ── 1. the gate: jev route AND jev model family, nothing else ────────────────


def test_gate_needs_flag_route_and_model_prefix():
    assert should_capture_jev_body(True, "openrouter-alpha", "typesafe/jev-1")
    assert should_capture_jev_body(True, "openrouter-alpha", JEV_MODEL)
    # Case-insensitive on the model prefix.
    assert should_capture_jev_body(True, "openrouter-alpha", "Typesafe/JEV-9")


def test_gate_rejects_everything_else():
    assert not should_capture_jev_body(False, "openrouter-alpha", JEV_MODEL)
    assert not should_capture_jev_body(True, "zai", JEV_MODEL)
    assert not should_capture_jev_body(True, "openrouter-alpha", "gpt-5")
    assert not should_capture_jev_body(True, "openrouter-alpha", "typesafe/other-1")
    # "typesafe/jev" without the dash is a different family.
    assert not should_capture_jev_body(True, "openrouter-alpha", "typesafe/jev")
    assert not should_capture_jev_body(True, "openrouter-alpha", None)
    assert not should_capture_jev_body(True, "openrouter-alpha", 42)


# ── 2. the sink: bounded accumulation with honest markers ────────────────────


def test_sink_accumulates_and_reports_complete():
    sink = JevBodyCapture(cap=100)
    sink.feed(b'{"choices": ')
    sink.feed(b'[{"delta": "hi"}]}')
    assert sink.state(OUTCOME_COMPLETED) == CAPTURE_COMPLETE
    assert sink.response_text() == '{"choices": [{"delta": "hi"}]}'
    assert sink.request_text(b'{"model": "x"}') == '{"model": "x"}'


def test_sink_truncates_at_cap_with_explicit_marker():
    sink = JevBodyCapture(cap=8)
    sink.feed(b"0123456789abcdef")  # 16 bytes into an 8-byte cap
    assert sink.state(OUTCOME_COMPLETED) == CAPTURE_TRUNCATED
    text = sink.response_text()
    assert text.startswith("01234567")
    assert "8-byte" in text and "truncated" in text
    # Nothing past the cap is ever held, even after more feeding.
    sink.feed(b"more")
    assert sink.response_text(OUTCOME_COMPLETED).startswith("01234567")
    # Request bodies share the cap and the marker.
    request = sink.request_text(b"x" * 20)
    assert request.startswith("xxxxxxxx")
    assert "truncated" in request


def test_sink_streamed_holds_nothing_and_says_so():
    sink = JevBodyCapture(cap=100)
    sink.feed(b"data: {\"token\": 1}\n\n")
    sink.mark_streamed()
    sink.feed(b"data: {\"token\": 2}\n\n")  # ignored after the marker
    assert sink.state(OUTCOME_COMPLETED) == CAPTURE_STREAMED
    assert sink.state(OUTCOME_ABORTED) == CAPTURE_STREAMED
    assert sink.response_text() == CAPTURE_STREAMED_NOTE


def test_sink_marks_incomplete_on_abort_and_decode_failure():
    sink = JevBodyCapture(cap=100)
    sink.feed(b'{"partial": ')
    assert sink.state(OUTCOME_ABORTED) == CAPTURE_INCOMPLETE
    assert CAPTURE_INCOMPLETE_NOTE in sink.response_text(OUTCOME_ABORTED)

    broken = JevBodyCapture(cap=100)
    broken.feed(b"ok")
    broken.mark_decode_broken()
    assert broken.state(OUTCOME_COMPLETED) == CAPTURE_INCOMPLETE
    assert CAPTURE_DECODE_BROKEN_NOTE in broken.response_text(OUTCOME_COMPLETED)


# ── 3. end-to-end through the real proxy ─────────────────────────────────────


def test_default_off_captures_nothing_and_creates_no_table(
    tmp_path, start_upstream, start_proxy
):
    upstream = start_upstream(
        respond_json({"choices": [], "usage": {"total_tokens": 1}})
    )
    proxy = start_proxy(_openrouter(upstream), db_name="off.sqlite")
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=_jev_body(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    db = str(tmp_path / "off.sqlite")
    assert len(wait_for_row_count(db, 1)) == 1  # ledger row still lands
    assert "jev_bodies" not in _table_names(db)  # and no capture table exists


def test_capture_on_stores_complete_non_streaming_pair(
    tmp_path, start_upstream, start_proxy
):
    response_payload = {
        "id": "resp-1",
        "model": JEV_MODEL,
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    upstream = start_upstream(respond_json(response_payload))
    proxy = start_proxy(
        _openrouter(upstream), db_name="on.sqlite", capture_jev_bodies=True
    )
    request_body = _jev_body(stream=False)
    status, headers, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=request_body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert body == json.dumps(response_payload).encode("utf-8")  # relay untouched
    db = str(tmp_path / "on.sqlite")
    rows = _wait_for_jev_rows(db, 1)
    assert len(rows) == 1
    row = rows[0]
    assert row["capture_state"] == CAPTURE_COMPLETE
    assert row["upstream"] == "openrouter-alpha"
    assert row["model"] == JEV_MODEL
    assert row["path"] == "chat/completions"  # routed path, as the ledger stores it
    assert row["status_code"] == 200
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
    assert row["request_body"] == request_body.decode("utf-8")
    assert row["response_body"] == json.dumps(response_payload)
    assert row["ts"] and row["created_at"]


def test_capture_on_stores_request_plus_streamed_marker_for_sse(
    tmp_path, start_upstream, start_proxy
):
    events = [
        b'data: {"model": "%s", "choices": [{"delta": "he"}]}\n\n' % JEV_MODEL.encode(),
        b'data: {"choices": [{"delta": "llo"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    upstream = start_upstream(respond_sse(events))
    proxy = start_proxy(
        _openrouter(upstream), db_name="sse.sqlite", capture_jev_bodies=True
    )
    request_body = _jev_body(stream=True)
    status, _, body = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=request_body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert b"[DONE]" in body
    db = str(tmp_path / "sse.sqlite")
    rows = _wait_for_jev_rows(db, 1)
    assert len(rows) == 1
    row = rows[0]
    assert row["capture_state"] == CAPTURE_STREAMED
    assert row["response_body"] == CAPTURE_STREAMED_NOTE
    assert b"data:" not in row["response_body"].encode("utf-8")
    # The stored request is the forwarded one: stream_options was injected.
    stored = json.loads(row["request_body"])
    assert stored["stream"] is True
    assert stored["stream_options"] == {"include_usage": True}


def test_non_jev_route_or_model_is_not_captured(
    tmp_path, start_upstream, start_proxy
):
    upstream = start_upstream(
        respond_json({"choices": [], "usage": {"total_tokens": 1}})
    )
    port = upstream.server_address[1]
    proxy = start_proxy(
        {
            "openrouter-alpha": f"http://127.0.0.1:{port}/v1",
            "zai": f"http://127.0.0.1:{port}/api",
        },
        db_name="mixed.sqlite",
        capture_jev_bodies=True,
    )
    p = proxy.server_address[1]
    for path, body in (
        ("/p/zai/chat/completions", _jev_body()),  # jev model, wrong route
        ("/p/openrouter-alpha/chat/completions", _jev_body(model="gpt-5")),
        ("/p/openrouter-alpha/chat/completions", None),  # no body, no model
    ):
        status, _, _ = proxy_request(
            p, "POST", path, body=body, headers={"Content-Type": "application/json"}
        )
        assert status == 200
    db = str(tmp_path / "mixed.sqlite")
    assert len(wait_for_row_count(db, 3)) == 3  # all three hit the ledger
    # The table exists (the flag created it) but stays empty: nothing met the
    # route+model gate.
    assert _jev_rows(db) == []


def test_case_insensitive_jev_model_is_captured(
    tmp_path, start_upstream, start_proxy
):
    upstream = start_upstream(respond_json({"choices": []}))
    proxy = start_proxy(
        _openrouter(upstream), db_name="case.sqlite", capture_jev_bodies=True
    )
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=_jev_body(model="Typesafe/JEV-9"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    rows = _wait_for_jev_rows(str(tmp_path / "case.sqlite"), 1)
    assert rows[0]["model"] == "Typesafe/JEV-9"


def test_failed_upstream_stores_request_with_incomplete_marker(
    tmp_path, start_proxy
):
    # Port 1 on loopback: connection refused, fast and deterministic.
    proxy = start_proxy(
        {"openrouter-alpha": "http://127.0.0.1:1/v1"},
        db_name="refused.sqlite",
        capture_jev_bodies=True,
    )
    request_body = _jev_body()
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=request_body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 502
    rows = _wait_for_jev_rows(str(tmp_path / "refused.sqlite"), 1)
    assert len(rows) == 1
    row = rows[0]
    assert row["capture_state"] == CAPTURE_INCOMPLETE
    assert row["status_code"] is None
    assert row["request_body"] == request_body.decode("utf-8")
    assert CAPTURE_INCOMPLETE_NOTE in row["response_body"]


# ── 4. the store: retention window and row cap ───────────────────────────────


def test_store_prunes_rows_past_the_retention_window(tmp_path):
    db = str(tmp_path / "retention.sqlite")
    store = UsageStore(db, capture_jev_bodies=True)
    try:
        store.insert_jev_body(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            upstream="openrouter-alpha",
            model=JEV_MODEL,
            path="/chat/completions",
            request_id=None,
            status_code=200,
            latency_ms=5,
            capture_state=CAPTURE_COMPLETE,
            request_body="{}",
            response_body="{}",
        )
        stale = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat(timespec="milliseconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE jev_bodies SET ts = ?", (stale,))
        conn.commit()
        conn.close()
        store.insert_jev_body(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            upstream="openrouter-alpha",
            model=JEV_MODEL,
            path="/chat/completions",
            request_id=None,
            status_code=200,
            latency_ms=5,
            capture_state=CAPTURE_COMPLETE,
            request_body="{}",
            response_body="{}",
        )
        rows = _jev_rows(db)
        assert len(rows) == 1
        assert rows[0]["ts"] != stale  # the survivor is the fresh row
    finally:
        store.close()


def test_store_caps_total_rows_keeping_the_newest(tmp_path):
    db = str(tmp_path / "cap.sqlite")
    store = UsageStore(db, capture_jev_bodies=True, capture_max_rows=3)
    try:
        for i in range(5):
            store.insert_jev_body(
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                upstream="openrouter-alpha",
                model=JEV_MODEL,
                path=f"/c/{i}",
                request_id=None,
                status_code=200,
                latency_ms=i,
                capture_state=CAPTURE_COMPLETE,
                request_body="{}",
                response_body="{}",
            )
        rows = _jev_rows(db)
        assert len(rows) == 3
        assert sorted(row["path"] for row in rows) == ["/c/2", "/c/3", "/c/4"]
    finally:
        store.close()


# ── 5. the webui viewer: list, detail, escaping, 404s ────────────────────────

WEBUI_SERVER = (
    Path(__file__).resolve().parents[3] / "apps" / "usage-proxy-webui" / "server.py"
)


def _load_webui():
    spec = importlib.util.spec_from_file_location("usage_proxy_webui_server", WEBUI_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def start_webui():
    """Serve the real webui on an ephemeral loopback port against one DB."""
    module = _load_webui()
    servers: list[ThreadingHTTPServer] = []

    def _start(db_path) -> int:
        module.UsageProxyHandler.db_path = str(db_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.UsageProxyHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()


def _capture_one_bad_pair(tmp_path, start_upstream, start_proxy) -> str:
    """Proxy one jev request whose bodies carry HTML, return the DB path."""
    hostile = '<script>alert("xss")</script>'
    upstream = start_upstream(respond_json({"error": hostile}))
    proxy = start_proxy(
        _openrouter(upstream), db_name="webui.sqlite", capture_jev_bodies=True
    )
    status, _, _ = proxy_request(
        proxy.server_address[1],
        "POST",
        "/p/openrouter-alpha/chat/completions",
        body=_jev_body(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    db = str(tmp_path / "webui.sqlite")
    assert len(_wait_for_jev_rows(db, 1)) == 1
    return db


def test_webui_lists_and_escapes_captures(tmp_path, start_upstream, start_proxy, start_webui):
    db = _capture_one_bad_pair(tmp_path, start_upstream, start_proxy)
    port = start_webui(db)

    status, html = _get(port, "/captures")
    assert status == 200
    assert "jev-1.13" in html  # model shown (display name strips the suffix)
    assert 'href="/captures/1"' in html  # detail link
    assert "chat/completions" in html  # routed path column

    status, html = _get(port, "/captures/1")
    assert status == 200
    assert "&lt;script&gt;" in html  # bodies escaped for display
    assert "<script" not in html  # and never raw
    assert '"role"' in html or "&quot;role&quot;" in html  # pretty-printed request
    assert "body-block" in html  # monospace body block rendered


def test_webui_404s_and_empty_states(tmp_path, start_upstream, start_proxy, start_webui):
    db = _capture_one_bad_pair(tmp_path, start_upstream, start_proxy)
    port = start_webui(db)
    assert _get(port, "/captures/999")[0] == 404  # well-formed but absent
    assert _get(port, "/captures/abc")[0] == 404  # malformed id
    # The dashboard links to the viewer.
    status, html = _get(port, "/")
    assert status == 200
    assert 'href="/captures"' in html

    # A DB whose proxy never enabled capture: empty viewer, not an error.
    empty = tmp_path / "empty.sqlite"
    store = UsageStore(str(empty))  # default-off: no jev_bodies table
    store.close()
    port2 = start_webui(str(empty))
    status, html = _get(port2, "/captures")
    assert status == 200
    assert "jev_bodies" in html  # the card explains why nothing is listed
    status, html = _get(port2, "/captures/1")
    assert status == 404


def test_build_exec_start_argv_carries_capture_flag_only_when_enabled(
    monkeypatch, tmp_path
):
    from plugins.llm_usage_proxy import systemd
    from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config

    monkeypatch.setattr(systemd, "profile_identity", lambda home=None: "ident")
    monkeypatch.setattr(systemd, "build_route_table", lambda cfg, environ=None: {})
    monkeypatch.setattr(
        "plugins.auto_update.platform.resolve_python_executable",
        lambda: "/usr/bin/python3",
    )

    cfg = dict(
        load_llm_usage_proxy_config({"capture_jev_bodies": True, "port": 8790})
    )
    argv = systemd.build_exec_start_argv(cfg, hermes_home=tmp_path)
    assert "--capture-jev-bodies" in argv

    plain = systemd.build_exec_start_argv(
        dict(load_llm_usage_proxy_config({"port": 8790})), hermes_home=tmp_path
    )
    assert "--capture-jev-bodies" not in plain
