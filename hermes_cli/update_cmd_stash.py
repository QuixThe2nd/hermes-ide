"""Autostash handling for ``hermes update``: stash before the pull, restore/park/discard afterwards, warn about orphans.

Split out of ``update_cmd.py``; names are re-imported there so ``hermes_cli.update_cmd.<name>`` still resolves/monkeypatches.
Origin helpers are imported lazily per function (no cycle; test patches on the origin stay effective).
"""

import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Log-record parity with the origin module.
logger = logging.getLogger("hermes_cli.update_cmd")

#: Autostash subject contract: this prefix + UTC YYYYMMDD-HHMMSS stamp
#: (producer _stash_local_changes_if_needed, consumer _warn_orphaned_update_autostashes).
_AUTOSTASH_NAME_PREFIX = "hermes-update-autostash-"

#: Age past which a leftover autostash is called out. Younger entries are normal
#: (recent --keep-stash park); older ones are almost always forgotten.
# Entries younger than this are normal (a parked stash from : the desktop updater's --keep-stash run minutes
# ago); older ones are almost : always forgotten (#63717 problem 6: an orphan persisted 9+ days unnoticed).
_AUTOSTASH_WARN_AGE_DAYS = 7

_STASH_LEFT_IN_PLACE = "  The stash was left in place. You can remove it manually after checking the result."


def _git_quiet(git_cmd: list[str], args: list[str], cwd: Path, **kwargs):
    """``subprocess.run`` of a git command with captured output; None when git cannot run."""
    try:
        return subprocess.run(git_cmd + args, cwd=cwd, capture_output=True, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_paths_z(git_cmd: list[str], args: list[str], cwd: Path):
    """NUL-separated path listing as a set, or None when git failed (surrogateescape keeps odd filenames)."""
    result = _git_quiet(git_cmd, args, cwd, text=True, encoding="utf-8", errors="surrogateescape")
    if result is None or result.returncode != 0:
        return None
    return {path for path in result.stdout.split("\0") if path}


def _reset_hard(git_cmd: list[str], cwd: Path) -> None:
    subprocess.run(git_cmd + ["reset", "--hard", "HEAD"], cwd=cwd, capture_output=True)


def _print_nonempty(text: str, prefix: str = "") -> None:
    if text.strip():
        print(f"{prefix}{text.strip()}")


def _print_first_line(text: str) -> None:
    if text.strip():
        print(f"  {text.strip().splitlines()[0]}")


def _stash_local_changes_if_needed(git_cmd: list[str], cwd: Path) -> Optional[str]:
    from hermes_cli.update_cmd_git import _git_run
    status = _git_run(git_cmd, ["status", "--porcelain"], cwd, check=True)
    if not status.stdout.strip():
        return None
    # Unmerged index entries (interrupted merge/rebase) make `git stash` fail with
    # "needs merge"; `git reset` drops only the index conflict state, not the tree.
    if _git_run(git_cmd, ["ls-files", "--unmerged"], cwd).stdout.strip():
        print("→ Clearing unmerged index entries from a previous conflict...")
        subprocess.run(git_cmd + ["reset"], cwd=cwd, capture_output=True)

    stash_name = datetime.now(timezone.utc).strftime(f"{_AUTOSTASH_NAME_PREFIX}%Y%m%d-%H%M%S")
    print("→ Local changes detected — stashing before update...")
    prev_stash = _git_run(git_cmd, ["rev-parse", "--verify", "refs/stash"], cwd).stdout.strip()
    push = _git_run(git_cmd, ["stash", "push", "--include-untracked", "-m", stash_name], cwd)
    _print_nonempty(push.stdout)
    stash_probe = _git_run(git_cmd, ["rev-parse", "--verify", "refs/stash"], cwd)
    stash_ref = stash_probe.stdout.strip()
    stash_created = stash_probe.returncode == 0 and bool(stash_ref) and stash_ref != prev_stash
    if push.returncode != 0:
        if not stash_created:
            # No entry created: changes NOT saved — bail before touching HEAD.
            print("✗ Could not stash local changes — update aborted.")
            _print_first_line(push.stderr)
            print("  Commit, stash, or clean up your local changes manually, then re-run `hermes update`.")
            raise subprocess.CalledProcessError(push.returncode, push.args, output=push.stdout, stderr=push.stderr)
        # Non-zero but entry created: push saved everything yet couldn't delete some untracked files
        # (e.g. root-owned dir). Not a failure — continue.
        _print_nonempty(push.stderr)
        print("  ⚠ Some untracked files could not be removed from the working tree (permission denied).")
        print("    They were still saved to the stash and were left in place — the update will continue.")
        # A partially-failed push also skips cleanup of TRACKED modifications; they'd break the following
        # pull. Safe to reset: all is in the stash.
        _reset_hard(git_cmd, cwd)
    return stash_ref


def _resolve_stash_selector(git_cmd: list[str], cwd: Path, stash_ref: str) -> Optional[str]:
    from hermes_cli.update_cmd_git import _git_run
    stash_list = _git_run(git_cmd, ["stash", "list", "--format=%gd %H"], cwd, check=True)
    for line in stash_list.stdout.splitlines():
        selector, _, commit = line.partition(" ")
        if commit.strip() == stash_ref:
            return selector.strip()
    return None


def _warn_orphaned_update_autostashes(git_cmd: list[str], cwd: Path) -> int:
    """Print a notice for update autostashes older than the warn threshold; return the count (0 on any git failure).

    Autostashes legitimately outlive a run (--keep-stash, failed restore) but nothing re-surfaces them.
    Deliberately NOT a GC: a stash may be the only copy of the user's work, so Hermes never drops one.

    Autostash entries legitimately outlive an update run (``--keep-stash`` parks them; a conflicted or
    failed restore preserves them for safety), but nothing ever re-surfaces them afterwards — they sit in
    ``git stash`` invisibly for weeks (#63717 problem 6). This prints a short notice naming the stale
    entries with recovery/cleanup guidance.
    """
    from hermes_cli.update_cmd_git import _git_run
    try:
        stash_list = _git_run(git_cmd, ["stash", "list", "--format=%gd %s"], cwd)
        if stash_list.returncode != 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=_AUTOSTASH_WARN_AGE_DAYS)
        stale: list[tuple[str, str]] = []
        for line in stash_list.stdout.splitlines():
            selector, _, subject = line.strip().partition(" ")
            pos = subject.find(_AUTOSTASH_NAME_PREFIX)
            if pos < 0:
                continue
            stamp = subject[pos + len(_AUTOSTASH_NAME_PREFIX):][:15]  # "YYYYMMDD-HHMMSS"
            try:
                stash_time = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # age unknown — leave it alone rather than guess
            if stash_time < cutoff:
                stale.append((selector, stamp))
        if not stale:
            return 0
        print()
        print(
            f"⚠ {len(stale)} leftover update autostash entr"
            f"{'y is' if len(stale) == 1 else 'ies are'} more than "
            f"{_AUTOSTASH_WARN_AGE_DAYS} days old:"
        )
        for selector, stamp in stale:
            print(f"    {selector}  ({_AUTOSTASH_NAME_PREFIX}{stamp})")
        print("  These hold local changes stashed by earlier updates and never")
        print("  restored. Review with: git stash show -p <entry>")
        print("  Restore with: git stash apply <entry>   Discard with: git stash drop <entry>")
        return len(stale)
    except Exception as exc:
        logger.debug("Autostash age check failed: %s", exc)
        return 0


