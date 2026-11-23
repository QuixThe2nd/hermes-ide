"""Watch-link contract for running OMP lane delegations.

The lane launcher (``tools/delegate_tool_omp.run_omp_lane``) announces a
freshly started run's live viewer page through the mid-tool status
callback as ``"OMP Lane: http://<host>:<port>/#<session-dir>"``. Platform
adapters render that exact line as a branded card only when the URL
passes the same safety gate the Claude Code Agent link passes
(``is_allowed_watch_url``): scheme http/https, path ``/`` only, no query,
fragment empty or an absolute traversal-free session directory, and a
private-network host (loopback, RFC1918, Tailscale CGNAT, ``::1``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tools.delegate_tool_omp as omp
from plugins.omp_viewer import url as viewer_url
from plugins.omp_viewer import watch
from tools.delegate_lane_specs import resolve_lane_spec
from tools.tool_status import (
    AGENT_VIEWER_STATUS_PREFIXES,
    CLAUDE_AGENT_VIEWER_STATUS_PREFIX,
    OMP_LANE_VIEWER_STATUS_PREFIX,
    is_agent_viewer_status_line,
    tool_status_scope,
)

SESSION_DIR = "/srv/work/proj/.hermes-lane/litellm/backend_dev"


@pytest.fixture(autouse=True)
def _fixed_public_host(monkeypatch):
    """Pin host auto-detection so emitted URLs are deterministic."""
    monkeypatch.setattr(viewer_url, "detect_public_host", lambda **kwargs: "10.0.0.7")

@pytest.fixture
def workdir(tmp_path):
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


# ---------------------------------------------------------------------------
# URL shape + safety gate
# ---------------------------------------------------------------------------


def test_watch_url_carries_quoted_session_dir():
    url = viewer_url.watch_url(SESSION_DIR, cfg={"port": 8790})

    assert url == f"http://10.0.0.7:8790/#{SESSION_DIR}"
    assert viewer_url.is_allowed_watch_url(url) is True


def test_watch_url_quotes_unsafe_selector_bytes():
    url = viewer_url.watch_url("/w/proj with space/.hermes-lane/m", cfg={"port": 8790})

    assert url.endswith("/#/w/proj%20with%20space/.hermes-lane/m")
    assert viewer_url.is_allowed_watch_url(url) is True


@pytest.mark.parametrize("url,expected", [
    ("http://10.0.0.7:8790/#/srv/work/x/.hermes-lane/m", True),   # RFC1918
    ("http://127.0.0.1:8790/#/srv/work/x", True),                 # loopback
    ("http://100.64.1.2:8790/#/srv/work/x", True),                # Tailscale CGNAT
    ("http://[::1]:8790/#/srv/work/x", True),                     # v6 loopback
    ("https://10.0.0.7:8790/#/srv/work/x", True),                 # https ok
    ("http://10.0.0.7:8790/", True),                              # no fragment ok
    ("http://8.8.8.8:8790/#/srv/work/x", False),                  # public host
    ("http://viewer.example.com/#/srv/work/x", False),            # bare DNS name
    ("ftp://10.0.0.7/#/srv/work/x", False),                       # scheme
    ("http://user@10.0.0.7/#/srv/work/x", False),                 # userinfo
    ("http://10.0.0.7/other#/srv/work/x", False),                 # path
    ("http://10.0.0.7/?q=1#/srv/work/x", False),                  # query
    ("http://10.0.0.7/#", False),                                 # empty fragment
    ("http://10.0.0.7/#../etc/passwd", False),                    # traversal
    ("http://10.0.0.7/#relative/dir", False),                     # not absolute
    ("http://10.0.0.7/#/has space/dir", False),                   # raw whitespace
    ("http://10.0.0.7/#/%2e%2e/escape", False),                   # encoded traversal
])
def test_is_allowed_watch_url_matrix(url, expected):
    assert viewer_url.is_allowed_watch_url(url) is expected


def test_is_allowed_watch_url_accepts_configured_extra_host():
    url = "http://viewer.lan:8790/#/srv/work/x"

    assert viewer_url.is_allowed_watch_url(url) is False
    assert viewer_url.is_allowed_watch_url(url, cfg={"extra_hosts": ["viewer.lan"]}) is True


# ---------------------------------------------------------------------------
# Status-line prefix contract
# ---------------------------------------------------------------------------


def test_omp_prefix_is_a_registered_agent_viewer_prefix():
    assert OMP_LANE_VIEWER_STATUS_PREFIX == "OMP Lane: "
    assert OMP_LANE_VIEWER_STATUS_PREFIX in AGENT_VIEWER_STATUS_PREFIXES
    assert is_agent_viewer_status_line(f"{OMP_LANE_VIEWER_STATUS_PREFIX}http://x") is True
    assert is_agent_viewer_status_line(
        f"{CLAUDE_AGENT_VIEWER_STATUS_PREFIX}http://x"
    ) is True
    assert is_agent_viewer_status_line("ordinary status") is False


# ---------------------------------------------------------------------------
# Status-line producer
# ---------------------------------------------------------------------------


def test_lane_watch_status_line_enabled():
    line = watch.lane_watch_status_line(SESSION_DIR)

    assert line == f"{OMP_LANE_VIEWER_STATUS_PREFIX}http://10.0.0.7:8790/#{SESSION_DIR}"
    url = line[len(OMP_LANE_VIEWER_STATUS_PREFIX):]
    assert viewer_url.is_allowed_watch_url(url) is True


def test_lane_watch_status_line_disabled_plugin_returns_none(monkeypatch):
    monkeypatch.setattr(watch, "plugin_explicitly_disabled", lambda: True)

    assert watch.lane_watch_status_line(SESSION_DIR) is None


def test_lane_watch_status_line_never_announces_a_failing_url(monkeypatch):
    # A public host override would produce a URL the adapters refuse to
    # render; announcing it would be a dead link, so the producer stays quiet.
    monkeypatch.setattr(viewer_url, "detect_public_host", lambda **kwargs: "8.8.8.8")

    assert watch.lane_watch_status_line(SESSION_DIR) is None


# ---------------------------------------------------------------------------
# Emit (mid-tool status callback)
# ---------------------------------------------------------------------------


def test_emit_lane_watch_notice_reaches_bound_callback():
    seen = []
    with tool_status_scope(seen.append):
        assert watch.emit_lane_watch_notice(Path(SESSION_DIR)) is True

    assert seen == [f"{OMP_LANE_VIEWER_STATUS_PREFIX}http://10.0.0.7:8790/#{SESSION_DIR}"]


def test_emit_lane_watch_notice_unbound_is_quiet_false():
    assert watch.emit_lane_watch_notice(Path(SESSION_DIR)) is False


def test_emit_lane_watch_notice_disabled_is_quiet_false(monkeypatch):
    monkeypatch.setattr(watch, "plugin_explicitly_disabled", lambda: True)
    seen = []
    with tool_status_scope(seen.append):
        assert watch.emit_lane_watch_notice(Path(SESSION_DIR)) is False
    assert seen == []


# ---------------------------------------------------------------------------
# Lane integration: run_omp_lane announces the link once the session exists
# ---------------------------------------------------------------------------


def _lane_secrets(tmp_path):
    secrets = tmp_path / "omp.env"
    secrets.write_text("LITELLM_API_KEY=test-key\n", encoding="utf-8")
    soul = tmp_path / "soul.md"
    soul.write_text("Preserve spacing.\n", encoding="utf-8")
    spec = resolve_lane_spec("ui_dev", {"ui_dev": {
        "soul": str(soul), "mcp": None, "tools_allowlist": ["read", "grep"],
    }})
    return spec, {"binary": sys.executable, "secrets_file": str(secrets)}


def _completed(argv, code, stdout=""):
    proc = subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
    proc.returncode = code
    return proc


def test_run_omp_lane_announces_watch_link(monkeypatch, tmp_path, workdir):
    spec, config = _lane_secrets(tmp_path)
    monkeypatch.setattr(omp, "caller_killed_service_cgroup", lambda: None)

    def _run(argv, **kwargs):
        # The notice must already have gone out while the run is live.
        live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
        assert live["session_dir"] in announced[0]
        return _completed(argv, 0, stdout=json.dumps({
            "type": "turn_end", "message": {"content": [{"text": "layout aligned"}]},
        }))

    monkeypatch.setattr(omp.subprocess, "run", _run)
    announced = []
    with tool_status_scope(announced.append):
        result = omp.run_omp_lane(
            spec, "Align the panel", str(workdir), "provider/model", omp_config=config,
        )

    assert result["success"] is True
    assert len(announced) == 1
    line = announced[0]
    assert line.startswith(OMP_LANE_VIEWER_STATUS_PREFIX)
    url = line[len(OMP_LANE_VIEWER_STATUS_PREFIX):]
    assert viewer_url.is_allowed_watch_url(url) is True
    assert url.endswith("#" + result["session_dir"])
