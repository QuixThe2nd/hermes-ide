"""Lane-run registry: writer contract, server discovery, and serving gate.

Covers the run-discovery contract of the bundled omp-viewer:

- ``plugins/omp_viewer/runs.py`` — the per-home registry the lane launcher
  (``tools/delegate_tool_omp.py``) writes the moment a run's ``.hermes-lane``
  live session exists, so the viewer finds every run on the machine wherever
  its workdir lives, with no machine-specific default path;
- ``plugins/omp_viewer/viewer/server.py`` — registry workdirs are discovered
  alongside (never instead of) ``scan_roots`` hits, stale or malformed
  entries are skipped, and ``resolve_transcript`` serves a registry workdir's
  transcripts with the same strictness as scan-root ones (exact workdir
  match — siblings stay closed);
- defaults carry no machine-specific path: ``DEFAULT_SCAN_ROOTS`` is empty,
  ``scan_roots`` stays an additive config option, and the systemd unit
  renders no ``--scan-root`` flag unless one is configured.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

import plugins.omp_viewer.viewer.server as server
from plugins.omp_viewer import runs as lane_runs
from plugins.omp_viewer import url as viewer_url
from plugins.omp_viewer.config import load_omp_viewer_config
from plugins.omp_viewer.systemd import build_exec_start_argv


def _user_event(text: str) -> dict:
    return {
        "type": "message",
        "id": "eebf10f6",
        "parentId": "ac01e871",
        "timestamp": "2026-11-15T16:19:33.677Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _make_lane(workdir: Path, lane: str = "backend_dev") -> Path:
    """Create ``<workdir>/.hermes-lane/<lane>/`` with one transcript."""
    lane_dir = workdir / ".hermes-lane" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    transcript = lane_dir / "s.jsonl"
    transcript.write_text(json.dumps(_user_event("# Task\nShip it")) + "\n", encoding="utf-8")
    return transcript


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture
def no_scan_roots(monkeypatch) -> None:
    monkeypatch.setattr(server, "SCAN_ROOTS", [])


# ── registry writer (plugins/omp_viewer/runs.py) ──────────────────────────


def test_record_lane_run_writes_valid_entry(home: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "proj-a"
    session_dir = workdir / ".hermes-lane" / "backend_dev"
    session_dir.mkdir(parents=True)

    path = lane_runs.record_lane_run(workdir, session_dir, "litellm/backend_dev")

    assert path is not None and path.is_file()
    assert path.parent == home / "omp-lane-runs"
    assert path.name == hashlib.sha256(str(workdir).encode("utf-8")).hexdigest() + ".json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "workdir": str(workdir),
        "session_dir": str(session_dir),
        "model": "litellm/backend_dev",
        "recorded_at": payload["recorded_at"],
    }
    assert payload["recorded_at"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_record_lane_run_overwrites_same_workdir(home: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "proj-a"
    workdir.mkdir()

    first = lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "a", "m1")
    second = lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "b", "m2")

    assert first == second
    payload = json.loads(second.read_text(encoding="utf-8"))
    assert payload["model"] == "m2"
    assert len(list((home / "omp-lane-runs").glob("*.json"))) == 1


def test_record_lane_run_rejects_relative_workdir(home: Path) -> None:
    assert lane_runs.record_lane_run(Path("relative/proj"), None, "m") is None
    assert not (home / "omp-lane-runs").exists()


def test_record_lane_run_never_raises_on_unwritable_home(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "hermes-home"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(blocker))
    assert lane_runs.record_lane_run(tmp_path, None, "m") is None


# ── defaults carry no machine-specific path ───────────────────────────────


def test_default_scan_roots_are_empty() -> None:
    assert viewer_url.DEFAULT_SCAN_ROOTS == ()
    assert viewer_url.viewer_scan_roots({}) == []


def test_scan_roots_config_is_additive() -> None:
    assert viewer_url.viewer_scan_roots({"scan_roots": ["/srv/a", "/srv/b"]}) == ["/srv/a", "/srv/b"]
    assert viewer_url.viewer_scan_roots({"scan_roots": "/srv/one"}) == ["/srv/one"]
    assert viewer_url.viewer_scan_roots({"scan_roots": ["/srv/a", "/srv/a", ""]}) == ["/srv/a"]


def test_load_config_defaults_have_no_scan_roots(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = load_omp_viewer_config()
    assert cfg["scan_roots"] == []


def test_exec_start_omits_scan_root_when_unconfigured(tmp_path: Path) -> None:
    argv = build_exec_start_argv({"bind": "127.0.0.1", "port": 8790, "scan_roots": []})
    assert "--scan-root" not in argv


# ── server-side registry discovery ────────────────────────────────────────


def test_find_lane_workdirs_includes_registry_workdir(
    home: Path, no_scan_roots, tmp_path: Path
) -> None:
    workdir = tmp_path / "elsewhere" / "proj-x"
    transcript = _make_lane(workdir)
    assert lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "backend_dev", "m")

    workdirs = server.find_lane_workdirs()
    assert workdirs == [workdir.resolve()]

    runs = server.list_runs()["runs"]
    assert [r["session_file"] for r in runs] == [str(transcript)]
    assert runs[0]["workdir"] == str(workdir.resolve())


def test_registry_skips_stale_and_malformed_entries(
    home: Path, no_scan_roots, tmp_path: Path
) -> None:
    runs_dir = home / "omp-lane-runs"
    runs_dir.mkdir()
    gone = {"version": 1, "workdir": str(tmp_path / "deleted-proj")}
    (runs_dir / "gone.json").write_text(json.dumps(gone), encoding="utf-8")
    (runs_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    (runs_dir / "shape.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    (runs_dir / "relative.json").write_text(
        json.dumps({"version": 1, "workdir": "relative/proj"}), encoding="utf-8"
    )
    (runs_dir / "huge.json").write_bytes(b" " * (server.MAX_RUN_ENTRY_BYTES + 1))
    no_lane = tmp_path / "no-lane"
    no_lane.mkdir()
    (runs_dir / "nolane.json").write_text(
        json.dumps({"version": 1, "workdir": str(no_lane)}), encoding="utf-8"
    )
    older = {"version": 0, "workdir": str(no_lane)}
    (runs_dir / "older.json").write_text(json.dumps(older), encoding="utf-8")

    assert server.find_lane_workdirs() == []


def test_registry_workdir_dedupes_with_scan_root(home: Path, tmp_path: Path, monkeypatch) -> None:
    scan_root = tmp_path / "scan"
    workdir = scan_root / "proj-a"
    _make_lane(workdir)
    monkeypatch.setattr(server, "SCAN_ROOTS", [str(scan_root)])
    assert lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "backend_dev", "m")

    assert server.find_lane_workdirs() == [workdir.resolve()]


# ── serving gate: registry workdirs at least as strict as scan roots ──────


def test_resolve_transcript_serves_registry_workdir(
    home: Path, no_scan_roots, tmp_path: Path
) -> None:
    workdir = tmp_path / "proj-x"
    transcript = _make_lane(workdir)
    assert lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "backend_dev", "m")

    assert server.resolve_transcript(str(transcript)) == transcript.resolve()


def test_resolve_transcript_rejects_sibling_of_registry_workdir(
    home: Path, no_scan_roots, tmp_path: Path
) -> None:
    """A registry entry opens its exact workdir only — never its siblings."""
    workdir = tmp_path / "proj-x"
    _make_lane(workdir)
    assert lane_runs.record_lane_run(workdir, workdir / ".hermes-lane" / "backend_dev", "m")
    sibling_transcript = _make_lane(tmp_path / "proj-y")

    assert server.resolve_transcript(str(sibling_transcript)) is None


def test_resolve_transcript_rejects_unregistered_path_without_scan_roots(
    home: Path, no_scan_roots, tmp_path: Path
) -> None:
    transcript = _make_lane(tmp_path / "stray")
    assert server.resolve_transcript(str(transcript)) is None


# ── lane launcher records the run it starts ───────────────────────────────


def test_write_live_records_registry_entry(home: Path, tmp_path: Path) -> None:
    from tools import delegate_tool_omp as omp

    workdir = tmp_path / "proj-live"
    session_dir = workdir / ".hermes-lane" / "provider" / "model"
    omp._write_live(workdir, session_dir, "provider/model")

    entries = list((home / "omp-lane-runs").glob("*.json"))
    assert len(entries) == 1
    payload = json.loads(entries[0].read_text(encoding="utf-8"))
    assert payload["workdir"] == str(workdir)
    assert payload["session_dir"] == str(session_dir)
    assert payload["model"] == "provider/model"


def test_write_live_survives_registry_failure(home: Path, tmp_path: Path, monkeypatch) -> None:
    from tools import delegate_tool_omp as omp

    def _boom(*args, **kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(lane_runs, "record_lane_run", _boom)
    workdir = tmp_path / "proj-live"
    session_dir = workdir / ".hermes-lane" / "provider" / "model"
    omp._write_live(workdir, session_dir, "provider/model")

    live = json.loads((workdir / ".hermes-lane" / "live.json").read_text(encoding="utf-8"))
    assert live["session_dir"] == str(session_dir)
