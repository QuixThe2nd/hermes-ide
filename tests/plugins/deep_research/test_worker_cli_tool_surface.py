"""Installed-process boundary: the no-file worker surface through the real CLI.

The FakeSpawn tests in test_runner_flow.py pin the worker ARGV; this module
runs that argv for real. Each test executes the actual worker CLI
(``python -m hermes_cli.main chat -Q --query-file … -t …``, built by the
runner's own ``build_worker_argv``) as a subprocess against a fake
OpenAI-compatible provider, from a temporary Hermes home whose ``researcher``
profile carries the YAML bool ``deep_research.worker_file_tools: false``.
The fake provider records every request's ``tools`` array — the actual model
tool definitions — so these tests inspect the tool surface the model would
really see, covering config propagation, CLI ``-t`` handling, plugin
discovery, and empty-toolset resolution end to end. No provider or network
spend: the only endpoint is a loopback ``http.server``.

Coverage:
  - lane ``-t web,browser``: web/browser surface only, no ``read_file`` or
    ``search_files`` (or any write/terminal tool);
  - writer ``-t research_writer``: zero model tools, even with a registry
    overlay (a user plugin) targeting ``research_writer`` in the child;
  - plugin discovery registers the plugin-owned toolset in the child: the
    strict one-shot validator (``-z``) accepts ``research_writer`` only
    because the bundled deep_research plugin loads and registers it — with
    the plugin disabled the same invocation fails closed before any provider
    contact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plugins.deep_research import jobs
from plugins.deep_research.config import load_deep_research_config
from plugins.deep_research.runner import (
    NO_FILE_LANE_TOOLSETS,
    NO_FILE_WRITER_TOOLSETS,
    build_worker_argv,
    load_frozen_request,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess env plumbing")

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_BASE_ARGV = [sys.executable, "-m", "hermes_cli.main"]
PROFILE = "researcher"
# Boundary assertions: these must never reach a no-file worker's schema.
FILE_TOOLS = {"read_file", "write_file", "patch", "search_files"}
# The overlay plugin registers one tool into ``web`` (must show up in the
# lane run — proving overlays really register in the child) and one into the
# sealed ``research_writer`` (must NEVER show up in the writer run).
OVERLAY_WEB_TOOL = "leak_probe_web"
OVERLAY_WRITER_TOOL = "leak_probe_research_writer"

_FAKE_TEXT = "REPORT BODY"


class _FakeProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible stub: records request bodies, answers fixed text."""

    captured: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured.append(req)
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": _FAKE_TEXT}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            resp = {
                "id": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": _FAKE_TEXT}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — model-catalog probes, if any
        body = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):  # silence default stderr logging
        pass


@pytest.fixture()
def fake_provider():
    _FakeProvider.captured = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


_OVERLAY_PLUGIN_INIT = f'''\
def register(ctx):
    schema = {{"name": "{OVERLAY_WRITER_TOOL}", "description": "overlay probe", "parameters": {{"type": "object", "properties": {{}}}}}}
    ctx.register_tool(
        name="{OVERLAY_WRITER_TOOL}",
        toolset="research_writer",
        schema=schema,
        handler=lambda args, **kw: "{{}}",
    )
    ctx.register_tool(
        name="{OVERLAY_WEB_TOOL}",
        toolset="web",
        schema={{"name": "{OVERLAY_WEB_TOOL}", "description": "overlay probe", "parameters": {{"type": "object", "properties": {{}}}}}},
        handler=lambda args, **kw: "{{}}",
    )
'''


def _profile_config(port: int, *, overlay: bool = True, extra: str = "") -> str:
    plugins_section = (
        "plugins:\n  enabled: [leak_overlay]\n"
        if overlay
        else ""
    )
    return (
        "model:\n"
        "  default: fake-model\n"
        "  provider: 'custom:fake'\n"
        "  base_url: ''\n"
        "custom_providers:\n"
        "  - name: fake\n"
        f"    base_url: 'http://127.0.0.1:{port}/v1'\n"
        "    model: fake-model\n"
        "    api_key: test-key\n"
        # Keep every resolved tool directly visible in the request schema —
        # the tool-search bridge (auto mode) would otherwise defer part of
        # the surface behind tool_search and weaken the assertions below.
        "tools:\n"
        "  tool_search: false\n"
        # The YAML bool under test: a real ``false``, never a string.
        "deep_research:\n"
        "  worker_file_tools: false\n"
        f"{plugins_section}"
        f"{extra}"
    )


@pytest.fixture()
def home(tmp_path: Path, fake_provider: int) -> Path:
    """A temp Hermes home with a ``researcher`` profile configured for the
    fake provider, ``worker_file_tools: false``, and an overlay user plugin
    that targets both ``web`` and the sealed ``research_writer`` toolset."""
    home = tmp_path / "home"
    profile = home / "profiles" / PROFILE
    profile.mkdir(parents=True)
    config = _profile_config(fake_provider)
    (home / "config.yaml").write_text(config, encoding="utf-8")
    (profile / "config.yaml").write_text(config, encoding="utf-8")
    overlay = profile / "plugins" / "leak_overlay"
    overlay.mkdir(parents=True)
    (overlay / "plugin.yaml").write_text('name: leak_overlay\nversion: 0.1.0\n', encoding="utf-8")
    (overlay / "__init__.py").write_text(_OVERLAY_PLUGIN_INIT, encoding="utf-8")
    return home


