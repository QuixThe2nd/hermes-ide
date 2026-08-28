"""code-lane-gate plugin — keep in-context source edits out of the main lane.

Wires one behaviour:

* ``pre_tool_call`` hook — refuses ``write_file`` / ``patch`` /
  ``execute_code`` calls that would edit *source code* inside a git
  repository, so coding work lands in the dedicated delegate lanes
  (``delegate_cursor_agent`` for small/medium tasks, ``delegate_claude_agent``
  with ``/goal`` for medium/large) instead of the in-context file tools.

Off by default: the gate only evaluates when ``CODE_LANE_GATE_E2E=1``.
With the env unset (or ``0``) the hook returns ``None`` immediately and
no scan happens.

What counts as a gated source edit:

* ``write_file`` — the ``path`` being written.
* ``patch`` mode ``replace`` — the ``path`` being edited.
* ``patch`` mode ``patch`` — every ``*** Update File:`` / ``*** Add File:``
  / ``*** Delete File:`` header path in the V4A patch text.
* ``execute_code`` — best-effort regex heuristic over ``code`` for
  file-writing constructs (``open(``, ``write_text``, ``shutil``,
  ``subprocess``, ``eval(``, ...). No path is available for this tool,
  so a heuristic match blocks without the repo/suffix scoping below.

A collected path is blocked only when BOTH hold:

* it sits inside a git repository — the gate walks upward from the path
  looking for a ``.git`` directory; no ``git`` CLI is spawned — and
* its suffix (after the final dot, lowercased) is a source-code suffix
  in the deny set (py/ts/tsx/js/jsx/go/rs/java/rb/sh/bash/sql/c/cpp/
  h/hpp/cs/php/swift/kt/scala).

No suffix, or a docs/config suffix (md, yaml, json, ...), is allowed:
the gate steers *code* to the delegate lanes, it does not police prose.

Known bypass in v1: ``terminal`` is not inspected, so a shell heredoc
(``cat > app.py <<'EOF'``) still writes source through the terminal
lane. The delegate lanes remain the cheaper path for real edits, which
is what this gate nudges toward; closing the terminal hole would mean
parsing shell for redirect targets and is deferred.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The gate is dark until this env var is set. E2E rollout switch — flip to
# "0" (or unset) to go back to pass-through with no scan at all.
_ENV_GATE_ENABLED = "CODE_LANE_GATE_E2E"

# Source-code suffixes (after the final dot, lowercased) that route through
# the delegate lanes. Docs and config (md/txt/yaml/json/toml/...) are not
# here on purpose — the gate polices code, not prose.
_DENY_SUFFIXES = frozenset(
    {
        "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "rb",
        "sh", "bash", "sql", "c", "cpp", "h", "hpp", "cs", "php",
        "swift", "kt", "scala",
    }
)

# execute_code exposes no path, so the repo/suffix scoping can't apply —
# a regex hit on file-writing constructs is the whole signal. Best-effort.
_SOURCE_WRITE_RE = re.compile(
    r"(open|Path|write_text|shutil|os\.remove|os\.rename|subprocess|eval|exec)\s*\("
)

# V4A patch headers that carry a file path.
_PATCH_FILE_HEADERS = (
    "*** Update File:",
    "*** Add File:",
    "*** Delete File:",
)

# Steering note appended to every block reason.
_BLOCK_STEER = (
    "Source-code edits go through delegate_cursor_agent (small/medium) "
    "or delegate_claude_agent with /goal (medium/large). "
    "Set CODE_LANE_GATE_E2E=0 to disable."
)


def _gate_enabled() -> bool:
    return os.environ.get(_ENV_GATE_ENABLED, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------------------
# Path helpers (unit-testable without the env switch)
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """abspath + expanduser, without resolving symlinks."""
    return os.path.abspath(os.path.expanduser(path))


def _deny_suffix(path: str) -> str:
    """Suffix after the final dot, lowercased. Empty when there is none."""
    return Path(path).suffix.lstrip(".").lower()


def _is_inside_git_repo(path: str) -> bool:
    """True when some ancestor of ``path`` holds a ``.git`` directory.

    Pure filesystem walk — no ``git`` CLI, so it stays cheap and works on
    checkouts the git binary can't see. A not-yet-existing file path is
    fine: the walk still climbs its (real) ancestor directories.
    """
    current = Path(path)
    if (current / ".git").is_dir():
        return True
    for parent in current.parents:
        if (parent / ".git").is_dir():
            return True
    return False


def _paths_from_v4a_patch(patch_text: str) -> List[str]:
    """Extract every file path named by a V4A patch's file headers.

    Handles the trailing-path forms the model produces: bare paths,
    surrounding whitespace, and backtick-quoted paths. Lines that merely
    mention a header inside patch body context still parse — anything
    starting with a known header is treated as a file section.
    """
    paths: List[str] = []
    for line in patch_text.splitlines():
        stripped = line.strip()
        for header in _PATCH_FILE_HEADERS:
            if stripped.startswith(header):
                target = stripped[len(header):].strip().strip("`").strip()
                if target:
                    paths.append(target)
                break
    return paths


# ---------------------------------------------------------------------------
# Tool-call evaluation
# ---------------------------------------------------------------------------


def _gated_paths(tool_name: str, args: Any) -> List[str]:
    """Paths whose write this gate must inspect. Empty for other tools."""
    if not isinstance(args, dict):
        return []
    if tool_name == "write_file":
        path = args.get("path")
        return [path] if isinstance(path, str) and path else []
    if tool_name == "patch":
        if (args.get("mode") or "replace") == "patch":
            patch_text = args.get("patch")
            if not isinstance(patch_text, str):
                return []
            return _paths_from_v4a_patch(patch_text)
        path = args.get("path")
        return [path] if isinstance(path, str) and path else []
    return []


def _first_denied_path(paths: List[str]) -> Optional[str]:
    """First path that is both inside a git repo and source-suffixed."""
    for path in paths:
        normalized = _normalize_path(path)
        if _deny_suffix(normalized) in _DENY_SUFFIXES and _is_inside_git_repo(
            normalized
        ):
            return normalized
    return None


def _execute_code_looks_like_source_write(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    code = args.get("code")
    return isinstance(code, str) and bool(_SOURCE_WRITE_RE.search(code))


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Block in-context source edits inside git repos (E2E-gated).

    Returns ``None`` — tool proceeds untouched — unless the call would edit
    a source file in a repo (or execute_code looks like a source write),
    in which case the delegate-lane steering message blocks it.
    """
    if not _gate_enabled():
        return None

    denied = _first_denied_path(_gated_paths(tool_name, args))
    if denied is not None:
        return {
            "action": "block",
            "message": (
                f"code-lane-gate: {denied} is source code inside a git "
                f"repository. {_BLOCK_STEER}"
            ),
        }

    if tool_name == "execute_code" and _execute_code_looks_like_source_write(
        args
    ):
        return {
            "action": "block",
            "message": (
                "code-lane-gate: this execute_code snippet looks like a "
                f"source-file write. {_BLOCK_STEER}"
            ),
        }

    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
