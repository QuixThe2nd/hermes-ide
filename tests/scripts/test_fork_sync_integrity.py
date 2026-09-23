"""Acceptance tests for .github/scripts/fork-sync-integrity.py.

Built the way the fork-sync acceptance setup works: real git against local
bare remotes under pytest tmp_path, driving the exact product file — the
CLI via subprocess for the end-to-end cases (exit codes, JSON report
shape), and the library's pure classifier with fabricated inputs for the
unit case. No network anywhere: DISCORD_SYNC_ALERT is stripped from the
environment so send_discord_alert() is unreachable in every test.

Shared history shape: upstream ``tracked.txt`` evolves A -> B -> C (one
commit each, fixed dates); the fork is cloned from the upstream bare,
reset to the pre-file root commit, given one fork-only commit (so every
import is a real --no-ff merge carrying the upstream commit as its second
parent — the fork-sync-sequential.sh shape), then imports upstream
commits one at a time. Case-specific resolutions create (or avoid) drops.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "fork-sync-integrity.py"

# Fixed author/committer dates keep the dropped_commits evidence
# deterministic; the report carries committer dates (%cI).
D_ROOT = "2024-01-01T00:00:00+00:00"
D_A = "2024-01-02T00:00:00+00:00"
D_B = "2024-01-03T00:00:00+00:00"
D_C = "2024-01-04T00:00:00+00:00"
D_FORK = "2024-01-05T00:00:00+00:00"

SUBJECT_A = "feat: tracked.txt to A"
SUBJECT_B = "feat: tracked.txt to B"
SUBJECT_C = "feat: tracked.txt to C"


def _git(repo, *args, check=True, date=None):
    """Run git in *repo*; commits get a fixture identity and fixed dates."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Fixture Writer",
        GIT_AUTHOR_EMAIL="fixture@example.com",
        GIT_COMMITTER_NAME="Fixture Writer",
        GIT_COMMITTER_EMAIL="fixture@example.com",
    )
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc


