#!/usr/bin/env python3
"""Divergence badges for hermes-ide vs upstream hermes-agent.

Compares this repo's `main` and upstream `NousResearch/hermes-agent` `main`
against their last shared commit (merge-base) and renders shields.io badges
plus plain-text lines into the README's `Divergence` section.

The upstream fetch is a READ-ONLY ancestry comparison — never a sync, merge,
or push target. `NousResearch/hermes-agent` is historical ancestry only.
"""

import argparse
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
UPSTREAM_REPO = "NousResearch/hermes-agent"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
README_MARKERS = ("<!-- divergence-badges:start -->", "<!-- divergence-badges:end -->")

CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".vue", ".rs", ".go", ".sh"}


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1] if parts else path
    if "tests" in parts or "test" in parts:
        return True
    return name.startswith("test_") or name.endswith("_test.py") or ".test." in name or ".spec." in name


def is_code_path(path: str) -> bool:
    return Path(path).suffix in CODE_EXTS and not is_test_path(path)


def round_half_up(value: float, digits: int) -> Decimal:
    quantum = Decimal(1).scaleb(-digits)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def format_pct(fraction: float) -> str:
    return str(round_half_up(100 * fraction, 0))


def format_ratio_line(removed: int, added: int, original: int, current: int) -> str:
    if removed >= added and added > 0:
        ratio = round_half_up(removed / added, 1)
        pct = format_pct(1 - current / original)
        return f"{ratio} deleted per 1 added, {pct}% smaller"
    if added > removed and removed > 0:
        ratio = round_half_up(added / removed, 1)
        pct = format_pct(current / original - 1)
        return f"{ratio} added per 1 deleted, {pct}% larger"
    # Degenerate side: no overlapping churn to ratio (one side added or removed nothing).
    if added == 0 and removed == 0:
        return "unchanged"
    if added == 0:
        return f"{format_pct(1 - current / original)}% smaller"
    return f"{format_pct(current / original - 1)}% larger"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _merge_base_from_github() -> str | None:
    """Last shared commit per the GitHub compare API ( NousResearch/hermes-agent
    main ... QuixThe2nd:hermes-ide main ). Read-only, no credentials needed."""
    import json
    import urllib.request

    url = f"https://api.github.com/repos/{UPSTREAM_REPO}/compare/main...QuixThe2nd:hermes-ide:main"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["merge_base_commit"]["sha"]
    except Exception:
        return None


def fetch_shared_history(repo: Path) -> tuple[str, str, str]:
    """Fetch both tips read-only (blobless, depth 1) and return (ours, upstream, merge-base)."""
    _git(repo, "fetch", "--filter=blob:none", "--depth=1", "origin", "main")
    ours = _git(repo, "rev-parse", "origin/main").strip()
    _git(repo, "fetch", "--filter=blob:none", "--depth=1", UPSTREAM_URL, "main")
    theirs = _git(repo, "rev-parse", "FETCH_HEAD").strip()
    base = _merge_base_from_github()
    if base is None:
        # Fall back to git itself: deepen history enough to find a common ancestor.
        _git(repo, "fetch", "--filter=blob:none", "--unshallow", UPSTREAM_URL, "main")
        base = _git(repo, "merge-base", ours, theirs).strip()
    else:
        _git(repo, "fetch", "--filter=blob:none", "--depth=1", UPSTREAM_URL, base)
    return ours, theirs, base


def tree_lines(repo: Path, tip: str) -> int:
    """Total code (non-test) lines in the tip tree, via git's own line counts."""
    out = _git(repo, "diff", "--numstat", EMPTY_TREE, tip)
    total = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or parts[0] == "-":
            continue
        if is_code_path(parts[2]):
            total += int(parts[0])
    return total


def churn(repo: Path, base: str, tip: str) -> tuple[int, int]:
    """(removed, added) code lines between base and tip. Deletions count
    against the old path, additions against the new path."""
    out = _git(repo, "-c", "diff.renameLimit=20000", "diff", "-M", "--numstat", base, tip)
    removed = added = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3:
            continue
        adds, dels, paths = parts
        if adds == "-" or dels == "-":
            continue  # binary
        if " => " in paths:
            old, new = paths.split(" => ", 1)
        else:
            old = new = paths
        if is_code_path(old):
            removed += int(dels)
        if is_code_path(new):
            added += int(adds)
    return removed, added


def compute_side(repo: Path, base: str, tip: str) -> dict:
    original = tree_lines(repo, base)
    current = tree_lines(repo, tip)
    removed, added = churn(repo, base, tip)
    return {
        "removed": removed,
        "added": added,
        "original": original,
        "current": current,
        "pct_gone": int(format_pct(removed / original)) if original else 0,
        "pct_new": int(format_pct(added / current)) if current else 0,
        "ratio_line": format_ratio_line(removed, added, original, current),
    }


def _badge(label: str, value: str, color: str) -> str:
    return f"![{label}](https://img.shields.io/badge/{label}-{value.replace('-', '--').replace(' ', '_')}-{color})"


def render_side(name: str, s: dict, color: str) -> str:
    return "\n".join(
        [
            f"**{name}**",
            "",
            " ".join(
                [
                    _badge("gone", f"{s['pct_gone']}%25", color),
                    _badge("new", f"{s['pct_new']}%25", color),
                    _badge("delta", s["ratio_line"].replace(", ", "~"), color),
                ]
            ),
            "",
            f"- {s['pct_gone']}% of the original code is gone",
            f"- {s['pct_new']}% of the code it has now is new",
            f"- {s['ratio_line']}",
        ]
    )


def render_block(ours: dict, theirs: dict) -> str:
    return "\n".join(
        [
            "<!-- divergence-badges:start -->",
            render_side("hermes-ide", ours, "blue"),
            "",
            render_side("upstream hermes", theirs, "orange"),
            "",
            "Code means py/ts/tsx/js/jsx/mjs/cjs/css/scss/vue/rs/go/sh, not tests, compared with the last commit both trees still share.",
            "<!-- divergence-badges:end -->",
        ]
    )


def replace_block(readme_text: str, block: str) -> str:
    start, end = README_MARKERS
    i = readme_text.index(start)
    j = readme_text.index(end) + len(end)
    return readme_text[:i] + block + readme_text[j:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repo checkout (default: cwd)")
    ap.add_argument("--readme", help="README path to update (default: no write)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    ours_tip, their_tip, base = fetch_shared_history(repo)
    ours = compute_side(repo, base, ours_tip)
    theirs = compute_side(repo, base, their_tip)
    block = render_block(ours, theirs)

    print(f"merge-base: {base}")
    for name, s in (("hermes-ide", ours), ("upstream hermes", theirs)):
        print(f"{name}: gone {s['pct_gone']}%, new {s['pct_new']}%, {s['ratio_line']}")
    print(block)

    if args.readme:
        path = Path(args.readme)
        old = path.read_text()
        if README_MARKERS[0] not in old:
            print(f"error: {README_MARKERS[0]} not found in {path}", file=sys.stderr)
            return 1
        path.write_text(replace_block(old, block))
        print(f"updated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
