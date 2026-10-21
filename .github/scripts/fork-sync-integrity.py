#!/usr/bin/env python3
"""Detect when fork main silently LOST already-imported upstream content.

The fork imports upstream one commit at a time (see fork-sync-sequential.sh),
so every upstream change should eventually be reachable from fork main. It
still has one silent failure mode: a merge can COMPLETE — the upstream commit
becomes an ancestor — while the merge resolution quietly puts an OLD version
of a file back (a bad conflict resolution, a stale patch re-applied). Ancestry
based sync then sees nothing to redo: the commit is "imported" forever, and
the content is gone without any error.

How this checker decides (it never infers intent from bytes alone):

  For every path where the fork and upstream differ, take the upstream history
  of that path and find ``c_k``, the NEWEST upstream commit touching it that is
  already imported (reachable from the frozen fork commit). If the fork
  carries exactly that version, the divergence is only unimported upstream
  work — ordinary lag, never an alert. Otherwise:

    * If that version DID land on the fork at some point and a later fork-side
      commit moved away from it, that is an OBSERVED post-import fork change.
      Fork reverts and deletions of landed content are reported as observed
      changes, never as losses: matching an older upstream blob proves nothing
      about intent.
    * If it never landed, the fork-side commit that should have carried it
      (the import boundary: the oldest commit on the fork's first-parent chain
      that descends from ``c_k``) is examined. When that boundary carries a
      version upstream itself had earlier — including the pre-addition absence
      of an initial addition — the import demonstrably resolved back to stale
      upstream content: a missing-import candidate. When it carries anything
      else, the evidence cannot say whether the imported content survived, so
      the path is an explicit unresolved record, never silently clean.

  Rename/copy census pairs are unresolved records too: a rename is one fact
  about two paths and cannot be proven lossless from blobs. Nothing is ever
  restored automatically; candidates are a detection result, not a proof of
  how the content was lost, and upstream changes that were never imported are
  out of scope by design.

Exit codes: 0 = assessed, no unexplained losses; 2 = missing-import
candidates found; 3 = unresolved integrity evidence (including renames);
1 = operational failure (bad ref, shallow/incomplete history, git error).
Stdlib only; the one network touch (the Discord webhook POST) lives in
send_discord_alert(), reached only when the environment carries
DISCORD_SYNC_ALERT and dry-run is off — the test harness never does.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request

EXIT_CLEAN = 0
EXIT_ERROR = 1
EXIT_CANDIDATES = 2
EXIT_UNRESOLVED = 3

# Same funnel as fork-sync-sequential.sh: the existing Discord webhook secret.
ALERT_WEBHOOK_ENV = "DISCORD_SYNC_ALERT"
# Set by the workflow from its dry_run dispatch input (or --dry-run); skips
# the POST only. Detection and exit codes are untouched either way.
DRY_RUN_ENV = "INTEGRITY_DRY_RUN"
RUN_URL_ENV = "INTEGRITY_RUN_URL"

ALERT_USER_AGENT = "DiscordBot (https://github.com/QuixThe2nd/hermes-ide, fork-sync-integrity)"
ALERT_CONTENT_CAP = 900

# How many (rev, path) pairs are resolved per `git cat-file --batch-check`
# process, and how many shas are handed to one `git show -s`. Responses are
# short lines, so these keep both pipes and argv far below the OS limits
# while cutting process spawns by orders of magnitude.
BLOB_BATCH_CHUNK = 1000
SHOW_BATCH_CHUNK = 400


class IntegrityError(RuntimeError):
    """Operational failure (bad ref, git error) — maps to exit 1."""


def _git(repo, args):
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc


def _checked(repo, args):
    proc = _git(repo, args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        # Keep reasons short and never echo a credential-bearing URL back.
        for token in ("http://", "https://", "git@", "x-access-token"):
            detail = detail.replace(token, "<redacted>")
        raise IntegrityError(f"git {' '.join(args[:3])} ... failed: {detail[:400]}")
    return proc.stdout


def run_git(repo: str | None, *args: str) -> str:
    """Run one git command, returning stdout; any failure is operational."""
    return _checked(repo, args)


def run_git_optional(repo: str | None, *args: str) -> str | None:
    """Run one git command whose documented "unset" answer is exit 1
    (`git config --get`). None means unset; any other failure still raises,
    so a broken repository can never be mistaken for evidence."""
    proc = _git(repo, args)
    if proc.returncode == 0:
        return proc.stdout
    if proc.returncode == 1 and not proc.stderr.strip():
        return None
    return _checked(repo, args)


def resolve_commit(repo: str | None, ref: str) -> str:
    """Full immutable commit sha for *ref*, or raise (bad ref → exit 1)."""
    proc = _git(repo, ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",))
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise IntegrityError(f"cannot resolve ref to a commit: {ref}")
    return sha


def ensure_complete_history(repo: str | None) -> None:
    """Refuse to run against history that could silently lie.

    A shallow clone grafts parents away, so both "newest imported commit" and
    the import boundary would be guessed from a truncated graph. A partial
    (promisor) clone may not even hold the blobs being compared, and satisfying
    one would mean a lazy network fetch inside this library. Both are
    operational errors, never a report.
    """
    is_shallow = run_git(repo, "rev-parse", "--is-shallow-repository").strip()
    if is_shallow != "false":
        raise IntegrityError(
            f"shallow clone ({is_shallow!r}): fork/upstream history is "
            "incomplete, so imported content cannot be assessed; fetch full "
            "history (fetch-depth: 0) and re-run"
        )
    partial = run_git_optional(repo, "config", "--get", "extensions.partialclone")
    if partial and partial.strip():
        raise IntegrityError(
            f"partial clone ({partial.strip()!r}): required blobs may be absent "
            "and fetching is out of scope for this checker"
        )
    promisors = run_git_optional(
        repo, "config", "--get-regexp", r"^remote\.[^ ]+\.promisor$"
    )
    if promisors and any(line.endswith("true") for line in promisors.splitlines()):
        raise IntegrityError(
            "partial clone (promisor remote): required blobs may be absent and "
            "fetching is out of scope for this checker"
        )


# --- pure classification (unit-testable without git) ------------------------


def classify_import_evidence(
    path,
    fork_blob,
    expected_blob,
    expected_landed,
    boundary_blob,
    older_upstream_states,
    pre_add_absence,
    boundary_found,
):
    """Decide one path from already-collected evidence.

    Every input is a fact gathered from git — no dates, no merge-parent
    guessing. Returns ``(classification, detail)``; *detail* is None except
    for the two classifications that need it (candidate, unresolved).

      ``candidate``   missing-import candidate: the import resolved the path
                      back to stale upstream content (exit 2 evidence)
      ``unresolved``  the evidence cannot say (exit 3)
      ``fork_change`` observed post-import fork change (reported, no alert)
      ``lag``         divergence is unimported upstream work
      ``retained``    upstream deleted the path, fork still carries content
    """
    if expected_blob == fork_blob:
        return "lag", None
    if expected_blob is None:
        # Newest imported upstream state is "absent" and the fork has content:
        # the fork retains a superset, so no upstream content is missing.
        return "retained", None
    if expected_landed:
        return "fork_change", None
    if not boundary_found:
        return (
            "unresolved",
            {
                "reason": (
                    "imported change is reachable from the fork but has no "
                    "first-parent import boundary; evidence is incomplete"
                ),
                "boundary_blob": None,
            },
        )
    if boundary_blob is not None and boundary_blob in older_upstream_states:
        return (
            "candidate",
            {
                "reason": (
                    "import resolved the path back to an older upstream "
                    "version; the newest imported change is absent from fork main"
                ),
                "matched_upstream_blob": boundary_blob,
                "matched_index_offset": 0,
            },
        )
    if boundary_blob is None and pre_add_absence:
        # A dropped initial addition: the boundary still shows the real
        # pre-addition tree, which is upstream's own earlier state.
        return (
            "candidate",
            {
                "reason": (
                    "initial addition never landed: the import boundary still "
                    "shows the pre-addition tree"
                ),
                "matched_upstream_blob": None,
                "matched_index_offset": 1,
            },
        )
    if boundary_blob is None:
        return (
            "unresolved",
            {
                "reason": (
                    "import boundary lacks the path where upstream has no "
                    "absent state to fall back on; a fork-side deletion and a "
                    "failed import cannot be told apart here"
                ),
                "boundary_blob": None,
            },
        )
    return (
        "unresolved",
        {
            "reason": (
                "import boundary carries content matching no upstream version "
                "of this path; a fork patch may or may not include the "
                "imported change"
            ),
            "boundary_blob": boundary_blob,
        },
    )


# --- git plumbing (batched where it matters) --------------------------------


def diverged_paths(repo, fork_sha, upstream_sha):
    """[(path, letter, rename_source)] from ``git diff --name-status -z``.

    *rename_source* is None except for rename/copy lines, which are kept
    intact as one fact about two paths instead of being split into a deletion
    plus an addition (a split that would let each side masquerade as evidence
    about the other).
    """
    out = run_git(
        repo, "diff", "--name-status", "--find-renames=50%", "-z",
        fork_sha, upstream_sha,
    )
    fields = [f for f in out.split("\0") if f]
    entries = []
    i = 0
    while i < len(fields):
        head = fields[i]
        i += 1
        letter = head[0].upper() if head else ""
        if letter in ("R", "C") and i + 1 < len(fields):
            entries.append((fields[i + 1], letter, fields[i]))
            i += 2
        else:
            entries.append((fields[i], letter, None))
            i += 1
    return entries


def imported_commit_set(repo, fork_sha, upstream_sha):
    """Set of upstream commits already reachable from the frozen fork commit.

    Reachability, not merge-parent position: an upstream commit counts as
    imported when it is an ancestor of upstream and reachable from the fork,
    whatever edge brought it in. The set is prefix-closed along upstream
    history by construction, so an unimported later change can never mask an
    imported earlier one.
    """
    upstream_all = set(run_git(repo, "rev-list", upstream_sha).split())
    unimported = set(run_git(repo, "rev-list", upstream_sha, f"^{fork_sha}").split())
    return upstream_all - unimported


def path_history(repo, rev, path):
    """[sha, ...] — upstream history of *path* under *rev*, newest first."""
    return run_git(repo, "rev-list", rev, "--", path).split()


def fork_first_parent_transitions(repo, fork_sha, path):
    """[sha, ...] — commits where *path* changed along the fork's first-parent
    chain, newest first. Each one introduced a version of the path; together
    with the tip that is every version the fork's main line ever carried,
    which is what "did this upstream version ever land" must be answered
    from. Commits that left the path identical to their first parent are not
    transitions and introduce no new state."""
    return run_git(
        repo, "rev-list", "--first-parent", fork_sha, "--", path
    ).split()


def import_boundary(repo, fork_sha, commit):
    """Fork-side commit that carried *commit* into the fork, or None.

    The oldest commit on the fork's first-parent chain that still descends
    from *commit* — the import merge itself. None means the first-parent walk
    cannot connect the two (imported through a non-first-parent edge), which
    is reported as incomplete evidence rather than guessed around.
    """
    out = run_git(
        repo, "rev-list", "--ancestry-path", "--first-parent",
        f"{commit}..{fork_sha}",
    ).split()
    return out[-1] if out else None


def resolve_blobs(repo, pairs):
    """{(rev, path): blob_sha or None} via chunked `git cat-file --batch-check`.

    One process per BLOB_BATCH_CHUNK pairs instead of one rev-parse per pair.
    None means the path is absent from that tree; the shallow and partial-clone
    rejection above is what keeps "absent from tree" from ever being confused
    with "object not fetched".
    """
    resolved = {}
    pairs = list(pairs)
    for start in range(0, len(pairs), BLOB_BATCH_CHUNK):
        chunk = pairs[start : start + BLOB_BATCH_CHUNK]
        request = "".join(f"{rev}:{path}\n" for rev, path in chunk)
        proc = _git_stdin(
            repo,
            ("cat-file", "--batch-check"),
            request,
        )
        if proc.returncode != 0:
            raise IntegrityError(
                f"git cat-file --batch-check failed: {proc.stderr.strip()[:400]}"
            )
        lines = proc.stdout.splitlines()
        if len(lines) != len(chunk):
            raise IntegrityError(
                f"cat-file --batch-check returned {len(lines)} results "
                f"for {len(chunk)} requests"
            )
        for (rev, path), line in zip(chunk, lines):
            fields = line.split()
            resolved[(rev, path)] = None if fields[-1] == "missing" else fields[0]
    return resolved


def _git_stdin(repo, args, stdin):
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def enrich_commits(repo, shas):
    """{sha: {"sha", "date", "subject"}} for the evidence lists."""
    unique = list(dict.fromkeys(sha for sha in shas if sha))
    info = {}
    for start in range(0, len(unique), SHOW_BATCH_CHUNK):
        chunk = unique[start : start + SHOW_BATCH_CHUNK]
        out = run_git(repo, "show", "-s", "--format=%H%x1f%cI%x1f%s", *chunk)
        for line in out.splitlines():
            if line.count("\x1f") == 2:
                sha, date, subject = line.split("\x1f")
                info[sha] = {"sha": sha, "date": date, "subject": subject}
    return info


def compute_integrity(repo, fork_ref, upstream_ref, candidate_ref=None):
    """Build the full report dict (see module docstring for the algorithm)."""
    ensure_complete_history(repo)

    # Both sides are frozen to full immutable SHAs once and used as SHAs
    # everywhere after this point.
    assessed_commit = resolve_commit(repo, candidate_ref or fork_ref)
    base_commit = resolve_commit(repo, fork_ref)
    upstream_commit = resolve_commit(repo, upstream_ref)

    census = diverged_paths(repo, assessed_commit, upstream_commit)
    report = {
        "schema_version": 2,
        "generated_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds"),
        "assessed": {
            "fork_ref": fork_ref,
            "fork_commit": base_commit,
            "candidate_ref": candidate_ref,
            "candidate_commit": assessed_commit if candidate_ref else None,
            "upstream_ref": upstream_ref,
            "upstream_commit": upstream_commit,
            "history_complete": True,
        },
        "totals": {
            "census_paths": 0,
            "rename_pair_paths": 0,
            "assessed_paths": 0,
            "missing_import_candidates": 0,
            "unresolved": 0,
            "post_import_fork_changes": 0,
            "lag": 0,
            "retained": 0,
            "fork_only": 0,
            "unaccounted": 0,
        },
        "missing_import_candidates": [],
        "unresolved": [],
        "post_import_fork_changes": [],
        "retained_paths": [],
        "reconciliation": {
            "formula": (
                "census_paths == rename_pair_paths + assessed_paths and "
                "assessed_paths == sum of all per-path classifications"
            ),
            "holds": False,
        },
    }
    totals = report["totals"]

    rename_pairs, single = [], []
    for path, letter, source in census:
        if source is not None:
            # A rename line is one census entry about two paths.
            totals["census_paths"] += 2
            rename_pairs.append((letter, source, path))
        else:
            totals["census_paths"] += 1
            single.append(path)
    totals["rename_pair_paths"] = 2 * len(rename_pairs)
    totals["assessed_paths"] = len(single)

    if rename_pairs:
        report["unresolved"].extend(
            {
                "paths": [source, path],
                "status": f"{letter}100" if letter == "R" else letter,
                "kind": "rename_or_copy",
                "reason": (
                    "rename/copy detected; whether imported upstream content "
                    "survived the rename cannot be proven from blobs"
                ),
            }
            for letter, source, path in rename_pairs
        )
    if not single:
        return _finish(report)

    imported = imported_commit_set(repo, assessed_commit, upstream_commit)
    walks = {path: path_history(repo, upstream_commit, path) for path in single}
    fork_blobs = resolve_blobs(repo, [(assessed_commit, p) for p in single])
    upstream_blobs = resolve_blobs(
        repo, [(sha, path) for path in single for sha in walks[path]]
    )

    # Paths where the fork already carries the newest imported version need no
    # fork-side walk at all — plain lag, usually the overwhelming majority
    # between an upstream push and its import.
    pending = []
    for path in single:
        if not walks[path]:
            # Upstream never touched the path; it cannot have lost anything.
            totals["fork_only"] += 1
            continue
        walk = [(sha, upstream_blobs.get((sha, path))) for sha in walks[path]]
        expected_index = next(
            (idx for idx, (sha, _b) in enumerate(walk) if sha in imported), None
        )
        fork_blob = fork_blobs.get((assessed_commit, path))
        if expected_index is None or walk[expected_index][1] == fork_blob:
            # Nothing of this path was ever imported, or the fork already
            # carries the newest imported version: unimported upstream work.
            totals["lag"] += 1
            continue
        pending.append((path, walk, expected_index, fork_blob))

    transitions = {
        path: fork_first_parent_transitions(repo, assessed_commit, path)
        for path, _w, _i, _f in pending
    }
    state_blobs = resolve_blobs(
        repo,
        [(sha, path) for path, _w, _i, _f in pending for sha in transitions[path]],
    )

    detail_shas = set()
    for path, walk, expected_index, fork_blob in pending:
        expected_sha, expected_blob = walk[expected_index]
        landed = any(
            state_blobs.get((sha, path)) == expected_blob
            for sha in transitions[path]
        )
        boundary_sha = None
        if not landed:
            boundary_sha = import_boundary(repo, assessed_commit, expected_sha)
        boundary_blob = (
            resolve_blobs(repo, [(boundary_sha, path)]).get((boundary_sha, path))
            if boundary_sha
            else None
        )
        older_index = expected_index + 1
        classification, detail = classify_import_evidence(
            path=path,
            fork_blob=fork_blob,
            expected_blob=expected_blob,
            expected_landed=landed,
            boundary_blob=boundary_blob,
            older_upstream_states={b for _s, b in walk[older_index:]},
            pre_add_absence=older_index == len(walk),
            boundary_found=boundary_sha is not None,
        )
        evidence = {
            "path": path,
            "status": "D" if fork_blob is None else "M",
            "fork_blob": fork_blob,
            "expected_blob": expected_blob,
            "expected_commit": {"sha": expected_sha},
        }
        if classification == "lag":
            totals["lag"] += 1
            continue
        if classification == "retained":
            totals["retained"] += 1
            evidence["upstream_commit"] = {"sha": expected_sha}
            detail_shas.add(expected_sha)
            report["retained_paths"].append(evidence)
            continue
        if classification == "fork_change":
            changed_at = next(
                (
                    sha
                    for sha in transitions[path]
                    if state_blobs.get((sha, path)) != expected_blob
                ),
                None,
            )
            evidence["changed_by_commit"] = {"sha": changed_at}
            detail_shas.update({expected_sha, changed_at} - {None})
            totals["post_import_fork_changes"] += 1
            report["post_import_fork_changes"].append(evidence)
            continue

        evidence["boundary_commit"] = {"sha": boundary_sha}
        evidence["boundary_blob"] = boundary_blob
        # matched_index_offset drives the missing-commit arithmetic below and
        # is not evidence; keep it out of the report.
        evidence.update(
            {k: v for k, v in detail.items() if not k.endswith("_index_offset")}
        )
        if classification == "candidate":
            # offset 1 means the fork fell back to the state *before*
            # walk[expected_index] — a dropped initial addition — so that
            # commit and everything newer is missing. offset 0 means it fell
            # back to an older state still in the walk, and everything NEWER
            # than that state is missing. Either way every listed commit is
            # imported and demonstrably absent from the fork's tree.
            if detail.get("matched_index_offset"):
                missing = [
                    sha for sha, _b in walk[expected_index:] if sha in imported
                ]
            else:
                matched_index = next(
                    idx
                    for idx, (_s, blob) in enumerate(walk)
                    if blob == boundary_blob
                )
                missing = [
                    sha for sha, _b in walk[:matched_index] if sha in imported
                ]
            evidence["missing_commits"] = [{"sha": sha} for sha in missing]
            detail_shas.update(missing)
            totals["missing_import_candidates"] += 1
            report["missing_import_candidates"].append(evidence)
        else:
            detail_shas.update({expected_sha, boundary_sha} - {None})
            report["unresolved"].append(evidence)

    details = enrich_commits(repo, detail_shas)
    for section, keys in (
        ("missing_import_candidates", ("expected_commit", "boundary_commit")),
        ("post_import_fork_changes", ("expected_commit", "changed_by_commit")),
        ("retained_paths", ("upstream_commit",)),
    ):
        for record in report[section]:
            for key in keys:
                entry = record.get(key) or {}
                entry.update(details.get(entry.get("sha"), {}))
    for record in report["missing_import_candidates"]:
        for entry in record.get("missing_commits", ()):
            entry.update(details.get(entry["sha"], {}))
    for record in report["unresolved"]:
        entry = record.get("expected_commit") or {}
        entry.update(details.get(entry.get("sha"), {}))
        entry = record.get("boundary_commit") or {}
        entry.update(details.get(entry.get("sha"), {}))
    return _finish(report)


def _finish(report):
    """Fill in the counts and check they reconcile — a report whose numbers do
    not add up would be its own integrity failure."""
    totals = report["totals"]
    totals["missing_import_candidates"] = len(report["missing_import_candidates"])
    totals["unresolved"] = len(report["unresolved"])
    totals["post_import_fork_changes"] = len(report["post_import_fork_changes"])
    classified = (
        totals["missing_import_candidates"]
        + totals["unresolved"]
        + totals["post_import_fork_changes"]
        + totals["lag"]
        + totals["retained"]
        + totals["fork_only"]
    )
    # Rename records are counted once per pair, so they belong on the
    # "expected" side alongside the individually assessed paths.
    totals["unaccounted"] = (
        totals["assessed_paths"] + totals["rename_pair_paths"] // 2 - classified
    )
    report["reconciliation"]["holds"] = (
        totals["unaccounted"] == 0
        and totals["census_paths"]
        == totals["rename_pair_paths"] + totals["assessed_paths"]
    )
    return report


# --- alerting (the only network touch; never called by tests) ---------------


def build_alert_content(report, max_report, run_url=""):
    """Single-line webhook content, same convention as fork-sync-sequential.sh.

    ``--max-report`` caps the paths listed inline so a mass regression cannot
    push the payload past Discord's message limit; the full uncapped list
    always lives in the JSON artifact.
    """
    candidates = report["missing_import_candidates"]
    unresolved = report["unresolved"]
    listed = [d["path"] for d in candidates[:max_report]]
    if not listed and unresolved:
        listed = [d.get("path") or " ".join(d.get("paths", [])) for d in unresolved[:max_report]]
    extra = len(candidates) + len(unresolved) - len(listed)
    paths = ", ".join(p for p in listed if p)
    if extra > 0:
        paths = f"{paths} (+{extra} more)" if paths else f"+{extra} more"
    content = (
        f"fork-sync-integrity: {len(candidates)} path(s) may have silently "
        f"lost imported upstream content"
        + (f", {len(unresolved)} unresolved" if unresolved else "")
        + f" | paths: {paths}"
        f" | fork: {report['assessed']['fork_commit']}"
        f" | upstream: {report['assessed']['upstream_commit']}"
    )
    if run_url:
        content += f" | run: {run_url}"
    return content[:ALERT_CONTENT_CAP]


def send_discord_alert(webhook, content):
    """POST the alert. Delivery failure is downgraded to a warning: it must
    never mask the integrity failure that triggered it (same policy as
    fork-sync-sequential.sh send_alert)."""
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    request = urllib.request.Request(
        webhook,
        data=payload.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Discord recommends webhooks identify as a bot; bare urllib
            # defaults can be filtered.
            "User-Agent": ALERT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001 — any delivery failure is a warning
        print(
            f"::warning::alert delivery failed ({type(exc).__name__}); "
            "the integrity failure above is the real problem",
            file=sys.stderr,
        )


# --- CLI --------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors, which would collide with the
    candidates-found exit code; usage errors are operational errors here."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def main(argv=None):
    parser = _ArgumentParser(
        prog="fork-sync-integrity.py",
        description="Detect silently dropped already-imported upstream content.",
    )
    parser.add_argument(
        "--fork-ref",
        required=True,
        help="fork ref to report on (e.g. main, or a full sha)",
    )
    parser.add_argument(
        "--upstream-ref",
        required=True,
        help="upstream ref to compare against (e.g. refs/remotes/upstream/main)",
    )
    parser.add_argument(
        "--candidate-ref",
        default=None,
        help=(
            "assess this candidate commit instead of --fork-ref's tip while "
            "still reporting --fork-ref as the base, so the manual repair "
            "procedure can run the same detector before publication"
        ),
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="write the full detail JSON here (always written, findings or not)",
    )
    parser.add_argument(
        "--max-report",
        type=int,
        default=10,
        help="max paths listed in the webhook content (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect and report but never deliver the alert "
        "(INTEGRITY_DRY_RUN=true does the same)",
    )
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get(DRY_RUN_ENV) == "true"

    try:
        report = compute_integrity(
            None, args.fork_ref, args.upstream_ref, args.candidate_ref
        )
    except IntegrityError as exc:
        print(
            f"::error title=fork-sync-integrity::operational failure: {exc}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")

    totals = report["totals"]
    assessed = report["assessed"]
    findings = totals["missing_import_candidates"] + totals["unresolved"]
    if findings:
        for drop in report["missing_import_candidates"]:
            missing = drop.get("missing_commits") or []
            named = next(
                (m.get("subject") for m in missing if m.get("subject")), None
            ) or (drop["expected_commit"].get("subject") or "an imported change")
            boundary = (drop.get("boundary_commit") or {}).get("sha") or "?"
            print(
                f"CANDIDATE {drop['status']} {drop['path']}: import "
                f"{boundary[:12]} carries {str(drop.get('boundary_blob'))[:12]} "
                f"while {named} was already imported"
            )
        for record in report["unresolved"]:
            where = record.get("path") or " ".join(record.get("paths", []))
            print(f"UNRESOLVED {record.get('status', '?')} {where}: {record['reason']}")
        print(
            f"fork-sync-integrity: {totals['missing_import_candidates']} "
            f"missing-import candidate(s), {totals['unresolved']} unresolved, "
            f"{totals['post_import_fork_changes']} observed fork change(s), "
            f"{totals['lag']} lagging, of {totals['census_paths']} diverged path(s)"
        )
        print(
            f"fork-sync-integrity: fork {assessed['fork_commit']} "
            f"({assessed['fork_ref']}) upstream {assessed['upstream_commit']} "
            f"({assessed['upstream_ref']})"
        )
        if os.environ.get(ALERT_WEBHOOK_ENV):
            if dry_run:
                print(
                    "fork-sync-integrity: dry run — alert suppressed",
                    file=sys.stderr,
                )
            else:
                send_discord_alert(
                    os.environ[ALERT_WEBHOOK_ENV],
                    build_alert_content(
                        report,
                        args.max_report,
                        os.environ.get(RUN_URL_ENV, ""),
                    ),
                )
        if totals["missing_import_candidates"]:
            return EXIT_CANDIDATES
        return EXIT_UNRESOLVED

    print(
        f"fork-sync-integrity: clean — {totals['census_paths']} diverged "
        f"path(s), all lag/observed fork-side (lag {totals['lag']}, fork-only "
        f"{totals['fork_only']}, observed fork changes "
        f"{totals['post_import_fork_changes']}, retained {totals['retained']}, "
        f"unresolved {totals['unresolved']})"
    )
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
