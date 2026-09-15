"""Seen-state persistence contracts: path, round-trip, atomicity, pruning."""

from __future__ import annotations

import json
import os
import stat

import pytest

from plugins.pr_intent_watch.core import (
    MAX_SEEN_ENTRIES,
    load_state,
    prune_seen,
    save_state,
    state_path,
)


def test_state_path_lives_under_hermes_home_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert state_path() == tmp_path / "state" / "pr_intent_watch.json"


def test_missing_state_file_loads_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_state() == {}


def test_corrupt_state_loads_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state" / "pr_intent_watch.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_state() == {}


def test_non_object_state_loads_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state" / "pr_intent_watch.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_state() == {}


def test_state_round_trip_creates_parent_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = {
        "repo": "QuixThe2nd/hermes-ide",
        "seen": {"123": {"head_sha": "abc", "commented": True, "skipped": False}},
        "baseline_complete": True,
    }
    save_state(state)
    assert state_path().is_file()  # parent dirs created on demand
    assert load_state() == state


def test_atomic_write_leaves_no_temp_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for _ in range(3):
        save_state({"repo": "r", "seen": {}, "baseline_complete": True})
    leftovers = [p.name for p in (tmp_path / "state").iterdir()]
    assert leftovers == ["pr_intent_watch.json"]


def test_state_file_is_owner_readable_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_state({"repo": "r", "seen": {}, "baseline_complete": True})
    mode = stat.S_IMODE(state_path().stat().st_mode)
    assert mode == 0o600
    if os.name == "posix":
        assert mode & 0o077 == 0  # nothing for group/other


def test_prune_seen_keeps_newest_numbers():
    seen = {str(number): {"commented": True} for number in range(600)}
    pruned = prune_seen(seen, cap=MAX_SEEN_ENTRIES)
    assert len(pruned) == MAX_SEEN_ENTRIES
    # Newest PR numbers survive; the oldest are dropped.
    assert "599" in pruned
    assert "100" in pruned
    assert "99" not in pruned
    assert "0" not in pruned


def test_prune_seen_noop_at_or_below_cap():
    seen = {"1": {}, "2": {}}
    assert prune_seen(seen) == seen
    assert prune_seen({}, cap=5) == {}


def test_prune_seen_tolerates_non_numeric_keys():
    pruned = prune_seen({"weird": {}, **{str(n): {} for n in range(600)}})
    assert len(pruned) == MAX_SEEN_ENTRIES
    assert "599" in pruned


@pytest.mark.parametrize(
    "payload",
    [
        {"repo": "QuixThe2nd/hermes-ide", "seen": {}, "baseline_complete": False},
        {"seen": {"7": {"head_sha": "s", "commented": False, "skipped": True}}},
    ],
)
def test_save_state_overwrites_previous_payload(monkeypatch, tmp_path, payload):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    save_state({"repo": "old", "seen": {}, "baseline_complete": True})
    save_state(payload)
    assert load_state() == payload
