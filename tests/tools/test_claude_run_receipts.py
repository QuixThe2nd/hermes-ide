"""Spawn-correlation receipts for ``delegate_claude_agent`` runs.

Mission Control links a Claude dispatch card to that exact run's live
viewer page through a small receipt written the moment the child spawns
(``tools/claude_run_receipts``). This suite pins the whole contract:

- the writer's durable shape: deterministic path under the SAME Hermes
  home the run logs into, exact correlation ids, the resolver-produced
  viewer URL, atomic whole-file replacement, mode 0600, and NEVER the
  task/prompt, credentials, tool results or CLI output;
- the writer's best-effort contract: missing/oversized ids, a non-run
  log stem or any write failure yield no receipt and never raise — the
  delegated coding task is never affected;
- the reader's fail-closed matrix: exact (home, session, call) triple,
  size bound, exact fixed key set, exact embedded ids, stem/fragment
  agreement, http(s)-only URL shape, 0600 permissions, owner check, no
  symlink, and the run log named by the stem actually existing in that
  same home — anything else reads as no receipt at all.

Run:  scripts/run_tests.sh tests/tools/test_claude_run_receipts.py
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tools import claude_run_receipts as r
from tools.claude_viewer_url import watch_url

SID = "sess-receipt-1"
CALL = "call-receipt-1"
STEM = "20260907-101500-4242"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A scratch Hermes home the writer's ambient resolution lands in."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def make_run_log(home: Path, stem: str = STEM) -> Path:
    log = r.claude_runs_dir(home) / (stem + ".jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type": "system"}\n', encoding="utf-8")
    return log


def write_receipt(home: Path, sid=SID, call=CALL, stem=STEM,
                  workdir="/tmp/repo"):
    return r.write_spawn_receipt(sid, call, str(make_run_log(home, stem)),
                                 workdir)


# ---------------------------------------------------------------------------
# Writer: durable shape
# ---------------------------------------------------------------------------

def test_receipt_lands_in_home_tree_with_exact_ids_and_url(home):
    path = write_receipt(home, workdir="/tmp/repo-x")
    assert path is not None
    # deterministic, hash-derived name inside <home>/claude-runs/receipts
    assert path == r.receipts_dir(home) / (
        r.binding_hash(SID, CALL) + ".json")
    assert path.parent == home / "claude-runs" / "receipts"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == r.SCHEMA_VERSION
    assert data["session_id"] == SID
    assert data["tool_call_id"] == CALL
    assert data["run_stem"] == STEM
    # the viewer URL is the one the shared host resolver produces, and
    # its fragment is exactly the run stem
    assert data["viewer_url"] == watch_url(STEM)
    assert data["viewer_url"].endswith("#" + STEM)
    assert data["workdir"] == "/tmp/repo-x"
    assert isinstance(data["created_at"], str) and data["created_at"]


def test_receipt_mode_is_0600(home):
    path = write_receipt(home)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_receipt_never_carries_task_or_secret_text(home):
    """The receipt has NO slot for the task: the writer's inputs are the
    correlation ids, the run log path and the workdir, so a dispatched
    task brief (or anything scraped from CLI output) cannot ride along —
    the serialized file is exactly the fixed key set, nothing else."""
    log = make_run_log(home, STEM)
    r.write_spawn_receipt(SID, CALL, str(log), workdir="/tmp/repo")
    raw = r.receipt_path(SID, CALL, home).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert set(data) == set(r.RECEIPT_KEYS)
    # the goal text a delegation would carry is simply not a value here
    task_brief = "SECRET GOAL: exfiltrate the plans, password=hunter2"
    assert task_brief not in raw
    assert "final_report" not in raw and "task" not in raw \
        and "prompt" not in raw
    # every value is one of the bounded known fields
    assert data["session_id"] == SID and data["tool_call_id"] == CALL
    assert data["run_stem"] == STEM and data["workdir"] == "/tmp/repo"


def test_receipt_replacement_is_atomic_and_whole(home):
    first = write_receipt(home)
    inode_first = os.stat(first).st_ino
    # a re-dispatch under the same call id replaces the receipt whole
    second = write_receipt(home, stem="20260907-101600-9999")
    assert second == first
    assert os.stat(second).st_ino != inode_first
    data = json.loads(second.read_text(encoding="utf-8"))
    assert data["run_stem"] == "20260907-101600-9999"
    # no temp litter beside it
    assert sorted(p.name for p in second.parent.iterdir()) == [
        second.name]


def test_workdir_is_clamped_on_write(home):
    log = make_run_log(home)
    r.write_spawn_receipt(SID, CALL, str(log), workdir="x" * 5000)
    data = json.loads(r.receipt_path(SID, CALL, home).read_text())
    assert len(data["workdir"]) == r.MAX_WORKDIR_CHARS


# ---------------------------------------------------------------------------
# Writer: best-effort, never raises, never blocks the delegation
# ---------------------------------------------------------------------------

def test_missing_ids_yield_no_receipt(home):
    make_run_log(home)
    assert r.write_spawn_receipt("", CALL, str(
        r.claude_runs_dir(home) / (STEM + ".jsonl"))) is None
    assert r.write_spawn_receipt(SID, None, str(
        r.claude_runs_dir(home) / (STEM + ".jsonl"))) is None
    assert not r.receipts_dir(home).exists() or \
        not list(r.receipts_dir(home).iterdir())


def test_non_run_log_stem_yields_no_receipt(home):
    bad = home / "claude-runs" / "not-a-run-stem.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("x", encoding="utf-8")
    assert r.write_spawn_receipt(SID, CALL, str(bad)) is None


def test_write_failure_is_swallowed_not_raised(home, monkeypatch):
    make_run_log(home)

    def _boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(r, "_atomic_write_json", _boom)
    # returns None; the delegation itself is never affected
    assert r.write_spawn_receipt(SID, CALL, str(
        r.claude_runs_dir(home) / (STEM + ".jsonl"))) is None


def test_spawn_hook_writes_receipt_only_with_the_full_pair(home,
                                                           monkeypatch):
    """The delegate tool's ``on_spawn`` factory: same contract, and the
    viewer notice + receipt ride one hook both dispatch modes share."""
    import tools.claude_agent_tool as tool

    log = make_run_log(home)
    hook = tool._spawn_on_spawn(SID, CALL, "/tmp/repo")
    hook(Path(log))
    rec = r.load_validated_receipt(home, SID, CALL)
    assert rec is not None and rec["tool_call_id"] == CALL

    # absent ids: the hook still fires (the viewer notice) but writes
    # nothing, and never raises
    before = sorted(p.name for p in r.receipts_dir(home).iterdir())
    hook_no_ids = tool._spawn_on_spawn(None, "", "/tmp/repo")
    hook_no_ids(log)
    after = sorted(p.name for p in r.receipts_dir(home).iterdir())
    assert before == after

    # a receipt writer that explodes cannot take the delegation down:
    # run_agent_cli swallows on_spawn callback failures by contract, so
    # the run continues either way (pinned here at the seam the tool
    # actually calls)
    def _raise(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(tool, "write_spawn_receipt", _raise)
    hook_bad = tool._spawn_on_spawn(SID, CALL, "/tmp/repo")
    with pytest.raises(OSError):
        hook_bad(log)


# ---------------------------------------------------------------------------
# Reader: fail closed on every axis
# ---------------------------------------------------------------------------

def valid_receipt(home):
    path = r.receipt_path(SID, CALL, home)
    assert write_receipt(home) is not None
    return path


def test_reader_happy_path(home):
    valid_receipt(home)
    rec = r.load_validated_receipt(home, SID, CALL)
    assert rec is not None
    assert r.resolve_watch_url(home, SID, CALL) == watch_url(STEM)


def test_reader_wrong_ids_fail_closed(home):
    valid_receipt(home)
    assert r.load_validated_receipt(home, "other-session", CALL) is None
    assert r.load_validated_receipt(home, SID, "other-call") is None
    assert r.resolve_watch_url(home, SID, "other-call") is None


def test_reader_cross_home_fails_closed(home, tmp_path):
    valid_receipt(home)
    other_home = tmp_path / "other-home"
    (other_home / "claude-runs").mkdir(parents=True)
    # a receipt living in another home's tree is invisible from here
    assert r.load_validated_receipt(other_home, SID, CALL) is None


def test_reader_missing_log_fails_closed(home):
    path = valid_receipt(home)
    os.unlink(r.claude_runs_dir(home) / (STEM + ".jsonl"))
    # the receipt itself is intact, but the run it names never existed
    # in this home -> not proof of anything
    assert path.exists()
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_oversized_fails_closed(home):
    path = valid_receipt(home)
    data = json.loads(path.read_text())
    data["workdir"] = "x" * (r.MAX_RECEIPT_BYTES + 10)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_malformed_json_fails_closed(home):
    path = valid_receipt(home)
    path.write_text("{not json at all")
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_wrong_key_set_fails_closed(home):
    path = valid_receipt(home)
    data = json.loads(path.read_text())
    data["extra"] = "nope"
    path.write_text(json.dumps(data))
    assert r.load_validated_receipt(home, SID, CALL) is None
    del data["extra"]
    data.pop("workdir")
    path.write_text(json.dumps(data))
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_wrong_schema_version_fails_closed(home):
    path = valid_receipt(home)
    data = json.loads(path.read_text())
    data["schema_version"] = 99
    path.write_text(json.dumps(data))
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_embedded_id_mismatch_fails_closed(home):
    path = valid_receipt(home)
    data = json.loads(path.read_text())
    data["session_id"] = "someone-else"
    path.write_text(json.dumps(data))
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_unsafe_url_fails_closed(home):
    for url in (
        "javascript:alert(1)//#" + STEM,
        "file:///etc/passwd#" + STEM,
        "http://user:pass@host:8787/#" + STEM,
        "http://host:8787/path#" + STEM,
        "http://host:8787/#" + STEM + "?x=1",
        "http://host:8787/#other-stem-entirely",
        "ftp://host:8787/#" + STEM,
    ):
        path = valid_receipt(home)
        data = json.loads(path.read_text())
        data["viewer_url"] = url
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        assert r.load_validated_receipt(home, SID, CALL) is None, url


def test_reader_loose_permissions_fail_closed(home):
    path = valid_receipt(home)
    os.chmod(path, 0o644)
    try:
        assert r.load_validated_receipt(home, SID, CALL) is None
    finally:
        os.chmod(path, 0o600)


def test_reader_symlink_fails_closed(home, tmp_path):
    path = valid_receipt(home)
    outside = tmp_path / "outside.json"
    data = json.loads(path.read_text())
    outside.write_text(json.dumps(data))
    os.unlink(path)
    os.symlink(outside, path)
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_bad_stem_fails_closed(home):
    path = valid_receipt(home)
    data = json.loads(path.read_text())
    data["run_stem"] = "../../etc/passwd"
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    assert r.load_validated_receipt(home, SID, CALL) is None


def test_reader_never_raises_on_missing_tree(tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    assert r.load_validated_receipt(empty, SID, CALL) is None
    assert r.resolve_watch_url(empty, SID, CALL) is None
