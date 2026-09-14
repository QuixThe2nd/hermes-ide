"""run_drift_watch: hash dedupe, alert text, capture contents, and pruning."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from plugins.drift_watch.core import (
    ERROR_PREFIX,
    build_inventory,
    prune_state,
    run_drift_watch,
)

T1 = datetime(2026, 8, 29, 10, 7, 0)
T2 = datetime(2026, 8, 29, 10, 37, 0)
T3 = datetime(2026, 8, 29, 11, 7, 0)


def test_first_run_on_clean_tree_reports_drift_gone_with_capture_dir(repo, tmp_path):
    state = tmp_path / "state"
    alert = run_drift_watch(repo, state, now=T1)
    capture = state / "captures" / "20260829-100700"
    assert alert == f"drift gone: live tree matches HEAD again (capture: {capture})"
    assert capture.is_dir()


def test_second_run_is_silent_then_new_drift_alerts(repo, tmp_path):
    state = tmp_path / "state"
    assert run_drift_watch(repo, state, now=T1) != ""
    assert run_drift_watch(repo, state, now=T1) == ""
    assert run_drift_watch(repo, state, now=T1) == ""

    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    alert = run_drift_watch(repo, state, now=T2)
    assert alert.startswith("drift detected in ")
    assert "1 path(s) differ from the last inventory" in alert
    assert " M a.txt " in alert
    assert "```" in alert
    # The alert never carries the inventory's head line (or the head sha).
    head_line = build_inventory(repo).splitlines()[0]
    assert head_line not in alert
    assert head_line[len("head "):] not in alert
    # And the alert is one-shot: the same drift is silent on the next pass.
    assert run_drift_watch(repo, state, now=T2) == ""


def test_drift_gone_after_reverting(repo, tmp_path):
    state = tmp_path / "state"
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    run_drift_watch(repo, state, now=T1)

    subprocess.run(["git", "-C", str(repo), "checkout", "--", "a.txt"], check=True)
    (repo / "new.txt").unlink()
    alert = run_drift_watch(repo, state, now=T2)
    assert alert.startswith("drift gone:")
    assert str(state / "captures" / "20260829-103700") in alert
    assert run_drift_watch(repo, state, now=T2) == ""


def test_capture_contents_patch_stat_meta_untracked(repo, tmp_path):
    state = tmp_path / "state"
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "small.txt").write_text("keep me\n", encoding="utf-8")
    (repo / "huge.bin").write_bytes(b"x" * (1024 * 1024))
    (repo / "adir").mkdir()
    (repo / "adir" / "inner.txt").write_text("nested\n", encoding="utf-8")

    run_drift_watch(repo, state, now=T1)
    capture = state / "captures" / "20260829-100700"

    patch = (capture / "tracked.patch").read_text(encoding="utf-8")
    assert "diff --git a/a.txt b/a.txt" in patch
    assert "-hello" in patch and "+changed" in patch
    assert "a.txt" in (capture / "stat.txt").read_text(encoding="utf-8")

    head = build_inventory(repo).splitlines()[0][len("head "):]
    assert (capture / "meta.txt").read_text(encoding="utf-8") == (
        f"head {head}\ncaptured 20260829-100700\n"
    )
    assert (capture / "inventory.txt").read_text(encoding="utf-8") == build_inventory(
        repo
    )

    copied = capture / "untracked" / "small.txt"
    assert copied.read_text(encoding="utf-8") == "keep me\n"
    # Oversize untracked files are recorded, never copied.
    assert not (capture / "untracked" / "huge.bin").exists()
    assert (
        capture / "untracked-too-big.txt"
    ).read_text(encoding="utf-8") == "huge.bin (1048576 bytes, too big to copy)\n"
    # Untracked directories are listed in the inventory, not copied.
    assert not (capture / "untracked" / "adir").exists()


def test_capture_rolls_last_inventory_and_history_forward(repo, tmp_path):
    state = tmp_path / "state"
    run_drift_watch(repo, state, now=T1)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    run_drift_watch(repo, state, now=T2)

    stored = (state / "last-inventory.txt").read_text(encoding="utf-8")
    assert stored == build_inventory(repo)
    history = state / "history"
    assert (history / "inventory-20260829-100700.txt").is_file()
    assert (history / "inventory-20260829-103700.txt").is_file()
    assert (
        history / "inventory-20260829-103700.txt").read_text(encoding="utf-8") == stored


def test_capture_writes_attribution_file(repo, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(
        "plugins.drift_watch.core.auditd_tail", lambda **kw: "line-one\nline-two"
    )
    run_drift_watch(repo, state, now=T1)
    attribution = (
        state / "captures" / "20260829-100700" / "attribution.txt"
    ).read_text(encoding="utf-8")
    assert attribution == "line-one\nline-two\n"


def test_prune_drops_old_captures_and_caps_the_count(tmp_path):
    state = tmp_path / "state" / "captures"
    state.mkdir(parents=True)
    for idx in range(4):
        capture = state / f"2026010{idx + 1}-000000"
        capture.mkdir()
        (capture / "inventory.txt").write_text("head x\n", encoding="utf-8")
    old = state / "20260101-000000"
    os.utime(old, (1000000000, 1000000000))

    from plugins.drift_watch.core import prune_state as prune

    prune(
        state.parent,
        retain_days=90,
        max_captures=2,
        now=datetime.fromtimestamp(1000000000 + 91 * 86400),
    )
    assert not old.exists()  # age-pruned
    remaining = sorted(d.name for d in state.iterdir())
    assert remaining == ["20260103-000000", "20260104-000000"]  # newest two kept


def test_prune_history_is_age_only(tmp_path):
    state = tmp_path / "state"
    history = state / "history"
    history.mkdir(parents=True)
    for idx in range(4):
        entry = history / f"inventory-2026010{idx + 1}-000000.txt"
        entry.write_text("head x\n", encoding="utf-8")
    os.utime(history / "inventory-20260101-000000.txt", (1000000000, 1000000000))

    prune_state(
        state,
        retain_days=90,
        max_captures=1,
        now=datetime.fromtimestamp(1000000000 + 91 * 86400),
    )
    remaining = sorted(p.name for p in history.iterdir())
    # Age-pruned only: the count cap never applies to history files.
    assert remaining == [
        "inventory-20260102-000000.txt",
        "inventory-20260103-000000.txt",
        "inventory-20260104-000000.txt",
    ]


def test_prune_respects_retain_boundary(tmp_path):
    state = tmp_path / "state" / "captures"
    state.mkdir(parents=True)
    capture = state / "20260101-000000"
    capture.mkdir()
    now = datetime(2026, 8, 29, 12, 0, 0)
    os.utime(capture, (now.timestamp() - 89 * 86400, now.timestamp() - 89 * 86400))
    prune_state(state.parent, retain_days=90, max_captures=50, now=now)
    assert capture.exists()  # 89 days old survives a 90-day retention


def test_missing_tree_returns_one_line_error(tmp_path):
    state = tmp_path / "state"
    for tree in ("", "   ", str(tmp_path / "nope")):
        alert = run_drift_watch(tree, state)
        assert alert.startswith(ERROR_PREFIX)
        assert "\n" not in alert
        assert not (state / "captures").exists()


def test_run_never_mutates_git_state(repo, tmp_path):
    state = tmp_path / "state"
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    for _ in range(3):
        run_drift_watch(repo, state, now=T1)

    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip() == head_before
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert sorted(status) == sorted([" M a.txt", "?? new.txt"])
    assert (repo / "a.txt").read_text(encoding="utf-8") == "changed\n"
    assert (repo / "new.txt").read_text(encoding="utf-8") == "untracked\n"
    # Nothing from the state dir leaked into the tree either.
    assert not (repo / "state").exists()
