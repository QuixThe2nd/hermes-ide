#!/usr/bin/env python3
"""Detect when fork main silently LOST already-imported upstream content.

The fork imports upstream one commit at a time (see fork-sync-sequential.sh),
so every upstream change should eventually be reachable from fork main. It
still has one silent failure mode: a merge can COMPLETE — the upstream commit
becomes an ancestor via the merge's second parent — while the merge resolution
quietly puts an OLD version of a file back (a bad conflict resolution, a
stale patch re-applied). Ancestry-based sync then sees nothing to redo: the
commit is "imported" forever, and the content is gone forever.

This checker compares BLOBS, not ancestry: for every path where the fork's
blob differs from upstream main's blob, it asks whether the newest upstream
commit touching that path is already imported (second parent of a fork merge)
while the fork's current blob matches only an OLDER imported version of that
path. That combination is a silent drop and is the ONLY thing that alerts —
plain lag (newest upstream commit not imported yet), fork-only files, and
fork modifications (blob matches no upstream version) are all normal and
stay silent.

Exit codes: 0 = clean, 2 = drops found, 1 = operational error (bad refs,
git failure). Stdlib only; the one network touch (the Discord webhook POST)
lives in send_discord_alert(), called from main() only when the workflow
exports DISCORD_SYNC_ALERT — the test harness never does.
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
EXIT_DROPS = 2

# Same funnel as fork-sync-sequential.sh: the existing Discord webhook secret.
ALERT_WEBHOOK_ENV = "DISCORD_SYNC_ALERT"
# Set by the workflow from its dry_run dispatch input; skips the POST only.
DRY_RUN_ENV = "INTEGRITY_DRY_RUN"
RUN_URL_ENV = "INTEGRITY_RUN_URL"

ALERT_USER_AGENT = "DiscordBot (https://github.com/QuixThe2nd/hermes-ide, fork-sync-integrity)"
ALERT_CONTENT_CAP = 900

# How many (rev, path) pairs are resolved per `git cat-file --batch-check`
# process. Responses are one short line each, so 1000 keeps both pipes far
# below the OS buffer size while cutting process spawns by three orders of
# magnitude versus per-path rev-parse.
BLOB_BATCH_CHUNK = 1000


class IntegrityError(RuntimeError):
    """Operational failure (bad ref, git error) — maps to exit 1."""


def run_git(repo: str | None, *args: str) -> str:
    """Run one git command, returning stdout; any failure is operational."""
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise IntegrityError(f"git {' '.join(args[:3])} ... failed: {detail[:400]}")
    return proc.stdout


def resolve_commit(repo: str | None, ref: str) -> str:
    """Full commit sha for *ref*, or raise (bad ref → exit 1)."""
    out = run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = out.strip()
    if not sha:
        raise IntegrityError(f"cannot resolve ref: {ref}")
    return sha


# --- pure classification (unit-testable without git) -----------------------


def classify_path(path, fork_blob, walk, imported_set):
    """Classify one diverged path.

    ``walk`` is the upstream history of the path, NEWEST first, as a list of
    ``(sha, blob_or_None)`` — blob None means the path was absent in that
    commit's tree (a deletion). ``fork_blob`` None means the path is absent
    on the fork. ``imported_set`` is the set of upstream commits already
    reachable from fork main via merge second parents.

    Returns ``(classification, drop)`` where *drop* is a dict (dropped_commits
    holds bare shas; the caller enriches them with date/subject) for
    classification ``"drop"``, else None.
    """
    if not walk:
        # Upstream never touched the path: fork-only file (or renamed away on
        # the upstream side with no history under this name). Never a drop.
        return "fork_only", None

    newest_sha, newest_blob = walk[0]
    if newest_sha not in imported_set:
        # The newest upstream change to this path has not been imported yet.
        # Ordinary lag: the next sync runs will bring it. Alerting here would
        # cry wolf every hour between an upstream push and its import.
        return "lag", None
    if fork_blob == newest_blob:
        # Fork already carries the newest upstream blob for this path; the
        # census divergence must be rename-side or timing noise. Not a drop.
        return "current", None

    # Hard-evidence branch: the fork's blob is an OLDER imported version. The
    # newest imported change to the path should be visible on the fork, and it
    # is not. (fork_blob None matches upstream commits where the path was
    # absent, so a fork-deleted path whose content upstream deleted and then
    # re-added is caught here too: the re-add never landed.)
    for idx in range(1, len(walk)):
        sha, blob = walk[idx]
        if sha in imported_set and blob == fork_blob:
            dropped = [s for s, _ in walk[:idx] if s in imported_set]
            return (
                "drop",
                {
                    "path": path,
                    "status": "D" if fork_blob is None else "M",
                    "fork_blob": fork_blob,
                    "matched_upstream_blob": blob,
                    "dropped_commits": dropped,
                },
            )

    if fork_blob is None:
        # Deleted on the fork, and no upstream commit matches "absent" — the
        # general rule cannot anchor. Anchor instead on K, the newest imported
        # PREDECESSOR version of the path (walk[0] is excluded: it is the
        # change that failed to land). Imported commits newer than K touched
        # the path after the fork last knew a version of it, so their content
        # is demonstrably missing: drop.
        for idx in range(1, len(walk)):
            sha, blob = walk[idx]
            if sha in imported_set and blob is not None:
                dropped = [s for s, _ in walk[:idx] if s in imported_set]
                if dropped:
                    return (
                        "drop",
                        {
                            "path": path,
                            "status": "D",
                            "fork_blob": None,
                            "matched_upstream_blob": blob,
                            "dropped_commits": dropped,
                        },
                    )
                break
        return "fork_deleted", None

    # Fork blob matches no imported upstream version: a deliberate fork edit.
    return "fork_modified", None


# --- git plumbing (batched where it matters) -------------------------------


def diverged_paths(repo, fork_ref, upstream_ref):
    """[(path, status_letter)] from ``git diff fork upstream``.

    Rename/copy lines are split into their two paths so each side is judged
    independently, per the census contract.
    """
    out = run_git(
        repo,
        "diff",
        "--name-status",
        "--find-renames=50%",
        fork_ref,
        upstream_ref,
    )
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        letter = fields[0][0].upper()
        paths = fields[1:]
        if letter in ("R", "C") and len(paths) >= 2:
            # Source exists on the fork side but not upstream, dest the other
            # way round — classify each path on its own blobs.
            entries.append((paths[0], "D"))
            entries.append((paths[1], "A"))
        else:
            entries.append((paths[0], letter))
    return entries


def imported_commit_set(repo, fork_ref, upstream_ref):
    """Set of upstream commits already imported into fork ancestry.

    Import tips = second parents of the fork's merge commits that are
    ancestors of upstream (a fork-side merge of a fork branch must not
    count). The imported set is the union of those tips' ancestries; in the
    one-commit-at-a-time world it is prefix-closed along upstream history.
    """
    out = run_git(repo, "rev-list", "--merges", "--format=%P", fork_ref)
    candidate_parents = []
    current = None
    for line in out.splitlines():
        if line.startswith("commit "):
            current = line.split(" ", 1)[1].strip()
        elif current is not None and line.strip():
            parents = line.split()
            if len(parents) >= 2:
                candidate_parents.append(parents[1])
            current = None

    if not candidate_parents:
        return set()

    upstream_ancestry = set(
        run_git(repo, "rev-list", upstream_ref).split()
    )
    tips = [p for p in dict.fromkeys(candidate_parents) if p in upstream_ancestry]
    if not tips:
        return set()

    imported = set()
    # rev-list of many tips is the union of their ancestries; chunk the argv
    # so a fork with tens of thousands of imports stays under ARG_MAX.
    for start in range(0, len(tips), 500):
        chunk = tips[start : start + 500]
        imported.update(run_git(repo, "rev-list", *chunk).split())
    return imported


def path_walks(repo, upstream_ref, paths):
    """{path: [(sha, blob_placeholder)]} — upstream history per path, newest first.

    Blobs are resolved later in one batch; the walk carries shas only.
    """
    walks = {}
    for path in paths:
        out = run_git(repo, "rev-list", upstream_ref, "--", path)
        walks[path] = [(sha, None) for sha in out.split()]
    return walks


def resolve_blobs(repo, pairs):
    """{(rev, path): blob_sha or None} via chunked `git cat-file --batch-check`.

    One process per BLOB_BATCH_CHUNK pairs instead of one rev-parse per pair:
    a ~4k-path divergence needs thousands of lookups, and each saved process
    spawn is ~10ms off the 30-minute budget. Responses come in request order.
    """
    resolved = {}
    pairs = list(pairs)
    for start in range(0, len(pairs), BLOB_BATCH_CHUNK):
        chunk = pairs[start : start + BLOB_BATCH_CHUNK]
        request = "".join(f"{rev}:{path}\n" for rev, path in chunk)
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "cat-file", "--batch-check"],
            cwd=repo,
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            # "<oid> blob <size>" when present, "<rev>:<path> missing" when not.
            resolved[(rev, path)] = None if fields[-1] == "missing" else fields[0]
    return resolved


def enrich_commits(repo, shas):
    """{sha: {"sha", "date", "subject"}} for the dropped-commit evidence list."""
    if not shas:
        return {}
    out = run_git(
        repo, "show", "-s", "--format=%H%x1f%cI%x1f%s", *dict.fromkeys(shas)
    )
    info = {}
    for line in out.splitlines():
        if line.count("\x1f") == 2:
            sha, date, subject = line.split("\x1f")
            info[sha] = {"sha": sha, "date": date, "subject": subject}
    return info


def compute_integrity(repo, fork_ref, upstream_ref):
    """Build the full report dict (see module docstring for the algorithm)."""
    resolve_commit(repo, fork_ref)
    resolve_commit(repo, upstream_ref)

    census = diverged_paths(repo, fork_ref, upstream_ref)
    paths = [path for path, _ in census]
    report = {
        "fork_ref": fork_ref,
        "upstream_ref": upstream_ref,
        "generated_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds"),
        "drops": [],
        "totals": {
            "diverged_paths": len(paths),
            "drops": 0,
            "lag": 0,
            "fork_only": 0,
            "fork_modified": 0,
            "fork_deleted": 0,
        },
    }
    if not paths:
        return report

    imported_set = imported_commit_set(repo, fork_ref, upstream_ref)
    walks = path_walks(repo, upstream_ref, paths)

    # Lag paths need no blob evidence at all — usually the overwhelming
    # majority between an upstream push and its import.
    interesting = [
        path for path in paths if walks[path] and walks[path][0][0] in imported_set
    ]
    lookups = [(fork_ref, path) for path in interesting]
    for path in interesting:
        lookups.extend((sha, path) for sha, _ in walks[path])
    blobs = resolve_blobs(repo, lookups)

    dropped_shas = []
    for path in paths:
        walk = [
            (sha, blobs.get((sha, path))) for sha, _ in walks[path]
        ]
        fork_blob = blobs.get((fork_ref, path))
        classification, drop = classify_path(path, fork_blob, walk, imported_set)
        if classification in report["totals"]:
            report["totals"][classification] += 1
        if drop is not None:
            drop["dropped_commits"] = [
                {"sha": sha} for sha in drop["dropped_commits"]
            ]
            dropped_shas.extend(d["sha"] for d in drop["dropped_commits"])
            report["drops"].append(drop)

    # One `git show -s` for every dropped commit across all paths.
    details = enrich_commits(repo, dropped_shas)
    for drop in report["drops"]:
        enriched = []
        for entry in drop["dropped_commits"]:
            info = details.get(entry["sha"])
            if info:
                enriched.append(info)
        drop["dropped_commits"] = enriched
    report["totals"]["drops"] = len(report["drops"])
    return report


# --- alerting (the only network touch; never called by tests) --------------


def build_alert_content(report, max_report, run_url=""):
    """Single-line webhook content, same convention as fork-sync-sequential.sh.

    ``--max-report`` caps the paths listed inline so a mass regression cannot
    push the payload past Discord's 2000-char message limit; the full list
    always lives in the JSON artifact.
    """
    drops = report["drops"]
    listed = [d["path"] for d in drops[:max_report]]
    extra = len(drops) - len(listed)
    paths = ", ".join(listed)
    if extra > 0:
        paths = f"{paths} (+{extra} more)" if paths else f"+{extra} more"
    content = (
        f"fork-sync-integrity: {len(drops)} path(s) silently lost imported "
        f"upstream content | paths: {paths}"
        f" | fork: {report['fork_ref']}"
        f" | upstream: {report['upstream_ref']}"
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
            f"::warning::alert delivery failed ({exc}); "
            "the integrity failure above is the real problem",
            file=sys.stderr,
        )


# --- CLI --------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors, which would collide with the
    drops-found exit code; usage errors are operational errors here."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def main(argv=None):
    parser = _ArgumentParser(
        prog="fork-sync-integrity.py",
        description="Detect silently dropped already-imported upstream content.",
    )
    parser.add_argument("--fork-ref", required=True, help="fork ref to check (e.g. main)")
    parser.add_argument(
        "--upstream-ref",
        required=True,
        help="upstream ref to compare against (e.g. refs/remotes/upstream/main)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="write the full detail JSON here (always written, drops or not)",
    )
    parser.add_argument(
        "--max-report",
        type=int,
        default=10,
        help="max drop paths listed in the webhook content (default: 10)",
    )
    args = parser.parse_args(argv)

    try:
        report = compute_integrity(None, args.fork_ref, args.upstream_ref)
    except IntegrityError as exc:
        print(f"::error title=fork-sync-integrity::operational failure: {exc}",
              file=sys.stderr)
        return EXIT_ERROR

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")

    totals = report["totals"]
    if report["drops"]:
        for drop in report["drops"]:
            newest = drop["dropped_commits"][0] if drop["dropped_commits"] else {}
            matched = drop["matched_upstream_blob"] or "absent"
            print(
                f"DROP {drop['status']} {drop['path']}: fork carries "
                f"{matched[:12]} (imported) while "
                f"{newest.get('subject', '?')} was already imported too"
            )
        print(
            f"fork-sync-integrity: {totals['drops']} silently dropped path(s) "
            f"of {totals['diverged_paths']} diverged"
        )
        if os.environ.get(ALERT_WEBHOOK_ENV) and os.environ.get(DRY_RUN_ENV) != "true":
            send_discord_alert(
                os.environ[ALERT_WEBHOOK_ENV],
                build_alert_content(
                    report,
                    args.max_report,
                    os.environ.get(RUN_URL_ENV, ""),
                ),
            )
        return EXIT_DROPS

    print(
        f"fork-sync-integrity: clean — {totals['diverged_paths']} diverged "
        f"path(s), all lag/fork-side (lag {totals['lag']}, fork-only "
        f"{totals['fork_only']}, fork-modified {totals['fork_modified']}, "
        f"fork-deleted {totals['fork_deleted']})"
    )
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
