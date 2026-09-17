"""Runtime downloads must not turn a transient transfer failure into a poisoned cache."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_cli.local_runtime import binaries


@pytest.fixture
def asset_server():
    responses = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body, length = responses.pop(0)
            self.send_response(200)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/{{asset}}", responses, requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("failure", ["short", "callback"])
@pytest.mark.parametrize("known_length", [True, False])
def test_download_only_publishes_complete_transfers(tmp_path, asset_server, failure, known_length):
    url, responses, requests = asset_server
    dest = tmp_path / "runtime.zip"
    payload = b"runtime archive bytes"
    responses.append((payload[:-1], len(payload)))

    def progress(done, total):
        if failure == "callback":
            raise RuntimeError("cancelled")

    error = RuntimeError if failure == "callback" else binaries.BinaryResolutionError
    with pytest.raises(error):
        binaries._download(url.format(asset=dest.name), dest, progress=progress)
    assert not dest.exists()
    assert not dest.with_suffix(".zip.part").exists()

    # A retry works, including servers that omit Content-Length.
    responses.append((payload, len(payload) if known_length else None))
    ticks = []
    binaries._download(url.format(asset=dest.name), dest,
                       progress=lambda done, total: ticks.append((done, total)))
    assert dest.read_bytes() == payload
    assert ticks[-1] == (len(payload), len(payload) if known_length else 0)
    assert len(requests) == 2
