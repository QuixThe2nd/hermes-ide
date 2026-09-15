#!/usr/bin/env python3
"""Apply, check, verify, or roll back the Honcho v3.0.10 Discord conclusion
notification patch against a self-hosted Honcho checkout.

Usage:
    python3 apply_patch.py --target /path/to/honcho check
    python3 apply_patch.py --target /path/to/honcho apply
    python3 apply_patch.py --target /path/to/honcho verify
    python3 apply_patch.py --target /path/to/honcho rollback

Safety properties:
- Validates the target checkout version and pristine file hashes from
  manifest.json before touching anything.
- `git apply` is all-or-nothing; a failed dry run leaves the tree untouched.
- Files not referenced by the patch (e.g. local-only edits) are never
  modified; their hashes are compared before and after as evidence.
- Never reads, prints, or copies webhook URL values — only which of the
  documented env var names are set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = SCRIPT_DIR / "manifest.json"

ROLLBACK_HINT = (
    "Rollback: python3 apply_patch.py --target <honcho-checkout> rollback "
    "(runs `git apply -R` with the same patch), then rebuild/restart the "
    "Honcho service (e.g. `docker compose up --build` or your usual build)."
)


class CheckFailure(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict:
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _target_version(target: Path) -> str | None:
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return None
    match = re.search(
        r'^version\s*=\s*"([^"]+)"',
        pyproject.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _git_apply(target: Path, patch: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "apply", *flags, str(patch)],
        cwd=target,
        capture_output=True,
        text=True,
    )


def check_patch_integrity(manifest: dict) -> Path:
    patch_path = SCRIPT_DIR / manifest["patch_file"]
    if not patch_path.is_file():
        raise CheckFailure(f"Patch file missing: {patch_path.name}")
    actual = _sha256(patch_path)
    if actual != manifest["patch_sha256"]:
        raise CheckFailure(
            f"Patch file hash mismatch for {patch_path.name}; "
            "refusing to apply a modified patch."
        )
    return patch_path


def check_target_version(manifest: dict, target: Path) -> None:
    expected = manifest["target_version"]
    actual = _target_version(target)
    if actual != expected:
        raise CheckFailure(
            f"Target checkout is version {actual!r}, expected {expected!r}. "
            "This patch is only valid against that Honcho release."
        )


def check_pristine_hashes(manifest: dict, target: Path) -> None:
    mismatched: list[str] = []
    missing: list[str] = []
    for rel_path, expected_hash in manifest["pristine_sha256"].items():
        file_path = target / rel_path
        if not file_path.is_file():
            missing.append(rel_path)
        elif _sha256(file_path) != expected_hash:
            mismatched.append(rel_path)
    problems = []
    if missing:
        problems.append("missing: " + ", ".join(sorted(missing)))
    if mismatched:
        problems.append("modified: " + ", ".join(sorted(mismatched)))
    if problems:
        raise CheckFailure(
            "Target files do not match the pristine v"
            f"{manifest['target_version']} hashes ({'; '.join(problems)}). "
            "Refusing to apply over local modifications to patched files."
        )


def snapshot_ignored_files(manifest: dict, target: Path) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for rel_path in manifest.get("ignored_local_files", []):
        file_path = target / rel_path
        snapshot[rel_path] = _sha256(file_path) if file_path.is_file() else None
    return snapshot


def patch_is_applied(manifest: dict, target: Path) -> bool:
    patch_path = SCRIPT_DIR / manifest["patch_file"]
    result = _git_apply(target, patch_path, "--check", "-R")
    return result.returncode == 0


def report_env_var_status(manifest: dict) -> None:
    print("Discord webhook env vars (names only, values never read or printed):")
    for name in manifest.get("discord_webhook_env_vars", []):
        state = "set" if os.environ.get(name, "").strip() else "unset"
        print(f"  {name}: {state}")


def cmd_check(manifest: dict, target: Path) -> int:
    check_patch_integrity(manifest)
    check_target_version(manifest, target)
    check_pristine_hashes(manifest, target)
    applied = patch_is_applied(manifest, target)
    print(f"Target {target} is Honcho v{manifest['target_version']} and pristine.")
    print(f"Patch currently applied: {'yes' if applied else 'no'}")
    report_env_var_status(manifest)
    return 0


def cmd_apply(manifest: dict, target: Path) -> int:
    patch_path = check_patch_integrity(manifest)
    check_target_version(manifest, target)

    if patch_is_applied(manifest, target):
        print("Patch is already applied; nothing to do.")
        return 0

    check_pristine_hashes(manifest, target)

    ignored_before = snapshot_ignored_files(manifest, target)

    dry_run = _git_apply(target, patch_path, "--check")
    if dry_run.returncode != 0:
        raise CheckFailure(
            "Dry run `git apply --check` failed; tree left untouched.\n"
            + dry_run.stderr.strip()
        )

    applied = _git_apply(target, patch_path)
    if applied.returncode != 0:
        raise CheckFailure(
            "`git apply` failed after a clean dry run.\n" + applied.stderr.strip()
        )

    ignored_after = snapshot_ignored_files(manifest, target)
    changed = [p for p in ignored_before if ignored_before[p] != ignored_after[p]]
    if changed:
        raise CheckFailure(
            "Unrelated local files changed during apply: " + ", ".join(changed)
        )

    if not patch_is_applied(manifest, target):
        raise CheckFailure(
            "Post-apply verification failed: `git apply --check -R` does not "
            "accept the tree."
        )

    print(f"Applied {patch_path.name} to {target}.")
    print("Verified: reverse dry run (`git apply --check -R`) accepts the tree.")
    if ignored_before:
        print(
            "Unrelated local files preserved: "
            + ", ".join(sorted(ignored_before))
        )
    report_env_var_status(manifest)
    print(ROLLBACK_HINT)
    return 0


def cmd_verify(manifest: dict, target: Path) -> int:
    check_patch_integrity(manifest)
    if not patch_is_applied(manifest, target):
        raise CheckFailure(
            "Patch is NOT cleanly applied: `git apply --check -R` rejected the "
            "tree."
        )
    print("Patch is applied cleanly (reverse dry run accepted the tree).")
    return 0


def cmd_rollback(manifest: dict, target: Path) -> int:
    patch_path = check_patch_integrity(manifest)
    if not patch_is_applied(manifest, target):
        raise CheckFailure(
            "Patch does not appear to be applied; reverse dry run failed. "
            "Nothing rolled back."
        )
    result = _git_apply(target, patch_path, "-R")
    if result.returncode != 0:
        raise CheckFailure(
            "`git apply -R` failed; tree may be partially reverted.\n"
            + result.stderr.strip()
        )
    check_target_version(manifest, target)
    check_pristine_hashes(manifest, target)
    print("Patch reverted; target files match pristine hashes again.")
    print("Rebuild and restart the Honcho service to complete the rollback.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        type=Path,
        help="Path to the self-hosted Honcho checkout.",
    )
    parser.add_argument(
        "command",
        choices=("check", "apply", "verify", "rollback"),
        help="check: validate only; apply: validate then patch; "
        "verify: confirm the patch is applied; rollback: reverse the patch.",
    )
    args = parser.parse_args(argv)

    target = args.target.resolve()
    if not target.is_dir():
        print(f"error: target is not a directory: {target}", file=sys.stderr)
        return 2

    try:
        manifest = load_manifest()
        handler = {
            "check": cmd_check,
            "apply": cmd_apply,
            "verify": cmd_verify,
            "rollback": cmd_rollback,
        }[args.command]
        return handler(manifest, target)
    except CheckFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
