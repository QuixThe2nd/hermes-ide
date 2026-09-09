"""Shared fixtures: hermeticity guards, fake upstreams, and a real proxy."""

from __future__ import annotations

import http.client
import json
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plugins.llm_usage_proxy.server import UsageProxyServer

REAL_SYSTEMD_PATHS = (
    "/etc/systemd/system",
    "/run/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    str(Path.home() / ".config/systemd"),
    str(Path.home() / ".local/share/systemd"),
    "/var/lib/systemd/timers",
)


@pytest.fixture(autouse=True)
def _block_real_systemd_subprocess(monkeypatch):
    """Fail fast if any llm_usage_proxy test touches real systemctl paths."""

    real_run = subprocess.run

    def guarded_run(args, *run_args, **run_kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        joined = " ".join(str(part) for part in argv)
        if "systemctl" in joined:
            raise AssertionError(
                f"llm_usage_proxy tests must not invoke real systemctl: {joined}"
            )
        for path in REAL_SYSTEMD_PATHS:
            if path in joined:
                raise AssertionError(
                    f"llm_usage_proxy tests must not touch live systemd paths: {joined}"
                )
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    """Start every test from a clean provider-env slate.

    The env-rewrite machinery is the point of this plugin; a developer's
    real GLM_BASE_URL leaking into capture/rewrite assertions would make
    them machine-dependent.
    """
    for var in (
        "GLM_BASE_URL",
        "KIMI_BASE_URL",
        "XAI_BASE_URL",
        "HERMES_CODEX_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    """Records the request, then delegates the response to per-test code."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence — never log headers
        return

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        self.server.respond(self)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle
    do_HEAD = _handle


class _FakeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, respond):
        self.requests = []
        self.respond = respond
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)


class _Proxy(UsageProxyServer):
    """UsageProxyServer carrying its shutdown bookkeeping."""

    def __init__(self, *, db_path, upstreams, identity="", port=0):
        super().__init__(port=port, db_path=db_path, upstreams=upstreams, identity=identity)


@pytest.fixture
def start_upstream():
    """Factory running a recording fake upstream on an ephemeral port."""
    servers: list[_FakeUpstream] = []

    def _start(respond) -> _FakeUpstream:
        server = _FakeUpstream(respond)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def start_proxy(tmp_path):
    """Factory running a real usage proxy on an ephemeral loopback port."""
    servers: list[_Proxy] = []

    def _start(upstreams, *, db_name="usage.sqlite", identity="", port=0) -> _Proxy:
        server = _Proxy(
            db_path=str(tmp_path / db_name),
            upstreams=upstreams,
            identity=identity,
            port=port,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()
        server.store.close()


def respond_json(payload: dict, *, status: int = 200, headers: dict | None = None):
    """Build a fake-upstream responder returning a JSON body."""

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return _respond


def respond_sse(events: list[bytes], *, gate: threading.Event | None = None):
    """Build a fake-upstream responder streaming chunked SSE events.

    When *gate* is given, the responder blocks after the first event until
    the gate opens — that is how tests prove the proxy forwards bytes as
    they arrive instead of buffering the stream.
    """

    def _respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

        def send(chunk: bytes) -> None:
            handler.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")

        for index, event in enumerate(events):
            if index == 1 and gate is not None:
                gate.wait(10)
            send(event)
        handler.wfile.write(b"0\r\n\r\n")

    return _respond


def proxy_request(port: int, method: str, path: str, *, body=None, headers=None):
    """One-shot HTTP request to the proxy; returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def wait_for_row_count(db_path, count: int, timeout: float = 5.0) -> list[dict]:
    """Poll the proxy's SQLite file until *count* rows exist (insert happens
    in the request thread's finally — a client that finished reading can
    still race the INSERT by microseconds)."""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while True:
        if Path(db_path).is_file():
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = [
                    dict(row) for row in conn.execute("SELECT * FROM usage_events")
                ]
            finally:
                conn.close()
        if len(rows) >= count or time.monotonic() >= deadline:
            return rows
        time.sleep(0.05)