def _commit(repo, message, files, date):
    """Stage {path: content} (content None deletes the path) and commit."""
    for rel, content in files.items():
        if content is None:
            _git(repo, "rm", "-q", "--", rel)
        else:
            (Path(repo) / rel).write_text(content)
            _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _build_fork(tmp_path, upstream_commits):
    """Bare upstream.git + fork.git, plus a fork work clone.

    *upstream_commits* is a list of ``(key, message, files, date)`` tuples;
    the first one is the pre-file root the fork starts from. Returns
    ``(fork_repo_path, {key: sha})``. The fork work clone carries
    refs/remotes/upstream/main (the ref the CLI is pointed at in CI) and
    pushes its main to fork.git, mirroring the real fork's shape.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    shas = {}
    for key, message, files, date in upstream_commits:
        shas[key] = _commit(upstream, message, files, date)

    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(tmp_path / "upstream.git"))
    _git(upstream, "push", "-q", str(tmp_path / "upstream.git"), "main")
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(tmp_path / "fork.git"))

    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(tmp_path / "upstream.git"), str(fork))
    _git(fork, "remote", "rename", "origin", "upstream")
    _git(fork, "reset", "-q", "--hard", shas["root"])
    _git(fork, "remote", "add", "origin", str(tmp_path / "fork.git"))
    _git(fork, "push", "-q", "origin", "main")
    return fork, shas


def _upstream_abc(tmp_path):
    """The TASK.md base scenario: tracked.txt evolves A -> B -> C."""
    return _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"tracked.txt": "A\n"}, D_A),
            ("B", SUBJECT_B, {"tracked.txt": "B\n"}, D_B),
            ("C", SUBJECT_C, {"tracked.txt": "C\n"}, D_C),
        ],
    )


def _import(fork, sha, resolve=None):
    """Import one upstream commit fork-sync style: --no-ff merge whose tree
    is whatever *resolve* staged (None = take the clean merge result).

    A conflicted merge without a resolver is a fixture bug, not a scenario.
    """
    proc = _git(fork, "merge", "--no-ff", "--no-commit", "--no-edit", sha, check=False)
    if proc.returncode != 0 and resolve is None:
        raise AssertionError(f"unexpected merge conflict importing {sha}:\n{proc.stdout}{proc.stderr}")
    if resolve is not None:
        resolve()
    _git(fork, "commit", "-q", "-m", f"Merge upstream {sha[:9]}")
    _git(fork, "push", "-q", "origin", "main")


def _blob(fork, rev, path):
    return _git(fork, "rev-parse", f"{rev}:{path}").stdout.strip()


def _run_checker(fork, *extra_args, upstream_ref="refs/remotes/upstream/main"):
    """Run the real CLI inside the fork clone; return (proc, report|None)."""
    json_out = Path(fork).parent / "integrity-report.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DISCORD_SYNC_ALERT" and not key.startswith("INTEGRITY_")
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--fork-ref",
            "main",
            "--upstream-ref",
            upstream_ref,
            "--json-out",
            str(json_out),
            *extra_args,
        ],
        cwd=fork,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = json.loads(json_out.read_text()) if json_out.exists() else None
    return proc, report


# --- case a: bad merge resolution puts an old imported blob back -------------


def test_merge_resolved_back_to_old_blob_is_a_drop(tmp_path):
    """Imported through B, then the C merge resolves back to A's blob.

    The stale A is reapplied fork-side first, so importing C conflicts and
    the (bad) resolution keeps A — exactly the silent-drop shape the
    checker exists for: every commit is imported, yet the newest imported
    content for the path is not what fork main carries.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])
    _commit(fork, "fix: reapply A (stale patch)", {"tracked.txt": "A\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    def keep_ours():
        _git(fork, "checkout", "--ours", "--", "tracked.txt")
        _git(fork, "add", "--", "tracked.txt")

    _import(fork, shas["C"], resolve=keep_ours)

    proc, report = _run_checker(fork)
    assert proc.returncode == 2, f"expected drops found:\n{proc.stdout}{proc.stderr}"
    assert "tracked.txt" in proc.stdout

    assert [d["path"] for d in report["drops"]] == ["tracked.txt"]
    drop = report["drops"][0]
    blob_a = _blob(fork, shas["A"], "tracked.txt")
    assert drop["status"] == "M"
    assert drop["fork_blob"] == blob_a
    assert drop["matched_upstream_blob"] == blob_a
    # Both the B import and the C import are demonstrably absent from the
    # fork's blob, newest first.
    assert [c["sha"] for c in drop["dropped_commits"]] == [shas["C"], shas["B"]]
    # %cI is strict ISO 8601: git renders a +00:00 fixture date as "Z".
    assert drop["dropped_commits"][0] == {
        "sha": shas["C"],
        "date": D_C.replace("+00:00", "Z"),
        "subject": SUBJECT_C,
    }
    assert report["totals"]["drops"] == 1
    assert report["fork_ref"] == "main"
    assert report["upstream_ref"] == "refs/remotes/upstream/main"


# --- case b: unimported upstream advance is plain lag ------------------------


def test_unimported_upstream_advance_is_lag_not_a_drop(tmp_path):
    """Fork through B, upstream advanced to C (not imported): stay quiet.

    The census still diverges (B vs C) — the checker must classify it as
    lag, not a drop, or it would cry wolf every hour between an upstream
    push and its import.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report is not None  # detail JSON is written on clean runs too
    assert report["drops"] == []
    assert report["totals"]["lag"] == 1


# --- case c: deliberate fork modification ------------------------------------


def test_fork_modified_file_is_not_a_drop(tmp_path):
    """Fully imported through C, then fork-edited to an unknown blob.

    The fork blob matches no imported upstream version, so there is no
    hard evidence of a drop — a deliberate fork edit must stay silent.
    """
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B", "C"):
        _import(fork, shas[key])
    _commit(fork, "fork: local tweak", {"tracked.txt": "Z (fork-only)\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["drops"] == []
    assert report["totals"]["fork_modified"] == 1


# --- case d: fork-only new file ----------------------------------------------


def test_fork_only_file_is_not_a_drop(tmp_path):
    """A path upstream never touched cannot have lost upstream content."""
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B", "C"):
        _import(fork, shas[key])
    _commit(fork, "fork: new file", {"fork_only.txt": "only here\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["drops"] == []
    assert report["totals"]["fork_only"] >= 1


# --- case e: deleted-on-fork path with imported evolution --------------------


def test_fork_deleted_path_with_imported_evolution_is_a_drop(tmp_path):
    """Fork deleted the file; the imported C commit's content is missing.

    The fork blob is absent and no imported upstream commit ever had it
    absent, so the drop is anchored on the newest imported predecessor
    version (B): the imported C change demonstrably never landed. This is
    the D-status path through the classifier.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])
    _commit(fork, "fork: drop tracked.txt", {"tracked.txt": None}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    # Modify/delete conflict; the bad resolution keeps the file deleted.
    _import(fork, shas["C"], resolve=lambda: _git(fork, "rm", "-q", "-f", "--", "tracked.txt"))

    proc, report = _run_checker(fork)
    assert proc.returncode == 2, f"expected drops found:\n{proc.stdout}{proc.stderr}"
    assert [d["path"] for d in report["drops"]] == ["tracked.txt"]
    drop = report["drops"][0]
    assert drop["status"] == "D"
    assert drop["fork_blob"] is None
    assert drop["matched_upstream_blob"] == _blob(fork, shas["B"], "tracked.txt")
    assert [c["sha"] for c in drop["dropped_commits"]] == [shas["C"]]
    assert drop["dropped_commits"][0]["subject"] == SUBJECT_C


# --- case f: operational error on a bogus ref --------------------------------


def test_bogus_ref_is_an_operational_error(tmp_path):
    """An unresolvable ref is exit 1 with a graceful ::error, not a crash."""
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])

    proc, _ = _run_checker(fork, upstream_ref="refs/remotes/upstream/does-not-exist")
    assert proc.returncode == 1
    assert "::error" in proc.stderr
    assert "Traceback" not in proc.stderr