def _print_stash_cleanup_guidance(stash_ref: str, stash_selector: Optional[str] = None) -> None:
    print("  Check `git status` first so you don't accidentally reapply the same change twice.")
    print("  Find the saved entry with: git stash list --format='%gd %H %s'")
    if stash_selector:
        print(f"  Remove it with: git stash drop {stash_selector}")
    else:
        print(f"  Look for commit {stash_ref}, then drop its selector with: git stash drop stash@{{N}}")


def _stash_apply_failed_only_on_existing_untracked(stderr: str) -> bool:
    """True when a ``git stash apply`` failure is ONLY about untracked files that already exist in the tree.

    Tail of the permission-denied class: push swept undeletable files into the stash but couldn't remove them;
    apply restores tracked changes, then refuses to overwrite those files and exits non-zero though nothing was lost.
    Any other error line (e.g. ``would be overwritten by merge``) means the tracked apply failed -> False.
    """
    saw_untracked_error = False
    for ln in (ln.strip() for ln in (stderr or "").splitlines() if ln.strip()):
        if "already exists, no checkout" in ln or "could not restore untracked files from stash" in ln:
            saw_untracked_error = True
        elif not ln.startswith(("warning:", "hint:")):
            return False
    return saw_untracked_error


def _park_stashed_changes(stash_ref: str) -> None:
    """Leave a pre-update autostash parked (``--keep-stash``, the desktop updater's mode): local source
    edits must never be silently re-applied onto updated code; the entry stays in ``git stash``."""
    print()
    print("ℹ️  Local changes were stashed before updating and were NOT re-applied (--keep-stash).")
    print(f"  Stash ref: {stash_ref}")
    print(f"  Restore manually with: git stash apply {stash_ref}")


