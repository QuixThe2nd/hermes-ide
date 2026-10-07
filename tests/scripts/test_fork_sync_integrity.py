"""Acceptance tests for .github/scripts/fork-sync-integrity.py.

Built the way the fork-sync acceptance setup works: real git in temporary
repositories under pytest tmp_path, driving the exact product file — the CLI
via subprocess for end-to-end behaviour, and its collectors/classifier
imported straight from the script for the pieces that are impractical to
reach through a whole repository. Nothing here fabricates a walk as its only
proof: every scenario is a real repository with real imports.

No network anywhere. The two alert tests bind a socket on 127.0.0.1 and
point DISCORD_SYNC_ALERT at it, which proves both that the funnel fires and
that dry-run suppresses it without touching anything off-box.

Shared history shape: upstream ``tracked.txt`` evolves A -> B -> C (one
commit each, fixed dates). The fork is cloned from the upstream bare, reset
to the pre-file root commit, then imports upstream commits one at a time as
real --no-ff merges whose tree is whatever the test resolves — which is the
only way a merge can "succeed" while silently dropping content.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "fork-sync-integrity.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fork-sync-integrity.yml"

# Fixed author/committer dates keep evidence deterministic; the report carries
# committer dates (%cI), which git renders as "Z" for a +00:00 fixture date.
D_ROOT = "2024-01-01T00:00:00+00:00"
D_A = "2024-01-02T00:00:00+00:00"
D_B = "2024-01-03T00:00:00+00:00"
D_C = "2024-01-04T00:00:00+00:00"
D_FORK = "2024-01-05T00:00:00+00:00"

SUBJECT_A = "feat: tracked.txt to A"
SUBJECT_B = "feat: tracked.txt to B"
SUBJECT_C = "feat: tracked.txt to C"


# --- fixture helpers ---------------------------------------------------------


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
            _git(repo, "rm", "-q", "-f", "--", rel)
        else:
            (Path(repo) / rel).write_text(content)
            _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _build_fork(tmp_path, upstream_commits, fork_seed=None):
    """Bare upstream.git + fork.git, plus a fork work clone.

    *upstream_commits* is a list of ``(key, message, files, date)`` tuples.
    The fork is cloned from upstream then reset to *fork_seed's* commit
    (default: the first upstream commit), optionally seeded with its own
    commit, and pushed to fork.git. The work clone carries
    refs/remotes/upstream/main (the ref the CLI is pointed at in CI) and
    remote origin = fork.git, mirroring the real fork's shape.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    shas = {}
    for key, message, files, date in upstream_commits:
        shas[key] = _commit(upstream, message, files, date)

    upstream_git = str(tmp_path / "upstream.git")
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", upstream_git)
    _git(upstream, "push", "-q", upstream_git, "main")
    fork_git = str(tmp_path / "fork.git")
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", fork_git)

    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", upstream_git, str(fork))
    _git(fork, "remote", "rename", "origin", "upstream")
    seed = fork_seed or {}
    _git(fork, "reset", "-q", "--hard", shas[seed.get("at", upstream_commits[0][0])])
    if seed.get("files"):
        _commit(fork, seed["message"], seed["files"], D_FORK)
    _git(fork, "remote", "add", "origin", fork_git)
    _git(fork, "push", "-q", "origin", "main")
    return fork, shas


def _upstream_abc(tmp_path):
    """The base scenario: tracked.txt evolves A -> B -> C upstream."""
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
    """Import one upstream commit fork-sync style: a --no-ff merge whose tree
    is whatever *resolve* staged (None = take the clean merge result).

    A conflicted merge without a resolver is a fixture bug, not a scenario.
    """
    proc = _git(fork, "merge", "--no-ff", "--no-commit", "--no-edit", sha, check=False)
    if proc.returncode != 0 and resolve is None:
        raise AssertionError(
            f"unexpected merge conflict importing {sha}:\n{proc.stdout}{proc.stderr}"
        )
    if resolve is not None:
        resolve(fork)
    _git(fork, "commit", "-q", "-m", f"Merge upstream {sha[:9]}", date=D_FORK)
    _git(fork, "push", "-q", "origin", "main")


