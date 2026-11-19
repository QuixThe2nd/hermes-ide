"""The isolated local_embedded Hindsight runtime (embedded_runtime).

Real imports of the plugin modules; the only fakes sit at the subprocess
boundary (the uv bridge invocations and the side-python bridge) — the URL the
daemon reports is proven against a real loopback HTTP /health server, never a
constant port. The boot-selected main environment is never touched.
"""

import http.server
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

import plugins.memory.hindsight.embedded_runtime as rt

_STATE = rt._STATE_FILE


@pytest.fixture()
def side_root(tmp_path, monkeypatch):
    root = tmp_path / "hermes-home" / "profiles" / "Hindsight" / "env"
    monkeypatch.setattr(rt, "sideenv_root", lambda: root)
    return root


def _fake_completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# -- pinned pyproject ---------------------------------------------------------


def test_sideenv_pyproject_pins_exact_pair():
    text = rt.sideenv_pyproject()
    assert 'hindsight-embed==0.9.2' in text
    assert 'hindsight-api-slim[all]==0.9.2' in text  # [all]: full feature parity
    assert 'requires-python = ">=3.11"' in text


# -- ensure_sideenv: candidate build -> atomic commit via the pm uv bridge ----


def test_ensure_sideenv_builds_and_publishes_generation(side_root, monkeypatch):
    calls = []

    def fake_bridge(venv):
        calls.append(("bridge", venv))
        return "uv-bin", {"VIRTUAL_ENV": str(venv)}

    def fake_run(uv_bin, env, args, timeout):
        calls.append(("run", [uv_bin, *args], env))
        return _fake_completed()

    monkeypatch.setattr(rt, "_uv_bridge", fake_bridge)
    monkeypatch.setattr(rt, "_run_uv", fake_run)

    gen = rt.ensure_sideenv()

    # The generation was built at its FINAL path (no rename), published by the
    # selection record, and no candidate/prev leftovers exist.
    assert gen.is_dir() and gen.name.startswith("gen-")
    assert rt.sideenv_pyproject() in (gen / "pyproject.toml").read_text(encoding="utf-8")
    assert json.loads((gen / _STATE).read_text(encoding="utf-8"))["pins"] == rt._EXPECTED
    assert json.loads((side_root / "active.json").read_text(encoding="utf-8"))["generation"] == gen.name
    assert [p for p in side_root.iterdir() if p.is_dir()] == [gen]
    # uv invoked for lock+sync against the generation's own venv.
    runs = [c for c in calls if c[0] == "run"]
    assert [c[1][1] for c in runs] == ["lock", "sync"]
    assert all(c[2]["VIRTUAL_ENV"].endswith(".venv") for c in runs)
    assert calls[0][1] == gen / ".venv"


def test_ensure_sideenv_short_circuits_when_current(side_root, monkeypatch):
    gen = side_root / "gen-1"
    gen.mkdir(parents=True)
    (side_root / "active.json").write_text(json.dumps({"generation": "gen-1", "pins": rt._EXPECTED}), encoding="utf-8")
    (gen / _STATE).write_text(json.dumps({"pins": rt._EXPECTED}), encoding="utf-8")
    from hermes_constants import venv_python_path

    py = venv_python_path(gen / ".venv")
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")
    monkeypatch.setattr(rt, "_uv_bridge", lambda venv: pytest.fail("bridge used for an up-to-date env"))
    assert rt.ensure_sideenv() == gen


def test_ensure_sideenv_failure_preserves_previous_generation(side_root, monkeypatch):
    old = side_root / "gen-old"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("old", encoding="utf-8")
    (side_root / "active.json").write_text(json.dumps({"generation": "gen-old"}), encoding="utf-8")

    def boom(*a, **kw):
        raise RuntimeError("uv sync failed: disk full")

    monkeypatch.setattr(rt, "_uv_bridge", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        rt.ensure_sideenv()
    # Previous generation untouched and still the published one; the failed
    # unpublished generation is gone.
    assert (old / "keep.txt").read_text(encoding="utf-8") == "old"
    assert json.loads((side_root / "active.json").read_text(encoding="utf-8"))["generation"] == "gen-old"
    assert [p for p in side_root.iterdir() if p.is_dir()] == [old]


# -- runtime probe: side interpreter, never the boot env ----------------------


def test_check_local_runtime_probes_the_side_python(side_root, monkeypatch):
    py = _install_side_python(side_root)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _fake_completed()

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    assert rt.check_local_runtime() == (True, None)
    assert seen["cmd"][0] == str(py)
    # The probe covers the API + manager + embedding stack (old-CPU NumPy class).
    assert "hindsight_embed.daemon_embed_manager" in seen["cmd"][2]
    assert "hindsight_api" in seen["cmd"][2]
    assert "sentence_transformers" in seen["cmd"][2]


def test_check_local_runtime_reports_probe_failure(side_root, monkeypatch):
    _install_side_python(side_root)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda cmd, **kw: _fake_completed(returncode=1, stderr="Illegal instruction (core dumped)"),
    )
    ok, reason = rt.check_local_runtime()
    assert ok is False
    assert "Illegal instruction" in reason


def test_check_local_runtime_missing_env_names_the_fix(side_root):
    ok, reason = rt.check_local_runtime()
    assert ok is False
    assert "hermes memory setup" in reason
    assert str(side_root) in reason


# -- daemon bridge: URL from the side env, loopback-proven --------------------