def _git_untracked_paths(git_cmd: list[str], cwd: Path) -> set[str] | None:
    """Return untracked paths, or ``None`` when Git cannot enumerate them."""
    paths = _git_paths_z(git_cmd, ["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    if paths is None:
        print("  ⚠ Could not enumerate untracked files while validating the restored stash.")
    return paths


def _restored_python_paths(git_cmd: list[str], cwd: Path) -> tuple[str, ...] | None:
    """Restored ``.py`` paths changed from ``HEAD``; deliberately Python-only (entry scripts stay outside the health check)."""
    from hermes_cli.update_cmd import _git_untracked_paths
    paths = _git_paths_z(git_cmd, ["diff", "--name-only", "-z", "HEAD", "--", "*.py"], cwd)
    if paths is None:
        print("  ⚠ Could not enumerate tracked Python files restored from the stash.")
        return None
    untracked = _git_untracked_paths(git_cmd, cwd)
    if untracked is None:
        return None
    paths.update(path for path in untracked if path.endswith(".py"))
    return tuple(sorted(paths))


def _reject_unsafe_stash_restore(
    git_cmd: list[str], cwd: Path, stash_ref: str, preexisting_untracked: set[str], failing_target: str,
    detail: str | None,
) -> None:
    """Restore the clean updated tree, preserve the stash, and abort the update."""
    from hermes_cli.update_cmd import _git_untracked_paths
    print()
    print("✗ Restored local changes made the Hermes agent unexecutable.")
    print(f"  Health check failed: {failing_target}")
    if detail:
        for line in str(detail).splitlines()[:6]:
            print(f"    {line}")

    def _ok(result) -> bool:
        return result is not None and result.returncode == 0

    current_untracked = _git_untracked_paths(git_cmd, cwd)
    restored_untracked = current_untracked - preexisting_untracked if current_untracked is not None else set()
    reset = _git_quiet(git_cmd, ["reset", "--hard", "HEAD"], cwd)
    clean = _git_quiet(git_cmd, ["clean", "-fd", "--", *sorted(restored_untracked)], cwd) if restored_untracked else None
    cleanup_ok = current_untracked is not None and _ok(reset) and (not restored_untracked or _ok(clean))
    if cleanup_ok:
        cleanup_ok = _ok(_git_quiet(git_cmd, ["diff", "--quiet", "HEAD", "--"], cwd))
    if cleanup_ok:
        print("  The clean updated tree has been restored; the gateway was not restarted.")
    else:
        print("  ⚠ The clean updated tree could not be fully restored automatically.")
        print("    Inspect `git status` and run `git reset --hard HEAD` before retrying.")
    print("  Platform connectivity alone does not mean the agent can execute turns.")
    print(f"  Your local changes remain preserved in stash: {stash_ref}")
    print(f"  Inspect them with: git stash show --stat {stash_ref}")
    print(f"  Restore manually after fixing them: git stash apply {stash_ref}")
    raise SystemExit(1)


def _confirm_restore(stash_ref: str, input_fn) -> bool:
    """Interactive gate; a remote ``input_fn`` defaults to No (``[y/N]``), the local prompt to Yes."""
    remote_prompt = input_fn is not None
    prompt_suffix = "[y/N]" if remote_prompt else "[Y/n]"
    print()
    print("⚠ Local changes were stashed before updating.")
    print("  Restoring them may reapply local customizations onto the updated codebase.")
    print("  Review the result afterward if Hermes behaves unexpectedly.")
    print(f"Restore local changes now? {prompt_suffix}")
    if remote_prompt:
        response = input_fn(f"Restore local changes now? {prompt_suffix}", "n")
    else:
        try:
            response = input().strip().lower()
        except (EOFError, UnicodeDecodeError):
            response = "n"  # closed stdin/encoding error must not crash mid-restore
    if response in {"y", "yes"} or (not remote_prompt and response == ""):
        return True
    print("Skipped restoring local changes.")
    print("Your changes are still preserved in git stash.")
    print(f"Restore manually with: git stash apply {stash_ref}")
    return False


# ---------------------------------------------------------------------------
# Conflict auto-resolution. Go twin: internal/stashconflict
# (Prompt / Classify / DriftNote) - keep the wording and verdict order in sync.
# ---------------------------------------------------------------------------

_CONFLICT_RESOLVE_TIMEOUT = 600


def _conflict_resolution_prompt(unmerged_paths: list[str], stash_ref: str) -> str:
    paths = "\n".join(f"- {p}" for p in unmerged_paths)
    return (
        "Resolve a git stash conflict in this checkout.\n\n"
        f"`hermes update` stashed local edits, pulled upstream, then re-applied {stash_ref} and hit\n"
        "conflicts. Leave the stash in place.\n\n"
        f"Conflicted files:\n{paths}\n\n"
        "Keep the newly pulled upstream behaviour and re-apply the local intent in the smallest form\n"
        "that still works: no conflict markers left, no unrelated rewrites, no dropped features.\n"
        "Touch only the files above. Do not commit, do not push, do not drop the stash.\n"
        "Finish when `git diff --name-only --diff-filter=U` prints nothing.\n"
    )


def _classify_conflict_outcome(exit_code: int, unmerged_paths: list[str]) -> str:
    if unmerged_paths:  # a tree that did not merge is never resolved, whatever the exit code says
        return "still-conflicted"
    return "resolved" if exit_code == 0 else "failed"


def _conflict_drift_note(resolved_paths: list[str]) -> str:
    if not resolved_paths:
        return ""
    listed = "\n".join(f"  \u2022 {p}" for p in resolved_paths)
    return (
        "These were local-only changes to core files. Upstream keeps editing them, so they will\n"
        f"conflict again: open a PR for what belongs in Hermes, or make it a plugin.\n{listed}"
    )


def _conflict_auto_resolve_enabled() -> bool:
    from hermes_cli.update_cmd import _updates_config
    try:
        return bool(_updates_config().get("auto_resolve_conflicts", True))
    except Exception:  # a broken config read must never decide the updater's fate
        return False


def _auto_resolve_stash_conflict(
    git_cmd: list[str], cwd: Path, stash_ref: str, unmerged_paths: list[str],
) -> bool:
    """Hand a conflicted stash re-apply to a fresh one-shot session.

    False keeps the caller's reset-and-park path, so a failed resolution costs nothing
    that the old behaviour did not already cost.
    """
    from hermes_cli.update_cmd_git import _git_run
    if not _conflict_auto_resolve_enabled():
        print("  Conflict auto-resolve is off (updates.auto_resolve_conflicts: false).")
        return False
    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        print("  Conflict auto-resolve skipped: no `hermes` executable on PATH to open a session.")
        return False
    print("\n\u2192 Handing the conflict to a fresh agent session to resolve...")
    try:
        run = subprocess.run(
            [hermes_bin, "-z", _conflict_resolution_prompt(unmerged_paths, stash_ref)],
            cwd=cwd, capture_output=True, text=True, timeout=_CONFLICT_RESOLVE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Conflict auto-resolve failed: {exc}")
        return False
    remaining = [
        p for p in _git_run(git_cmd, ["diff", "--name-only", "--diff-filter=U"], cwd).stdout.split() if p
    ]
    outcome = _classify_conflict_outcome(run.returncode, remaining)
    if outcome != "resolved":
        print(f"  Conflict auto-resolve did not finish ({outcome}).")
        if remaining:
            print("  Still conflicted: " + ", ".join(remaining))
        _print_nonempty(run.stderr)
        return False
    print("\u2192 Conflict resolved by a fresh session; validating the restored tree...")
    note = _conflict_drift_note(unmerged_paths)
    if note:
        print(note)
    return True


def _apply_stash(git_cmd: list[str], cwd: Path, stash_ref: str) -> bool:
    """``git stash apply``; False (tree reset, stash kept) on conflicts or any failure other than the
    undeletable-untracked class."""
    from hermes_cli.update_cmd_git import _git_run
    print("→ Restoring local changes...")
    restore = _git_run(git_cmd, ["stash", "apply", stash_ref], cwd)
    unmerged = _git_run(git_cmd, ["diff", "--name-only", "--diff-filter=U"], cwd)  # conflicts can exist even on rc 0
    conflicted_files = unmerged.stdout.strip()
    if restore.returncode == 0 and not conflicted_files:
        return True
    if not conflicted_files and _stash_apply_failed_only_on_existing_untracked(restore.stderr):
        # Tracked changes applied; only undeletable-at-stash-time untracked files were refused. Their
        # content is untouched — treat as restored.
        print("  ⚠ Some stashed untracked files already exist in the working tree and were kept as-is.")
        return True
    print("✗ Update pulled new code, but restoring local changes hit conflicts.")
    _print_nonempty(restore.stdout)
    _print_nonempty(restore.stderr)
    if conflicted_files:
        print("\nConflicted files:")
        for f in conflicted_files.splitlines():
            print(f"  • {f}")
    if conflicted_files and _auto_resolve_stash_conflict(
        git_cmd, cwd, stash_ref, conflicted_files.splitlines()
    ):
        return True
    print("\nYour stashed changes are preserved — nothing is lost.")
    print(f"  Stash ref: {stash_ref}")
    _reset_hard(git_cmd, cwd)  # conflict markers make hermes unrunnable; changes stay in the stash
    print("Working tree reset to clean state.")
    print(f"Restore your changes later with: git stash apply {stash_ref}")
    return False  # code update succeeded; cmd_update continues (deps, skills, gateway)


def _drop_restored_stash(git_cmd: list[str], cwd: Path, stash_ref: str) -> None:
    from hermes_cli.update_cmd_git import _git_run
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print("⚠ Local changes were restored, but Hermes couldn't find the stash entry to drop.")
        print(_STASH_LEFT_IN_PLACE)
        _print_stash_cleanup_guidance(stash_ref)
        return
    drop = _git_run(git_cmd, ["stash", "drop", stash_selector], cwd)
    if drop.returncode != 0:
        print("⚠ Local changes were restored, but Hermes couldn't drop the saved stash entry.")
        _print_nonempty(drop.stdout)
        _print_nonempty(drop.stderr)
        print(_STASH_LEFT_IN_PLACE)
        _print_stash_cleanup_guidance(stash_ref, stash_selector)


def _restore_stashed_changes(
    git_cmd: list[str], cwd: Path, stash_ref: str, prompt_user: bool = False, input_fn=None,
) -> bool:
    from hermes_cli.update_cmd import _critical_module_import_failures, _git_untracked_paths, _restored_python_paths, _validate_python_files_syntax
    if prompt_user and not _confirm_restore(stash_ref, input_fn):
        return False
    preexisting_untracked = _git_untracked_paths(git_cmd, cwd)
    if preexisting_untracked is None:
        print("  The stash was not restored because its cleanup baseline is unknown.")
        print(f"  Restore manually with: git stash apply {stash_ref}")
        return False
    clean_import_failures = _critical_module_import_failures(cwd, report_runtime_errors=True)
    if not _apply_stash(git_cmd, cwd, stash_ref):
        return False

    def reject(failing_target: str, detail) -> None:
        _reject_unsafe_stash_restore(git_cmd, cwd, stash_ref, preexisting_untracked, failing_target, detail)

    restored_python = _restored_python_paths(git_cmd, cwd)
    if restored_python is None:
        reject("restored Python source discovery", "could not determine which restored Python files require validation")
    syntax_ok, failing_path, syntax_error = _validate_python_files_syntax(cwd, restored_python)
    if not syntax_ok:
        reject(failing_path or "restored Python source", syntax_error)
    for module, error in _critical_module_import_failures(cwd, report_runtime_errors=True).items():
        if clean_import_failures.get(module) != error:
            reject(f"agent import {module or 'unknown'}", error[1])
            break
    _drop_restored_stash(git_cmd, cwd, stash_ref)
    print("⚠ Local changes were restored on top of the updated codebase.")
    print("  Review `git diff` / `git status` if Hermes behaves unexpectedly.")
    return True


def _discard_stashed_changes(git_cmd: list[str], cwd: Path, stash_ref: str) -> bool:
    """Drop a pre-update stash without applying (non-interactive ``updates.non_interactive_local_changes: discard``).

    Unlike reset --hard + clean -fd this touches only what was stashed; ignored paths are never affected.
    Returns True if dropped, False on git failure (stash left in place).
    """
    from hermes_cli.update_cmd_git import _git_run
    stash_selector = _resolve_stash_selector(git_cmd, cwd, stash_ref)
    if stash_selector is None:
        print(
            "⚠ Configured to discard local changes on non-interactive update, "
            "but Hermes couldn't find the stash entry to drop."
        )
        _print_stash_cleanup_guidance(stash_ref)
        return False
    drop = _git_run(git_cmd, ["stash", "drop", stash_selector], cwd)
    if drop.returncode != 0:
        print("⚠ Configured to discard local changes, but Hermes couldn't drop the saved stash entry.")
        _print_first_line(drop.stderr)
        _print_stash_cleanup_guidance(stash_ref, stash_selector)
        return False
    print("→ Discarded local source changes (updates.non_interactive_local_changes=discard).")
    return True