def _keep(path, content):
    """Resolver that forces *path* to *content* (None deletes it)."""

    def resolve(fork):
        if content is None:
            _git(fork, "rm", "-q", "-f", "--", path)
        else:
            (Path(fork) / path).write_text(content)
            _git(fork, "add", "-f", "--", path)

    return resolve


def _blob(fork, rev, path):
    return _git(fork, "rev-parse", f"{rev}:{path}").stdout.strip()


def _merge_commit(fork, subject_part):
    """Sha of the fork merge commit whose subject mentions *subject_part*."""
    out = _git(fork, "log", "--format=%H %s", "main").stdout
    for line in out.splitlines():
        sha, subject = line.split(" ", 1)
        if subject_part in subject:
            return sha
    raise AssertionError(f"no merge commit matching {subject_part!r}")


def _run_checker(fork, *extra_args, upstream_ref="refs/remotes/upstream/main",
                 env_extra=None):
    """Run the real CLI inside the fork clone; return (proc, report|None)."""
    json_out = Path(fork).parent / "integrity-report.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DISCORD_SYNC_ALERT" and not key.startswith("INTEGRITY_")
    }
    env.update(env_extra or {})
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


def _load_module():
    """The product script as a module — no copy, no reimplementation."""
    spec = importlib.util.spec_from_file_location("fork_sync_integrity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --- case 1: an import resolved back to an older upstream version ------------


def test_import_resolved_back_to_older_upstream_version_is_a_candidate(tmp_path):
    """Imported through B, then the C merge resolves back to A's bytes.

    Every commit is imported, yet the newest imported content for the path is
    not what fork main carries — exactly the silent drop this checker exists
    for. The stale A was reapplied fork-side first, so importing C conflicted
    and the (bad) resolution kept A.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])
    _commit(fork, "fix: reapply A (stale patch)", {"tracked.txt": "A\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")
    _import(fork, shas["C"], resolve=_keep("tracked.txt", "A\n"))

    proc, report = _run_checker(fork)
    assert proc.returncode == 2, f"expected candidates:\n{proc.stdout}{proc.stderr}"
    assert "tracked.txt" in proc.stdout

    assert [d["path"] for d in report["missing_import_candidates"]] == ["tracked.txt"]
    drop = report["missing_import_candidates"][0]
    blob_a = _blob(fork, shas["A"], "tracked.txt")
    assert drop["status"] == "M"
    assert drop["fork_blob"] == blob_a
    assert drop["expected_blob"] == _blob(fork, shas["C"], "tracked.txt")
    assert drop["boundary_blob"] == blob_a
    # The boundary is the C import merge itself, named by full sha.
    assert drop["boundary_commit"]["sha"] == _merge_commit(fork, shas["C"][:9])
    # Both imported changes the fork's tree fails to carry, newest first.
    assert [c["sha"] for c in drop["missing_commits"]] == [shas["C"], shas["B"]]
    assert drop["missing_commits"][0] == {
        "sha": shas["C"],
        "date": D_C.replace("+00:00", "Z"),
        "subject": SUBJECT_C,
    }
    assert report["totals"]["missing_import_candidates"] == 1
    assert report["reconciliation"]["holds"] is True
    assert report["assessed"]["fork_ref"] == "main"
    assert report["assessed"]["upstream_ref"] == "refs/remotes/upstream/main"
    assert report["assessed"]["upstream_commit"] == _git(
        fork, "rev-parse", "refs/remotes/upstream/main"
    ).stdout.strip()
    assert len(report["assessed"]["upstream_commit"]) == 40


# --- case 2: an omitted initial addition -------------------------------------


def test_omitted_initial_addition_is_a_candidate(tmp_path):
    """An upstream commit that ADDS a file was imported without the file.

    There is no older blob to fall back on; the evidence is the import
    boundary still showing the real pre-addition tree. This is the case a
    blob-diff-only checker cannot see at all.
    """
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("N", "feat: add new.txt", {"new.txt": "added\n"}, D_A),
            ("M", "feat: edit new.txt", {"new.txt": "edited\n"}, D_B),
        ],
    )
    drop_new = _keep("new.txt", None)
    _import(fork, shas["N"], resolve=drop_new)

    proc, report = _run_checker(fork)
    assert proc.returncode == 2, f"expected candidates:\n{proc.stdout}{proc.stderr}"
    assert [d["path"] for d in report["missing_import_candidates"]] == ["new.txt"]
    drop = report["missing_import_candidates"][0]
    assert drop["status"] == "D"
    assert drop["fork_blob"] is None
    assert drop["boundary_blob"] is None
    assert drop["expected_commit"]["sha"] == shas["N"]
    assert [c["sha"] for c in drop["missing_commits"]] == [shas["N"]]
    assert "pre-addition" in drop["reason"]
    # The later, still unimported upstream edit is not required for detection
    # and is not reported as missing either: the path is already accounted for
    # by the candidate, so nothing else is left to call lag.
    assert report["totals"]["lag"] == 0


# --- case 3: genuine lag -----------------------------------------------------


def test_unimported_upstream_advance_is_lag_not_a_candidate(tmp_path):
    """Fork through B, upstream advanced to C (not imported): stay quiet.

    The census still diverges (B vs C) — alerting here would cry wolf every
    hour between an upstream push and its import.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report is not None  # detail JSON is written on clean runs too
    assert report["missing_import_candidates"] == []
    assert report["totals"]["lag"] == 1
    assert report["reconciliation"]["holds"] is True


# --- case 4: an unimported later change must not mask a missing earlier one --


def test_missing_imported_change_is_not_masked_by_later_unimported_one(tmp_path):
    """B was imported but its content is absent, while C is still unimported.

    Anchoring on the upstream *tip* would call this plain lag and stay quiet
    forever. The anchor must be the newest IMPORTED change to the path.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"], resolve=_keep("tracked.txt", "A\n"))

    proc, report = _run_checker(fork)
    assert proc.returncode == 2, f"expected candidates:\n{proc.stdout}{proc.stderr}"
    assert [d["path"] for d in report["missing_import_candidates"]] == ["tracked.txt"]
    drop = report["missing_import_candidates"][0]
    # Only the imported-but-absent B is missing; C is not imported yet and
    # must not be listed as lost.
    assert [c["sha"] for c in drop["missing_commits"]] == [shas["B"]]
    assert drop["missing_commits"][0]["subject"] == SUBJECT_B
    assert drop["expected_commit"]["sha"] == shas["B"]


# --- case 5: a post-import revert to old upstream bytes ----------------------


def test_post_import_revert_to_old_upstream_bytes_is_not_a_loss(tmp_path):
    """Fully imported, then a fork commit deliberately reverts to A's bytes.

    Blob equality with an older upstream version proves nothing about intent:
    the revert is an observed fork-side change after the content landed, and
    must never be reported as a lost import.
    """
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B", "C"):
        _import(fork, shas[key])
    revert_sha = _commit(
        fork, "revert: back to A", {"tracked.txt": "A\n"}, D_FORK
    )
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert report["totals"]["post_import_fork_changes"] == 1
    change = report["post_import_fork_changes"][0]
    assert change["path"] == "tracked.txt"
    assert change["fork_blob"] == _blob(fork, shas["A"], "tracked.txt")
    assert change["expected_commit"]["sha"] == shas["C"]
    assert change["changed_by_commit"]["sha"] == revert_sha
    assert revert_sha in proc.stdout or report["totals"]["post_import_fork_changes"]


# --- case 6: a post-import deletion ------------------------------------------


def test_post_import_deletion_is_not_a_loss(tmp_path):
    """Fully imported, then the fork deletes the path itself.

    Same rule as the revert: the deletion is observed on the fork side, after
    the content demonstrably landed, so it is reported as an observed change
    and stays quiet.
    """
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B", "C"):
        _import(fork, shas[key])
    delete_sha = _commit(fork, "fork: drop tracked.txt", {"tracked.txt": None}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert report["totals"]["post_import_fork_changes"] == 1
    change = report["post_import_fork_changes"][0]
    assert change["status"] == "D"
    assert change["fork_blob"] is None
    assert change["changed_by_commit"]["sha"] == delete_sha


# --- case 7: a fork-only file ------------------------------------------------


def test_fork_only_file_is_not_a_loss(tmp_path):
    """A path upstream never touched cannot have lost upstream content."""
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B", "C"):
        _import(fork, shas[key])
    _commit(fork, "fork: new file", {"fork_only.txt": "only here\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert report["totals"]["fork_only"] == 1


# --- case 8: an unrelated fork edit to an upstream path ----------------------


def test_unrelated_fork_edit_is_reported_as_observed_change(tmp_path):
    """The fork edits a second upstream path it fully imported.

    The edit must neither become a loss nor hide the fact that the fork moved
    the path: it is an observed post-import fork change, and the totals still
    reconcile.
    """
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"tracked.txt": "A\n", "other.txt": "o1\n"}, D_A),
            ("B", SUBJECT_B, {"other.txt": "o2\n"}, D_B),
        ],
    )
    for key in ("A", "B"):
        _import(fork, shas[key])
    _commit(fork, "fork: tweak other.txt", {"other.txt": "o2+fork\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert report["totals"]["post_import_fork_changes"] == 1
    assert report["post_import_fork_changes"][0]["path"] == "other.txt"
    assert report["reconciliation"]["holds"] is True
    assert report["totals"]["census_paths"] == report["totals"]["assessed_paths"]


# --- case 9: an upstream deletion -------------------------------------------


def test_upstream_deletion_does_not_fabricate_a_loss(tmp_path):
    """Upstream deleted f.txt and the fork has not imported that deletion.

    The fork carrying content upstream no longer has is the opposite of a
    loss: it must stay quiet, not be reported as a missing import.
    """
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"f.txt": "f\n"}, D_A),
            ("D", "feat: delete f.txt", {"f.txt": None}, D_B),
            ("E", "feat: add g.txt", {"g.txt": "g\n"}, D_C),
        ],
    )
    _import(fork, shas["A"])

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert report["totals"]["lag"] >= 1


def test_fork_imports_upstream_deletion_and_stays_clean(tmp_path):
    """Both sides end up without the path: nothing to report at all."""
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"f.txt": "f\n"}, D_A),
            ("D", "feat: delete f.txt", {"f.txt": None}, D_B),
        ],
    )
    for key in ("A", "D"):
        _import(fork, shas[key])

    proc, report = _run_checker(fork)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["totals"]["census_paths"] == 0
    assert report["missing_import_candidates"] == []


# --- case 10: a rename -------------------------------------------------------


def test_rename_is_an_explicit_unresolved_record(tmp_path):
    """Upstream renamed tracked.txt -> renamed.txt; the fork renamed it back.

    A rename is one fact about two paths, and blobs cannot prove that no
    content was lost across it: it must stay an explicit unresolved record
    rather than being split into a deletion and an addition.
    """
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"tracked.txt": "A\n"}, D_A),
            ("R", "refactor: rename tracked.txt", {"tracked.txt": None, "renamed.txt": "A\n"}, D_B),
        ],
    )
    for key in ("A", "R"):
        _import(fork, shas[key])
    _commit(
        fork, "fork: rename it back", {"renamed.txt": None, "tracked.txt": "A\n"}, D_FORK
    )
    _git(fork, "push", "-q", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 3, f"expected unresolved:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert len(report["unresolved"]) == 1
    record = report["unresolved"][0]
    assert record["paths"] == ["tracked.txt", "renamed.txt"]
    assert record["kind"] == "rename_or_copy"
    assert "rename" in record["reason"].lower()
    assert report["totals"]["rename_pair_paths"] == 2
    assert report["reconciliation"]["holds"] is True


# --- case 11: a fork patch that may omit an imported update ------------------


def test_fork_patch_over_imported_update_is_unresolved_not_clean(tmp_path):
    """The fork carries its own patch; upstream's imported update is missing.

    The fork blob matches no upstream version, which is NOT proof that the
    fork's content supersedes the imported change. Mixed content like this
    must stay an explicit unresolved record — never silently clean, and never
    labelled a deliberate fork edit.
    """
    fork, shas = _build_fork(
        tmp_path,
        [
            ("root", "chore: root", {"root.txt": "root\n"}, D_ROOT),
            ("A", SUBJECT_A, {"tracked.txt": "A\n"}, D_A),
            ("B", SUBJECT_B, {"tracked.txt": "B\n"}, D_B),
        ],
        fork_seed={
            "message": "fork: carry a local patch",
            "files": {"tracked.txt": "A + fork patch\n"},
        },
    )
    _import(fork, shas["A"], resolve=_keep("tracked.txt", "A + fork patch\n"))
    _import(fork, shas["B"], resolve=_keep("tracked.txt", "A + fork patch\n"))

    proc, report = _run_checker(fork)
    assert proc.returncode == 3, f"expected unresolved:\n{proc.stdout}{proc.stderr}"
    assert report["missing_import_candidates"] == []
    assert len(report["unresolved"]) == 1
    record = report["unresolved"][0]
    assert record["path"] == "tracked.txt"
    assert record["status"] == "M"
    assert "matching no upstream version" in record["reason"]
    assert record["expected_commit"]["sha"] == shas["B"]
    assert report["reconciliation"]["holds"] is True


# --- case 12: an invalid ref -------------------------------------------------


def test_invalid_ref_is_an_operational_error(tmp_path):
    """An unresolvable ref is exit 1 with a graceful ::error, not a crash."""
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])

    proc, report = _run_checker(
        fork, upstream_ref="refs/remotes/upstream/does-not-exist"
    )
    assert proc.returncode == 1
    assert report is None or report is not None  # no report is fine either way
    assert "::error" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "does-not-exist" in proc.stderr


# --- case 13: shallow / incomplete history ----------------------------------


def test_shallow_history_is_rejected(tmp_path):
    """A shallow clone cannot answer "newest imported" honestly: exit 1.

    Refusing is the point — a truncated graph would let the checker invent
    roots and report a confident answer built on missing history.
    """
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B"):
        _import(fork, shas[key])
    _git(fork, "fetch", "-q", "--depth=1", "origin", "main")

    proc, report = _run_checker(fork)
    assert proc.returncode == 1, f"expected operational error:\n{proc.stdout}{proc.stderr}"
    assert "shallow" in proc.stderr.lower()
    assert "Traceback" not in proc.stderr
    # No report is written, so nothing can be mistaken for an assessment.
    assert report is None


def test_partial_clone_is_rejected(tmp_path):
    """A promisor clone may not hold the blobs being compared: exit 1."""
    fork, shas = _upstream_abc(tmp_path)
    for key in ("A", "B"):
        _import(fork, shas[key])
    _git(str(tmp_path / "fork.git"), "config", "uploadpack.allowFilter", "true")
    partial = tmp_path / "partial.git"
    _git(
        tmp_path,
        "clone", "-q", "--bare", "--filter=blob:none",
        f"file://{tmp_path / 'fork.git'}", str(partial),
    )
    _git(partial, "remote", "add", "upstream", str(tmp_path / "upstream.git"))
    _git(partial, "fetch", "-q", "upstream", "main")

    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--fork-ref", "main",
            "--upstream-ref", "refs/remotes/upstream/main",
        ],
        cwd=partial,
        capture_output=True,
        text=True,
        timeout=300,
        env={k: v for k, v in os.environ.items() if k != "DISCORD_SYNC_ALERT"},
    )
    assert proc.returncode == 1, f"expected operational error:\n{proc.stdout}{proc.stderr}"
    assert "partial clone" in proc.stderr
    assert "Traceback" not in proc.stderr


# --- CLI surface: candidate refs and the alert funnel -----------------------


def test_candidate_ref_assesses_the_candidate_not_the_base(tmp_path):
    """The manual repair procedure runs the same detector before publishing.

    fork main is broken; a candidate commit repairs it. The detector must
    assess the candidate and still report both identities as full SHAs.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"], resolve=_keep("tracked.txt", "A\n"))
    broken = _git(fork, "rev-parse", "main").stdout.strip()
    # The repair is prepared on its own branch, not yet on main.
    _git(fork, "checkout", "-q", "-b", "repair")
    _commit(fork, "repair: reapply B", {"tracked.txt": "B\n"}, D_FORK)
    candidate = _git(fork, "rev-parse", "repair").stdout.strip()
    _git(fork, "checkout", "-q", "main")

    proc, report = _run_checker(fork, "--candidate-ref", candidate)
    assert proc.returncode == 0, f"expected clean:\n{proc.stdout}{proc.stderr}"
    assert report["assessed"]["candidate_commit"] == candidate
    assert report["assessed"]["fork_commit"] == broken
    assert report["assessed"]["fork_ref"] == "main"
    assert report["missing_import_candidates"] == []

    # And the same detector still flags the base it would replace.
    proc_base, report_base = _run_checker(fork)
    assert proc_base.returncode == 2
    assert report_base["assessed"]["fork_commit"] == broken


@pytest.fixture()
def alert_socket():
    """A loopback listener standing in for the webhook endpoint."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.settimeout(10)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        yield server
    finally:
        server.close()


def _candidate_scenario(tmp_path):
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"], resolve=_keep("tracked.txt", "A\n"))
    return fork, shas


def test_alert_fires_to_local_funnel_without_candidates_delivery(tmp_path, alert_socket):
    """DISCORD_SYNC_ALERT reaches the loopback endpoint on exit 2."""
    fork, _shas = _candidate_scenario(tmp_path)
    host, port = alert_socket.getsockname()
    proc, _report = _run_checker(
        fork,
        env_extra={"DISCORD_SYNC_ALERT": f"http://{host}:{port}/hook"},
    )
    assert proc.returncode == 2
    connection, _ = alert_socket.accept()
    with connection:
        body = connection.recv(4096).decode("utf-8", "replace")
    assert '"allowed_mentions": {"parse": []}' in body
    assert "may have silently" in body


def test_dry_run_suppresses_delivery_but_keeps_detection_exit(tmp_path, alert_socket):
    """dry-run: no POST at all, and the exit code still reports the finding."""
    fork, _shas = _candidate_scenario(tmp_path)
    host, port = alert_socket.getsockname()
    proc, report = _run_checker(
        fork,
        env_extra={
            "DISCORD_SYNC_ALERT": f"http://{host}:{port}/hook",
            "INTEGRITY_DRY_RUN": "true",
        },
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert report["totals"]["missing_import_candidates"] == 1
    assert "dry run" in proc.stderr
    with pytest.raises(socket.timeout):
        alert_socket.accept()


def test_dry_run_flag_suppresses_delivery(tmp_path, alert_socket):
    """--dry-run behaves like INTEGRITY_DRY_RUN=true."""
    fork, _shas = _candidate_scenario(tmp_path)
    host, port = alert_socket.getsockname()
    proc, _report = _run_checker(
        fork,
        "--dry-run",
        env_extra={"DISCORD_SYNC_ALERT": f"http://{host}:{port}/hook"},
    )
    assert proc.returncode == 2
    with pytest.raises(socket.timeout):
        alert_socket.accept()


# --- product classification logic, driven from the real script ---------------


def test_classifier_distinguishes_candidate_from_observed_change():
    """Branch coverage of the pure decision, fed by the collectors in use.

    The shapes here are the ones the repository scenarios above produce; the
    real-repo tests remain the proof that the collectors feed these facts.
    """
    module = _load_module()
    candidate = module.classify_import_evidence(
        path="p",
        fork_blob="bA",
        expected_blob="bC",
        expected_landed=False,
        boundary_blob="bA",
        older_upstream_states={"bA", "bB"},
        pre_add_absence=False,
        boundary_found=True,
    )
    assert candidate[0] == "candidate"
    assert candidate[1]["matched_upstream_blob"] == "bA"

    observed = module.classify_import_evidence(
        path="p", fork_blob="bZ", expected_blob="bC", expected_landed=True,
        boundary_blob=None, older_upstream_states={"bB"}, pre_add_absence=False,
        boundary_found=True,
    )
    assert observed == ("fork_change", None)

    retained = module.classify_import_evidence(
        path="p", fork_blob="bA", expected_blob=None, expected_landed=False,
        boundary_blob=None, older_upstream_states={"bA"}, pre_add_absence=False,
        boundary_found=True,
    )
    assert retained == ("retained", None)

    mixed = module.classify_import_evidence(
        path="p", fork_blob="bFork", expected_blob="bB", expected_landed=False,
        boundary_blob="bFork", older_upstream_states={"bA"}, pre_add_absence=False,
        boundary_found=True,
    )
    assert mixed[0] == "unresolved"

    dropped_addition = module.classify_import_evidence(
        path="p", fork_blob=None, expected_blob="bN", expected_landed=False,
        boundary_blob=None, older_upstream_states=set(), pre_add_absence=True,
        boundary_found=True,
    )
    assert dropped_addition[0] == "candidate"

    lag = module.classify_import_evidence(
        path="p", fork_blob="bB", expected_blob="bB", expected_landed=False,
        boundary_blob=None, older_upstream_states=set(), pre_add_absence=False,
        boundary_found=True,
    )
    assert lag == ("lag", None)

    no_boundary = module.classify_import_evidence(
        path="p", fork_blob="bZ", expected_blob="bC", expected_landed=False,
        boundary_blob=None, older_upstream_states=set(), pre_add_absence=False,
        boundary_found=False,
    )
    assert no_boundary[0] == "unresolved"


def test_imported_set_is_prefix_closed_over_real_history(tmp_path):
    """The imported set is reachability from the fork, not merge-parent shape.

    An unimported later upstream commit must not evict earlier imported ones
    from the set — that is what keeps an unimported change from masking a
    missing earlier one.
    """
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])
    module = _load_module()
    fork_sha = _git(fork, "rev-parse", "main").stdout.strip()
    upstream_sha = _git(fork, "rev-parse", "refs/remotes/upstream/main").stdout.strip()
    imported = module.imported_commit_set(str(fork), fork_sha, upstream_sha)
    assert imported == {shas["root"], shas["A"], shas["B"]}
    assert shas["C"] not in imported


def test_import_boundary_finds_the_import_merge(tmp_path):
    """The boundary is the fork-side merge that carried the commit in."""
    fork, shas = _upstream_abc(tmp_path)
    _import(fork, shas["A"])
    _import(fork, shas["B"])
    _commit(fork, "fork: unrelated", {"unrelated.txt": "u\n"}, D_FORK)
    _git(fork, "push", "-q", "origin", "main")
    module = _load_module()
    fork_sha = _git(fork, "rev-parse", "main").stdout.strip()
    boundary = module.import_boundary(str(fork), fork_sha, shas["B"])
    assert boundary == _merge_commit(fork, shas["B"][:9])
    # A commit that is not imported yet has no fork-side boundary at all.
    assert module.import_boundary(str(fork), fork_sha, shas["C"]) is None


# --- workflow: the checker that runs is the checker that shipped -------------


def _load_workflow():
    """Parse the workflow preserving the `on:` key.

    PyYAML's safe loader resolves the unquoted key `on` to True (YAML 1.1
    booleans); BaseLoader leaves every scalar a string, which is what a
    workflow actually says.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML needed to read the workflow")
    text = WORKFLOW.read_text()
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(data.get("on"), dict), "workflow must keep a literal `on:` key"
    return data, text