def _child_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    # HERMES_HOME stays set (unlike the runner's worker_env, which drops it):
    # the profile must resolve under OUR temp home, not the operator's
    # default ~/.hermes. ``-p researcher`` still owns profile selection.
    env["HERMES_HOME"] = str(home)
    env["PYTHONUNBUFFERED"] = "1"
    # A present-but-fake search key so check_web_api_key() passes and the
    # web tools actually reach the schema; the fake model never calls them.
    env["TAVILY_API_KEY"] = "test-key"
    return env


def _run_worker(home: Path, argv: list) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell
        [str(part) for part in argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=_child_env(home),
        timeout=240,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _tool_names(req: dict) -> set:
    return {t["function"]["name"] for t in req.get("tools") or []}


def _captured_tool_names() -> list:
    return [_tool_names(req) for req in _FakeProvider.captured]


def _prompt_file(home: Path, name: str) -> Path:
    path = home / f"{name}.md"
    path.write_text("Answer briefly.", encoding="utf-8")
    return path


def _port_of(home: Path) -> int:
    """Recover the fake provider port recorded in the profile config."""
    import re

    text = (home / "profiles" / PROFILE / "config.yaml").read_text(encoding="utf-8")
    match = re.search(r"127\.0\.0\.1:(\d+)", text)
    assert match, "fake provider port not found in profile config"
    return int(match.group(1))


class TestNoFileWorkerToolSurface:
    def test_config_freezes_yaml_bool_false(self, home: Path, monkeypatch) -> None:
        """Config propagation: the YAML bool loads as a real False and
        survives the frozen-request round trip the runner performs."""
        monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / PROFILE))
        config = load_deep_research_config()
        assert config.worker_file_tools is False
        created = jobs.create_job(
            brief="probe",
            research_questions=None,
            timeout_minutes=5,
            max_parallel=1,
            worker_profile=PROFILE,
            worker_file_tools=config.worker_file_tools,
            hermes_home=home / "profiles" / PROFILE,
        )
        frozen = load_frozen_request(created["dir"])
        assert frozen["worker_file_tools"] is False

    def test_lane_cli_surface_is_web_browser_only(self, home: Path) -> None:
        argv = build_worker_argv(
            WORKER_BASE_ARGV, PROFILE, _prompt_file(home, "lane"),
            toolsets=NO_FILE_LANE_TOOLSETS,
        )
        proc = _run_worker(home, argv)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert _FakeProvider.captured, "worker never contacted the provider"

        names = set().union(*_captured_tool_names())
        # Web retrieval really is on the surface (not vacuously empty)…
        assert {"web_search", "web_extract"} <= names
        # …and the overlay tool the user plugin registered into ``web`` made
        # it through plugin discovery in the child — proving overlays DO
        # register there, so the writer-side absence below is the seal
        # holding, not a discovery no-op.
        assert OVERLAY_WEB_TOOL in names
        # Nothing outside the web/browser surface (plus the plugin-owned web
        # overlays that legitimately merge into it: our probe tool and the
        # deep_research plugin's own delegate_research, whose presence also
        # proves the plugin loaded in the child), and no filesystem tools.
        from toolsets import resolve_toolset

        surface = set(resolve_toolset("web", include_registry=False)) | set(
            resolve_toolset("browser", include_registry=False)
        )
        assert names - surface <= {OVERLAY_WEB_TOOL, "delegate_research"}
        assert not names & FILE_TOOLS
        assert not names & {"terminal", "process", "execute_code"}

    def test_writer_cli_surface_is_empty_under_sealed_toolset(self, home: Path) -> None:
        argv = build_worker_argv(
            WORKER_BASE_ARGV, PROFILE, _prompt_file(home, "writer"),
            toolsets=NO_FILE_WRITER_TOOLSETS,
        )
        proc = _run_worker(home, argv)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert _FakeProvider.captured, "writer never contacted the provider"
        # The toolset validated (plugin discovery registered it): the CLI
        # never warned about an unknown toolset.
        assert "Unknown toolsets" not in proc.stdout + proc.stderr
        # Every request — main turn and auxiliaries — carried zero tools,
        # even though the overlay plugin registered a tool INTO
        # research_writer in this same child: the seal ignored it.
        for names in _captured_tool_names():
            assert names == set(), names

    def test_plugin_discovery_makes_writer_toolset_valid_in_child(self, home: Path) -> None:
        """Strict one-shot validation (``-z``) accepts ``research_writer``
        only because plugin discovery registers it; it then resolves empty."""
        proc = _run_worker(
            home,
            WORKER_BASE_ARGV + ["-p", PROFILE, "-z", "Answer briefly.", "-t", NO_FILE_WRITER_TOOLSETS],
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert _FakeProvider.captured, "provider never contacted"
        for names in _captured_tool_names():
            assert names == set(), names

    def test_disabled_plugin_makes_writer_toolset_invalid_in_child(self, home: Path) -> None:
        """Plugin disabled → not advertised, not valid: the same ``-z``
        invocation fails closed BEFORE any provider contact.

        The overlay plugin is deliberately NOT enabled here: a tool
        registered into the ``research_writer`` name would make it a valid
        (ordinary, unsealed) plugin toolset on its own, which is legal — the
        boundary under test is deep_research's absence, not the overlay's."""
        profile_cfg = home / "profiles" / PROFILE / "config.yaml"
        profile_cfg.write_text(
            _profile_config(
                _port_of(home), overlay=False, extra="plugins:\n  disabled: [deep_research]\n"
            ),
            encoding="utf-8",
        )
        proc = _run_worker(
            home,
            WORKER_BASE_ARGV + ["-p", PROFILE, "-z", "Answer briefly.", "-t", NO_FILE_WRITER_TOOLSETS],
        )
        assert proc.returncode != 0
        assert "did not contain any valid toolsets" in proc.stderr
        assert not _FakeProvider.captured
