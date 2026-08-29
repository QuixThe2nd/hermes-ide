"""Inventory building and hashing on a real temp git repo."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from plugins.drift_watch.core import (
    build_inventory,
    drift_head,
    drift_lines,
    inventory_hash,
    last_capture_dir,
    last_drift_count,
)

HEAD_RE = re.compile(r"^head [0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def test_clean_repo_inventory_is_head_line_only(repo):
    inventory = build_inventory(repo)
    lines = inventory.splitlines()
    assert len(lines) == 1
    assert HEAD_RE.match(lines[0])
    assert inventory.endswith("\n")


def test_head_line_matches_rev_parse(repo):
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert build_inventory(repo).splitlines()[0] == f"head {head}"


def test_dirty_tracked_file_gets_content_sha(repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    inventory = build_inventory(repo)
    lines = inventory.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith(" M a.txt ")
    sha = lines[1].rsplit(" ", 1)[1]
    assert SHA_RE.match(sha)
    assert sha == hashlib.sha256(b"changed\n").hexdigest()


def test_untracked_file_and_directory_listed_sorted(repo):
    (repo / "b.txt").write_text("bee\n", encoding="utf-8")
    (repo / "a-new.txt").write_text("ay\n", encoding="utf-8")
    (repo / "somedir").mkdir()
    (repo / "somedir" / "inner.txt").write_text("x\n", encoding="utf-8")
    lines = build_inventory(repo).splitlines()[1:]
    assert [line.split(" ")[1] for line in lines] == [
        "a-new.txt",
        "b.txt",
        "somedir/",
    ]
    # Directories are not regular files, so they hash to a dash.
    assert lines[2].endswith(" -")


def test_deleted_file_hashes_to_dash(repo):
    (repo / "a.txt").unlink()
    lines = build_inventory(repo).splitlines()[1:]
    assert lines == [" D a.txt -"]


def test_inventory_hash_is_sha256_of_text(repo):
    inventory = build_inventory(repo)
    assert inventory_hash(inventory) == hashlib.sha256(inventory.encode()).hexdigest()
    assert inventory_hash("") == hashlib.sha256(b"").hexdigest()


def test_inventory_changes_when_drift_changes(repo):
    before = build_inventory(repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    after = build_inventory(repo)
    assert before != after
    assert inventory_hash(before) != inventory_hash(after)


def test_drift_lines_drops_head_line(repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    inventory = build_inventory(repo)
    assert drift_lines(inventory) == inventory.splitlines()[1:]
    assert drift_lines("head abc\n") == []
    assert drift_head(inventory) == inventory.splitlines()[0][len("head "):]


def test_missing_tree_raises_drift_watch_error(tmp_path):
    from plugins.drift_watch.core import DriftWatchError

    with pytest.raises(DriftWatchError):
        build_inventory(tmp_path / "nope")
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(DriftWatchError):
        build_inventory(not_a_repo)


def test_last_drift_count_and_capture_dir(tmp_path, repo):
    state = tmp_path / "state"
    assert last_drift_count(state) is None
    assert last_capture_dir(state) is None

    (state / "captures" / "20260829-100700").mkdir(parents=True)
    (state / "captures" / "20260829-103700").mkdir(parents=True)
    (state / "last-inventory.txt").write_text(
        "head abc\n M a.txt sha\n?? b.txt sha2\n", encoding="utf-8"
    )
    assert last_drift_count(state) == 2
    assert last_capture_dir(state) == state / "captures" / "20260829-103700"