def _steps(data):
    return data["jobs"]["integrity"]["steps"]


def _step_named(steps, needle):
    for step in steps:
        if needle in str(step.get("name", "")) or needle in str(step.get("id", "")):
            return step
    raise AssertionError(f"no workflow step matching {needle!r}")


def test_workflow_runs_its_own_revision_not_forced_main():
    """The checker that executes must be the one shipped with the workflow.

    Checking out `ref: main` would run whatever checker bytes main happens to
    hold — possibly older, possibly absent — regardless of which workflow
    revision was selected.
    """
    data, _text = _load_workflow()
    checkout = _steps(data)[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert re.fullmatch(r"[0-9a-f]{40}", checkout["uses"].split("@", 1)[1])
    options = checkout.get("with", {})
    assert options.get("fetch-depth") == "0", "full history is required"
    ref = options.get("ref")
    assert ref is None or "github.sha" in ref, (
        f"checkout must use the selected workflow revision, got {ref!r}"
    )
    assert ref != "main"
    # The CLI step invokes the script by its in-tree path, so the bytes that
    # run are the bytes checked out above.
    check = _step_named(_steps(data), "Check")
    assert str(check["run"]).count(".github/scripts/fork-sync-integrity.py") == 1


def test_workflow_assesses_a_resolved_fork_main_not_the_checkout_name():
    """The ref handed to the CLI comes from an explicit fetch + rev-parse.

    Passing the literal name `main` would conflate the ref the job happened to
    check out with the ref under examination; the workflow must pin a full
    immutable SHA for both sides.
    """
    data, _text = _load_workflow()
    steps = _steps(data)
    resolve = _step_named(steps, "Resolve")
    run = str(resolve["run"])
    assert "git fetch" in run, "fork main must be fetched explicitly"
    assert "rev-parse" in run
    # A failed resolution must fail the step, never hand the CLI an empty or
    # partial ref.
    assert "set -euo pipefail" in run
    names = re.findall(r"([A-Z_]+_SHA)=", run)
    assert names, f"resolve step defines no sha variable:\n{run}"

    check = _step_named(steps, "Check")
    check_run = str(check["run"])
    for variable in names:
        assert f"${variable}" in check_run or f"{{{variable}}}" in check_run, (
            f"{variable} must be what the CLI is given, not a ref name"
        )
    assert "--fork-ref main" not in check_run
    assert "--upstream-ref main" not in check_run
    assert str(check["env"].get("DISCORD_SYNC_ALERT", "")).startswith("${{ secrets.")
    # Upstream is fetched from the real upstream URL, as before.
    fetch = _step_named(steps, "Fetch upstream")
    assert "remote add upstream" in str(fetch["run"])
    assert "NousResearch/hermes-agent" in str(fetch["run"])


def test_workflow_resolve_step_yields_full_immutable_shas(tmp_path):
    """The resolve step's own commands, run verbatim against local remotes."""
    data, _text = _load_workflow()
    run = str(_step_named(_steps(data), "Resolve")["run"])

    origin_git = str(tmp_path / "origin.git")
    upstream_git = str(tmp_path / "upstream.git")
    for bare in (origin_git, upstream_git):
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "f.txt").write_text("x\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed", date=D_ROOT)
    _git(seed, "push", "-q", origin_git, "main")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "upstream tip", date=D_A)
    _git(seed, "push", "-q", upstream_git, "main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", origin_git, str(work))
    # Simulate the workflow's earlier "Fetch upstream main" step, which is
    # what puts refs/remotes/upstream/main in place for the resolve step.
    _git(work, "remote", "add", "upstream", upstream_git)
    _git(work, "fetch", "-q", "--no-tags", "upstream", "main")

    output_file = tmp_path / "github_output"
    env = dict(os.environ, GITHUB_OUTPUT=str(output_file))
    proc = subprocess.run(
        ["bash", "-c", run],
        cwd=work, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    resolved = dict(
        line.split("=", 1) for line in output_file.read_text().splitlines() if "=" in line
    )
    assert resolved["fork_sha"] == _git(work, "rev-parse", "main").stdout.strip()
    assert resolved["upstream_sha"] == _git(
        work, "rev-parse", "refs/remotes/upstream/main"
    ).stdout.strip()
    assert all(re.fullmatch(r"[0-9a-f]{40}", v) for v in resolved.values())


def test_workflow_keeps_schedule_bound_artifact_and_dry_run():
    """The operational contract of the scheduled run is unchanged."""
    data, text = _load_workflow()
    triggers = data["on"]
    assert triggers["schedule"] == [{"cron": "53 * * * *"}]
    assert "workflow_dispatch" in triggers
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"

    job = data["jobs"]["integrity"]
    assert job["timeout-minutes"] == "30"
    # contents:read may sit at workflow or job level; it must be exactly that.
    granted = data.get("permissions") or job.get("permissions") or {}
    assert granted == {"contents": "read"}
    concurrency = data.get("concurrency") or job.get("concurrency") or {}
    assert concurrency["cancel-in-progress"] == "false"
    assert "INTEGRITY_DRY_RUN" in job["env"]
    assert "inputs.dry_run" in str(job["env"]["INTEGRITY_DRY_RUN"])

    upload = _step_named(_steps(data), "Upload")
    assert upload["if"] == "always()", "artifact must upload on a nonzero exit"
    assert re.fullmatch(r"[0-9a-f]{40}", upload["uses"].split("@", 1)[1])
    assert "fork-sync-integrity" in upload["with"]["name"]

    # The alert leaves only through the existing funnel, once.
    assert text.count("secrets.DISCORD_SYNC_ALERT") == 1


def test_workflow_adds_no_credential_persistence_or_wider_scopes():
    """Token use stays where it was: one checkout, contents: read."""
    _data, text = _load_workflow()
    assert "contents: write" not in text
    assert text.count("secrets.FORK_SYNC_TOKEN") == 1
    assert "persist-credentials" not in text