# --- unit level: the pure classifier with fabricated inputs ------------------


def _load_module():
    spec = importlib.util.spec_from_file_location("fork_sync_integrity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_classify_path_with_fabricated_inputs():
    """Every branch of the pure classifier, no git involved.

    Walks are (sha, blob) newest-first; blob None means the path was
    absent in that upstream commit's tree.
    """
    module = _load_module()
    imported = {"c9", "c8", "c7"}
    walk = [("c9", "bC"), ("c8", "bB"), ("c7", "bA")]

    # Fork carries the OLDEST imported blob while c8 and c9 also imported.
    classification, drop = module.classify_path("p", "bA", walk, imported)
    assert classification == "drop"
    assert drop == {
        "path": "p",
        "status": "M",
        "fork_blob": "bA",
        "matched_upstream_blob": "bA",
        "dropped_commits": ["c9", "c8"],
    }

    # Newest upstream change not imported yet: plain lag.
    assert module.classify_path("p", "bB", walk, {"c8", "c7"}) == ("lag", None)

    # Fork blob matches no imported version: deliberate fork edit.
    assert module.classify_path("p", "bZ", walk, imported) == ("fork_modified", None)

    # Upstream never touched the path: fork-only.
    assert module.classify_path("p", "bZ", [], imported) == ("fork_only", None)

    # Fork deleted the path; imported upstream commits kept it alive.
    classification, drop = module.classify_path("p", None, walk, imported)
    assert classification == "drop"
    assert drop["status"] == "D"
    assert drop["fork_blob"] is None
    assert drop["matched_upstream_blob"] == "bB"
    assert drop["dropped_commits"] == ["c9"]

    # Fork deleted, upstream deleted too (newest, imported): agreement.
    walk_deleted = [("c9", None), ("c8", "bB"), ("c7", "bA")]
    assert module.classify_path("p", None, walk_deleted, imported) == ("current", None)

    # Fork deleted, upstream deleted then re-added (both imported): the
    # re-add never landed — drop via the general rule, anchored on the
    # imported commit where the path was absent.
    walk_readd = [("c9", "bC2"), ("c8", None), ("c7", "bA")]
    classification, drop = module.classify_path("p", None, walk_readd, imported)
    assert classification == "drop"
    assert drop["dropped_commits"] == ["c9"]
    assert drop["matched_upstream_blob"] is None


def test_build_alert_content_caps_listed_paths_and_stays_single_line():
    """The webhook payload convention: one line, capped, run URL appended."""
    module = _load_module()
    report = {
        "fork_ref": "main",
        "upstream_ref": "refs/remotes/upstream/main",
        "drops": [{"path": f"dir/file{i}.py"} for i in range(12)],
    }
    content = module.build_alert_content(report, 5, "https://example/runs/1")
    assert "\n" not in content
    assert "file4" in content
    assert "file5" not in content
    assert "(+7 more)" in content
    assert content.endswith("run: https://example/runs/1")
    assert len(content) <= 900
