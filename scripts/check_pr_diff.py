#!/usr/bin/env python3
"""Gate a PR diff on regression tests and file-class scope.

Two named checks over the files a PR touches:

  REGRESSION-TEST — a PR that changes product (non-test) Python source must
    also change at least one test file. Escape hatches, reported when used:
    the PR carries the ``no-tests-needed`` label, or every changed file is a
    docs/config/chore path (.md/.txt/.toml, yaml under .github/, or files
    under skills/ and optional-skills/).

  SCOPE — lockfiles (package-lock.json, poetry.lock, uv.lock,
    pnpm-lock.yaml, yarn.lock) and CI workflow rewrites
    (.github/workflows/*.yml|*.yaml) must be the PR's primary purpose: a
    strict majority of changed files is that class, or the PR carries the
    matching label (``deps`` / ``ci``). Offenders are reported by path.

Inputs: ``check_pr_diff.py [base-ref [head-ref]]``. Defaults are the merge
base of ``origin/main`` and ``HEAD``. Changed paths come from
``git diff --name-only <base>...<head>`` (three-dot, merge-base semantics).
CI passes the PR's labels through the ``PR_LABELS`` env var (comma-separated;
whitespace-tolerated, case-insensitive).

Output is one human-readable line per check outcome followed by a final
``VERDICT PASS`` or ``VERDICT FAIL: <reasons>`` line. Exit 0 on PASS, 1 on
FAIL, 2 on usage/git errors. Pure stdlib.

Run: python scripts/check_pr_diff.py            # origin/main...HEAD
     python scripts/check_pr_diff.py main HEAD  # explicit refs
"""
from __future__ import annotations

import os
import subprocess
import sys

LOCKFILES = {
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "pnpm-lock.yaml",
    "yarn.lock",
}

DOCS_CONFIG_CHORE_SUFFIXES = (".md", ".txt", ".toml")
SKILL_ROOTS = ("skills", "optional-skills")


def _split(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def is_test(path: str) -> bool:
    parts = _split(path)
    base = parts[-1] if parts else path
    return (
        "tests" in parts[:-1]
        or base.startswith("test_") and base.endswith(".py")
        or base.endswith("_test.py")
    )


def is_product_python(path: str) -> bool:
    return path.endswith(".py") and not is_test(path)


def is_docs_config_chore(path: str) -> bool:
    parts = _split(path)
    base = parts[-1] if parts else path
    if base.endswith(DOCS_CONFIG_CHORE_SUFFIXES):
        return True
    if parts[0] == ".github" and base.endswith((".yml", ".yaml")):
        return True
    if parts[0] in SKILL_ROOTS:
        return True
    return False


def is_lockfile(path: str) -> bool:
    parts = _split(path)
    base = parts[-1] if parts else path
    return base in LOCKFILES


def is_ci_workflow(path: str) -> bool:
    parts = _split(path)
    return (
        len(parts) >= 3
        and parts[0] == ".github"
        and parts[1] == "workflows"
        and parts[-1].endswith((".yml", ".yaml"))
    )


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_ref(ref: str) -> str:
    result = _git(["rev-parse", "--verify", "--quiet", ref])
    if result.returncode != 0 or not result.stdout.strip():
        print(f"error: cannot resolve git ref {ref!r}", file=sys.stderr)
        raise SystemExit(2)
    return result.stdout.strip()


def changed_files(base: str, head: str) -> list[str]:
    result = _git(["diff", "--name-only", f"{base}...{head}"])
    if result.returncode != 0:
        print(
            f"error: git diff {base}...{head} failed:\n{result.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def default_base() -> str:
    result = _git(["merge-base", "origin/main", "HEAD"])
    if result.returncode != 0 or not result.stdout.strip():
        print(
            "error: no merge-base between origin/main and HEAD — "
            "pass refs explicitly: check_pr_diff.py <base> <head>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return result.stdout.strip()


def read_labels() -> set[str]:
    raw = os.environ.get("PR_LABELS", "")
    return {label.strip().lower() for label in raw.split(",") if label.strip()}


def main() -> int:
    args = sys.argv[1:]
    if len(args) > 2 or any(a in ("-h", "--help") for a in args):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    base = _resolve_ref(args[0]) if len(args) >= 1 else default_base()
    head = _resolve_ref(args[1]) if len(args) >= 2 else _resolve_ref("HEAD")

    files = changed_files(base, head)
    labels = read_labels()
    print(f"base: {base}")
    print(f"head: {head}")
    print(f"changed files: {len(files)}")
    if labels:
        print(f"labels: {', '.join(sorted(labels))}")

    failures: list[str] = []

    # ── REGRESSION-TEST gate ────────────────────────────────────────────
    product_py = [f for f in files if is_product_python(f)]
    test_files = [f for f in files if is_test(f)]
    non_test = [f for f in files if not is_test(f)]
    chore_only = bool(non_test) and all(is_docs_config_chore(f) for f in non_test)
    if not product_py:
        if chore_only:
            print(
                "REGRESSION-TEST: no product Python changed — "
                "admitted: all changes are docs/config/chore"
            )
        else:
            print("REGRESSION-TEST: no product Python changed — gate not triggered")
    elif test_files:
        print(
            f"REGRESSION-TEST: {len(test_files)} test file(s) alongside "
            f"{len(product_py)} product Python file(s) — admitted by tests present"
        )
    elif "no-tests-needed" in labels:
        print(
            "REGRESSION-TEST: product Python changed without tests — "
            "admitted by label no-tests-needed"
        )
    elif chore_only:
        print(
            "REGRESSION-TEST: Python changed only under skill trees — "
            "admitted: all changes are docs/config/chore"
        )
    else:
        print("REGRESSION-TEST: product Python changed without a test file:")
        for f in product_py:
            print(f"  {f}")
        failures.append("REGRESSION-TEST: product Python changed without a test file")

    # ── SCOPE gate ──────────────────────────────────────────────────────
    total = len(files)
    for cls_name, cls_pred, label in (
        ("lockfile", is_lockfile, "deps"),
        ("CI workflow", is_ci_workflow, "ci"),
    ):
        members = [f for f in files if cls_pred(f)]
        if not members:
            continue
        if total and len(members) * 2 > total:
            print(
                f"SCOPE: {len(members)}/{total} changed files are {cls_name}s — "
                "admitted as the PR's primary purpose (majority)"
            )
        elif label in labels:
            print(
                f"SCOPE: {cls_name} change admitted by label {label}"
            )
        else:
            print(f"SCOPE: unrelated {cls_name} file(s) in PR:")
            for f in members:
                print(f"  {f}")
            failures.append(f"SCOPE: unrelated file class in PR ({cls_name})")

    if failures:
        print(f"VERDICT FAIL: {'; '.join(failures)}")
        return 1
    print("VERDICT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
