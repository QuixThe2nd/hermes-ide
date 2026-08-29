"""code-lane-gate plugin — keep in-context source edits out of the main lane.

Wires one behaviour:

* ``pre_tool_call`` hook — refuses ``write_file`` / ``patch`` /
  ``execute_code`` calls that would edit *source code* — wherever the
  file lives, git repo or not — so coding work lands in the dedicated
  delegate lanes
  (``delegate_cursor_agent`` for small/medium tasks, ``delegate_claude_agent``
  with ``/goal`` for medium/large) instead of the in-context file tools.

On by default: the gate evaluates unless ``CODE_LANE_GATE_E2E`` carries
an explicit opt-out (``0``, ``false``, ``no``, ``off`` —
case-insensitive, whitespace-stripped). With an opt-out set the hook
returns ``None`` immediately and no scan happens; any other value
(unset, ``1``, ``true``, unrecognized garbage) leaves it enabled —
fail closed, because blocking is the gate's purpose.

What counts as a gated source edit:

* ``write_file`` — the ``path`` being written.
* ``patch`` mode ``replace`` — the ``path`` being edited.
* ``patch`` mode ``patch`` — every file path named by a V4A patch header:
  ``*** Update/Add/Delete File:`` (with or without a space after ``***``)
  and BOTH endpoints of ``*** Move File: src -> dst`` — the same header
  grammar ``tools/patch_parser.py`` accepts, so a form the real parser
  would honour can't slip past the gate.
* ``execute_code`` — best-effort regex heuristic over ``code`` for WRITE
  evidence: ``open(...)`` calls whose mode string contains ``w``/``a``/
  ``x`` or ``+`` (so ``"w"``, ``"wb+"``, ``"w+b"``, ``"r+"`` all write),
  ``write_text`` / ``write_bytes`` / ``.write_*`` methods,
  ``shutil.copy*`` / ``os.replace`` / ``os.rename``, or a shell redirect
  to a deny-suffixed file. Read-only snippets (``open(..., "r")``,
  ``open(..., "rb")``, ``Path(...).read_text()``, ``eval("1+1")``) pass.
  No path is available for this tool, so a heuristic match blocks
  without the suffix scoping below.

Each collected path is resolved BEFORE the checks, the same way the
tool layer resolves it: relative paths anchor to the TASK workspace
(live terminal cwd, registered cwd override, ``$TERMINAL_CWD``, else
process cwd — see ``tools.file_tools._resolve_path_for_task``), and
symlinks are resolved (``realpath``) so the gate judges the real file
the tools would touch, not the link spelling. The hook receives
``task_id`` from the dispatch layer and uses it for that resolution.

A collected path is blocked when its suffix (after the final dot,
lowercased) is a source-code suffix in the deny set (py/ts/tsx/js/jsx/
go/rs/java/rb/sh/bash/sql/c/cpp/h/hpp/cs/php/swift/kt/scala). Nothing
else is consulted — no location check, no git repository required —
the denial is suffix-only, everywhere.

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

# The gate is ON unless this env var carries an explicit opt-out. Set it
# to "0" (also "false"/"no"/"off", case-insensitive) to go back to
# pass-through with no scan at all; any other value — including the
# deployment host's "1" — keeps it enabled.
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

# execute_code exposes no path, so the suffix scoping can't apply —
# evidence of a WRITE in the snippet is the whole signal. Best-effort:
# match write-mode opens and write-shaped calls only, so read-only code
# (open(..., "r"), read_text(), eval("1+1")) passes while real writes
# (open(f, "w"), shutil.copyfile(src, "app.py")) are caught.
#
# open() modes are NOT enumerated as permutations in one character class
# — a class like [wa][+b]? silently misses real write modes Python
# accepts ("wb+", "w+b"). Instead the mode string is captured and judged
# by content: a quoted run of [rwaxbt+] (1-4 chars, every documented
# mode) after the comma of an open( call. The comma keeps read opens
# (no mode arg) and nearby dict literals ({"w": ...}) out of the match.
_OPEN_MODE_RE = re.compile(
    r"open\s*\(.{0,400}?,\s*(?:mode\s*=\s*)?[\"']([rwaxbt+]{1,4})[\"']",
    re.DOTALL,
)

# A captured open() mode is a WRITE when any of these chars appear in
# it: w/a/x create or truncate, and + upgrades any mode (even "r+") to
# read-write. Modes built only from r/b/t ("r", "rb", "rt") read.
_WRITE_MODE_CHARS = frozenset("wax+")

# Write-shaped calls that carry no mode string to parse.
_SOURCE_WRITE_RE = re.compile(
    r"(?:"
    r"write_text\s*\("
    r"|write_bytes\s*\("
    r"|\.write_\w+\s*\("
    r"|shutil\.copy\w*\s*\("
    r"|os\.replace\s*\("
    r"|os\.rename\s*\("
    r")",
)

# Shell redirection inside a command string (subprocess.run("... > app.py",
# shell=True), os.system) to a deny-suffixed file. Catches `>` and `>>`.
_SHELL_REDIRECT_RE = re.compile(
    r">\s*[\w./\\-]+\.(?:%s)\b" % "|".join(sorted(_DENY_SUFFIXES))
)

# V4A patch file headers, mirroring tools/patch_parser.py: `\s*` after
# `***` accepts the no-space `***Update File:` form, and Move carries two
# paths (`src -> dst`) — a move edits both places, so both are gated.
_V4A_FILE_HEADER_RE = re.compile(
    r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+?)\s*$"
)
_V4A_MOVE_HEADER_RE = re.compile(
    r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$"
)

# Steering note appended to every block reason.
_BLOCK_STEER = (
    "Source-code edits go through delegate_cursor_agent (small/medium) "
    "or delegate_claude_agent with /goal (medium/large). "
    "Set CODE_LANE_GATE_E2E=0 to disable."
)


def _gate_enabled() -> bool:
    """True unless the env var carries an explicit opt-out.

    Unset, ``1``, and any unrecognized value all enable the gate — it
    fails closed, because blocking is its purpose. Only ``0``/``false``/
    ``no``/``off`` (case-insensitive, stripped) disable it.
    """
    return os.environ.get(_ENV_GATE_ENABLED, "").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ---------------------------------------------------------------------------
# Path helpers (unit-testable without the env switch)
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """realpath + expanduser — symlink-resolved absolute form.

    ``abspath`` deliberately does NOT resolve symlinks, which let a path
    like ``/tmp/link/subdir/new.py`` (``link`` -> somewhere inside a repo)
    dodge the ``.git`` walk. ``realpath`` resolves the link first, so the
    walk climbs the repo's real ancestors.
    """
    return os.path.realpath(os.path.expanduser(path))


def _resolve_for_task(path: str, task_id: str = "") -> str:
    """Resolve *path* the way the write_file/patch tool layer would.

    The tool layer anchors relative paths to the TASK workspace (live
    terminal cwd, registered cwd override, ``$TERMINAL_CWD``, else process
    cwd) — see ``tools.file_tools._resolve_path_for_task``. Reusing that
    helper keeps the gate and the tools agreeing on where a relative
    ``app.py`` actually lands, instead of the gate guessing process cwd.
    Falls back to plain normalization when the helper can't be imported
    (plugin loaded outside the repo) or refuses the input.
    """
    try:
        from tools.file_tools import _resolve_path_for_task

        return str(_resolve_path_for_task(path, task_id or "default"))
    except Exception:
        return _normalize_path(path)


def _deny_suffix(path: str) -> str:
    """Suffix after the final dot, lowercased. Empty when there is none."""
    return Path(path).suffix.lstrip(".").lower()


def _clean_patch_path(raw: str) -> str:
    """Strip the whitespace/backticks the model wraps V4A header paths in."""
    return raw.strip().strip("`").strip()


def _paths_from_v4a_patch(patch_text: str) -> List[str]:
    """Extract every file path named by a V4A patch's file headers.

    Header grammar mirrors ``tools/patch_parser.py``: optional whitespace
    after ``***`` (the no-space ``***Update File:`` form parses too) and
    ``*** Move File: src -> dst`` yields BOTH endpoints. Bare paths,
    surrounding whitespace, and backtick-quoted paths are all handled.
    """
    paths: List[str] = []
    for line in patch_text.splitlines():
        stripped = line.strip()
        move = _V4A_MOVE_HEADER_RE.match(stripped)
        if move:
            for endpoint in move.groups():
                cleaned = _clean_patch_path(endpoint)
                if cleaned:
                    paths.append(cleaned)
            continue
        header = _V4A_FILE_HEADER_RE.match(stripped)
        if header:
            cleaned = _clean_patch_path(header.group(1))
            if cleaned:
                paths.append(cleaned)
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


def _first_denied_path(paths: List[str], task_id: str = "") -> Optional[str]:
    """First path whose resolved suffix is in the deny set.

    Location plays no part — a source-suffixed write is denied wherever
    it lands, so no repo/ancestor inspection happens here.
    """
    for path in paths:
        normalized = _resolve_for_task(path, task_id)
        if _deny_suffix(normalized) in _DENY_SUFFIXES:
            return normalized
    return None


def _execute_code_looks_like_source_write(args: Any) -> bool:
    """True only when the snippet shows evidence of a file WRITE.

    Best-effort, by design: execute_code exposes no path, so this is a
    regex over the code text. Write-mode opens, write_* method calls,
    copy/replace/rename, or a shell redirect to a deny-suffixed file all
    count; read-only constructs do not. Every open(..., "mode") in the
    snippet is judged — an early read open must not mask a later write.
    """
    if not isinstance(args, dict):
        return False
    code = args.get("code")
    if not isinstance(code, str):
        return False
    if _SOURCE_WRITE_RE.search(code) or _SHELL_REDIRECT_RE.search(code):
        return True
    return any(
        _WRITE_MODE_CHARS.intersection(match.group(1))
        for match in _OPEN_MODE_RE.finditer(code)
    )


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Block in-context source edits, git repo or not (on by default;
    opt out with ``CODE_LANE_GATE_E2E=0``).

    Returns ``None`` — tool proceeds untouched — unless the call would edit
    a source-suffixed file anywhere (or execute_code looks like a source
    write), in which case the delegate-lane steering message blocks it.
    ``task_id`` comes from the hook dispatch layer and drives the same
    task-workspace path resolution the file tools use.
    """
    if not _gate_enabled():
        return None

    denied = _first_denied_path(_gated_paths(tool_name, args), task_id)
    if denied is not None:
        return {
            "action": "block",
            "message": (
                f"code-lane-gate: {denied} is a source-file write. "
                f"{_BLOCK_STEER}"
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
