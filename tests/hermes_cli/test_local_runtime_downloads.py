"""Runtime downloads must not turn a transient transfer failure into a poisoned cache."""

import hashlib
import io
import json
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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
def test_download_only_publishes_complete_transfers(tmp_path, asset_server, failure):
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
    responses.append((payload, None))
    ticks = []
    binaries._download(url.format(asset=dest.name), dest,
                       progress=lambda done, total: ticks.append((done, total)))
    assert dest.read_bytes() == payload
    assert ticks[-1] == (len(payload), 0)
    assert len(requests) == 2


@pytest.mark.parametrize("archive_format,corruption", [
    ("zip", "truncated"), ("zip", "crc"), ("tar.gz", "truncated"),
])
@pytest.mark.parametrize("replacement_kind", ["valid", "invalid", "permission"])
def test_install_recovers_corrupt_cache_once(tmp_path, monkeypatch, asset_server,
                                            archive_format, corruption, replacement_kind):
    url, responses, requests = asset_server
    # Use the real cache/root resolution, HTTP transfer, digest and extraction.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    tag = f"b{sys.version_info.major}"
    asset = f"runtime.{archive_format}"
    plan = binaries.AssetPlan(tag, "cpu", [asset])
    monkeypatch.setattr(binaries, "resolve_assets", lambda *args: plan)
    monkeypatch.setattr(binaries, "RELEASE_URL", url)
    # Exercise the real --version subprocess without downloading a native runtime.
    monkeypatch.setattr(binaries, "server_binary", lambda _: Path(sys.executable))
    payload = b"complete runtime payload"
    stream = io.BytesIO()
    if archive_format == "zip":
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("bin/runtime.dat", payload)
    else:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo("bin/runtime.dat")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    valid = stream.getvalue()
    corrupt = (valid.replace(payload, b"!" * len(payload))
               if corruption == "crc" else valid[:10])
    cached = binaries.runtimes_root() / "downloads" / asset
    cached.parent.mkdir(parents=True)
    cached.write_bytes(corrupt)
    replacement = valid if replacement_kind == "valid" else corrupt
    responses.append((replacement, len(replacement)))
    ticks = []

    if replacement_kind == "permission":
        def denied(*args, **kwargs):
            raise PermissionError("destination is not writable")

        monkeypatch.setattr(binaries, "_extract", denied)
        with pytest.raises(PermissionError):
            binaries.ensure_runtime_installed(tag, "cpu")
        assert cached.read_bytes() == corrupt
        assert not requests
        return

    if replacement_kind == "valid":
        installed = binaries.ensure_runtime_installed(tag, "cpu", progress=lambda *p: ticks.append(p))
        assert (installed / "bin/runtime.dat").read_bytes() == payload
        manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["assets"][asset] == hashlib.sha256(valid).hexdigest()
        assert manifest["verified_version"]
        assert cached.read_bytes() == valid
        assert {p[0] for p in ticks} == {"download", "verify", "extract"}
        assert binaries.ensure_runtime_installed(tag, "cpu") == installed
    else:
        with pytest.raises(binaries.BinaryResolutionError, match="archive"):
            binaries.ensure_runtime_installed(tag, "cpu")
        assert not cached.exists()
        assert not (plan.install_dir / "manifest.json").exists()
    assert len(requests) == 1