class _LoopbackHealth(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture()
def loopback_health():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _install_side_python(side_root, name="gen-1"):
    """A minimal 'installed' generation: selection record + interpreter file
    (laid out by the canonical venv_python_path, so the test matches the
    platform layout the runtime itself resolves)."""
    from hermes_constants import venv_python_path

    gen = side_root / name
    py = venv_python_path(gen / ".venv")
    py.parent.mkdir(parents=True, exist_ok=True)
    (side_root / "active.json").write_text(json.dumps({"generation": name}), encoding="utf-8")
    py.write_bytes(b"")
    return py


def test_ensure_daemon_url_from_sideenv_output_is_reachable(side_root, monkeypatch, loopback_health):
    """The bridge subprocess reports the URL the side env resolved (here: a real
    loopback /health server) — the plugin never invents a port."""
    py = _install_side_python(side_root)
    seen = {}

    def fake_run(cmd, *, env=None, **kw):
        seen["cmd"], seen["env"] = list(cmd), env
        # Manager noise on stdout; the marked JSON line is the authoritative tail.
        return _fake_completed(
            stdout="  Loading profile hermes\n"
                   f"{rt._BRIDGE_MARKER}" + json.dumps({"ok": True, "url": loopback_health})
        )

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    url = rt.ensure_daemon_and_url({"profile": "hermes"})
    assert url == loopback_health
    # E2E: the reported daemon answers /health 200 on the real loopback.
    import urllib.request

    with urllib.request.urlopen(url + "/health", timeout=5) as resp:
        assert resp.status == 200
    assert seen["cmd"][0] == str(py)
    assert seen["cmd"][3] == "hermes"


def test_ensure_daemon_env_carries_grace_and_version_pin(side_root, monkeypatch, loopback_health):
    _install_side_python(side_root)
    captured = {}

    def fake_run(cmd, *, env=None, **kw):
        captured["env"] = env
        return _fake_completed(stdout=f"{rt._BRIDGE_MARKER}" + json.dumps({"ok": True, "url": loopback_health}))

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    monkeypatch.delenv("HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT", raising=False)
    monkeypatch.delenv("HINDSIGHT_EMBED_API_VERSION", raising=False)

    rt.ensure_daemon_and_url({"profile": "hermes", "port_health_grace_timeout": 60})

    # The grace window is read at IMPORT time of the side manager process, so it
    # rides the subprocess env (not Hermes' own environment).
    assert captured["env"]["HINDSIGHT_EMBED_PORT_HEALTH_GRACE_TIMEOUT"] == "60.0"
    assert captured["env"]["HINDSIGHT_EMBED_API_VERSION"] == "0.9.2"
    # Main-env interpreter markers are stripped so the side python resolves only
    # its own environment.
    assert "VIRTUAL_ENV" not in captured["env"]
    assert "PYTHONPATH" not in captured["env"]


def test_ensure_daemon_missing_runtime_raises_with_hint(side_root, monkeypatch):
    with pytest.raises(RuntimeError, match="hermes memory setup"):
        rt.ensure_daemon_and_url({"profile": "hermes"})


def test_ensure_daemon_bridge_failure_surfaces(side_root, monkeypatch):
    _install_side_python(side_root)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda cmd, **kw: _fake_completed(returncode=1, stderr="boom inside side env"),
    )
    with pytest.raises(RuntimeError, match="boom inside side env"):
        rt.ensure_daemon_and_url({"profile": "hermes"})


def test_ensure_daemon_not_ok_surfaces_bridge_error(side_root, monkeypatch):
    _install_side_python(side_root)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda cmd, **kw: _fake_completed(
            stdout=f"{rt._BRIDGE_MARKER}" + json.dumps({"ok": False, "url": None, "error": "daemon did not start"})),
    )
    with pytest.raises(RuntimeError, match="daemon did not start"):
        rt.ensure_daemon_and_url({"profile": "hermes"})


# -- local hint ---------------------------------------------------------------


def test_local_runtime_hint_points_at_isolated_install(side_root):
    hint = rt._local_runtime_hint("No module named 'hindsight'")
    assert "hermes memory setup" in hint
    assert "hindsight-embed==0.9.2" in hint
    assert "hindsight-api-slim[all]==0.9.2" in hint
    assert str(side_root) in hint
    # The main Hermes environment is never the target of the fix.
    assert "uv pip install --python" not in hint


# -- trace logging on every local_embedded path -------------------------------


def test_trace_logs_on_daemon_start_and_ready(side_root, monkeypatch, caplog, loopback_health):
    _install_side_python(side_root)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda cmd, **kw: _fake_completed(stdout=f"{rt._BRIDGE_MARKER}" + json.dumps({"ok": True, "url": loopback_health})),
    )
    with caplog.at_level("INFO", logger="plugins.memory.hindsight.embedded_runtime"):
        rt.ensure_daemon_and_url({"profile": "hermes"})
    messages = [r.getMessage() for r in caplog.records]
    assert any("starting side-env daemon manager" in m for m in messages)
    assert any("daemon ready" in m for m in messages)


def test_trace_logs_on_startup_failure(side_root, monkeypatch, caplog):
    _install_side_python(side_root)
    monkeypatch.setattr(
        rt.subprocess, "run",
        lambda cmd, **kw: _fake_completed(returncode=1, stderr="no dice"),
    )
    with caplog.at_level("WARNING", logger="plugins.memory.hindsight.embedded_runtime"):
        with pytest.raises(RuntimeError):
            rt.ensure_daemon_and_url({"profile": "hermes"})
    assert any("bridge failed" in r.getMessage() for r in caplog.records)


def test_no_in_process_import_of_the_embedded_stack():
    # The isolation contract: importing the plugin must not pull the heavy
    # embedded stack into the boot env (it lives only in the side env).
    import sys

    import plugins.memory.hindsight  # noqa: F401

    assert "hindsight_embed" not in sys.modules
    assert "hindsight_api" not in sys.modules
    assert "hindsight" not in sys.modules
