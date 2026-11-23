#!/usr/bin/env python3
"""omp-viewer — live web UI for Hermes OMP delegate-lane session transcripts.

Serves ``<workdir>/.hermes-lane/<lane>/*.jsonl`` files discovered through
the per-home lane-run registry (``<HERMES_HOME>/omp-lane-runs/``, written
by the lane launcher as each run starts) plus any configured scan roots.
The transcripts are OMP session
JSONL: ``session`` / ``model_change`` / ``message`` (roles user, assistant,
toolResult; content parts text, thinking, toolCall) / ``custom`` (lane
bookkeeping such as ``date-cwd-reminder`` and ``tool_execution_start``) /
``custom_message`` / ``compaction`` events, plus wire events like
``turn_end`` and ``error`` that OMP emits on its stream.

Unauthenticated by design: binds loopback unless the operator passes a LAN
bind. Only paths shaped like a lane transcript and resolve-contained inside
a known workdir (registry entry or under a configured scan root) are ever
served.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# One response may carry a session's whole visible transcript; larger
# transcripts trim (and page) past this.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(SCRIPT_DIR, "ui.html")
FONT_PATH = os.path.join(SCRIPT_DIR, "DejaVuSansMono.ttf")
INTER_REGULAR_PATH = os.path.join(SCRIPT_DIR, "Inter-Regular.ttf")
INTER_MEDIUM_PATH = os.path.join(SCRIPT_DIR, "Inter-Medium.ttf")
LOG_LOCK = threading.Lock()

# Scan policy: workdirs carrying a .hermes-lane directory are discovered at
# most this many levels below each scan root. Anything deeper is out of
# scope for the viewer (and keeps the walk cheap on big trees).
SCAN_MAX_DEPTH = 3
SKIP_DIR_NAMES = frozenset(
    {"node_modules", ".git", ".venv", "venv", "__pycache__", ".hermes-lane"}
)
LANE_DIR_NAME = ".hermes-lane"
# Run registry written by the lane launcher (plugins/omp_viewer/runs.py);
# this stdlib-only server re-reads it directly. One entry is a handful of
# short paths — anything past this bound is refused without parsing.
RUNS_DIR_NAME = "omp-lane-runs"
MAX_RUN_ENTRY_BYTES = 4096
# A run whose transcript changed within this window renders as active.
ACTIVE_WINDOW_SEC = 15.0

SCAN_ROOTS: list[str] = []
LOG_FILE: str = ""
SERVER_BIND = "127.0.0.1"
SERVER_PORT = 8790


def default_log_file() -> str:
    hermes_home = os.environ.get("HERMES_HOME")
    base = hermes_home if hermes_home else os.path.expanduser("~/.hermes")
    return os.path.join(base, "logs", "omp-viewer.log")


def request_log_line(method: str, path: str, status: int, nbytes: int, duration_ms: int) -> None:
    line = f"{method} {path} {status} {nbytes} {duration_ms}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with LOG_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _resolved_scan_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in SCAN_ROOTS:
        try:
            root = Path(raw).resolve()
        except OSError:
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def _lane_runs_dir() -> Path:
    """The run-registry directory, resolved like ``default_log_file``."""
    hermes_home = os.environ.get("HERMES_HOME")
    base = hermes_home if hermes_home else os.path.expanduser("~/.hermes")
    return Path(base) / RUNS_DIR_NAME


def _registry_workdirs() -> list[Path]:
    """Workdirs the lane launcher registered, fail-closed per entry.

    An entry counts only with the registry's exact shape (schema version,
    absolute traversal-free workdir) AND a live ``.hermes-lane`` directory
    on disk — a stale, malformed, oversized, or pointing-nowhere entry is
    skipped, never served.
    """
    workdirs: list[Path] = []
    try:
        entries = sorted(_lane_runs_dir().glob("*.json"))
    except OSError:
        return workdirs
    for entry in entries:
        try:
            raw = entry.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_RUN_ENTRY_BYTES:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("version") != 1:
            continue
        workdir = payload.get("workdir")
        if not isinstance(workdir, str) or not workdir or "\x00" in workdir:
            continue
        candidate = Path(workdir)
        if not candidate.is_absolute() or ".." in candidate.parts:
            continue
        try:
            resolved = candidate.resolve()
            lane = resolved / LANE_DIR_NAME
            if not resolved.is_dir() or not lane.is_dir() or lane.is_symlink():
                continue
        except OSError:
            continue
        if resolved not in workdirs:
            workdirs.append(resolved)
    return workdirs


def _walk_scan_roots(roots: list[Path]) -> dict[str, Path]:
    """Walk ``roots`` for workdirs carrying a ``.hermes-lane`` dir.

    Bounded depth, symlinked directories never followed, a symlinked
    ``.hermes-lane`` never counted, unreadable directories skipped. One
    ``os.scandir`` per directory: entry types come from the directory
    stream itself (``follow_symlinks=False``), so the walk pays no
    per-entry stat and no per-directory sort — callers sort the merged
    result once.
    """
    found: dict[str, Path] = {}
    for root in roots:
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                with os.scandir(current) as scan:
                    entries = list(scan)
            except OSError:
                continue
            has_lane = False
            descend = depth < SCAN_MAX_DEPTH
            for entry in entries:
                if entry.name == LANE_DIR_NAME:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            has_lane = True
                    except OSError:
                        pass
                    continue
                if not descend or entry.name in SKIP_DIR_NAMES:
                    continue
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                stack.append((Path(entry.path), depth + 1))
            if has_lane:
                found.setdefault(str(current), current)
    return found


# The scan-root walk is the viewer's one expensive discovery step (a big
# tree costs seconds per pass), and its result only changes when workdirs
# appear or disappear. It is cached per resolved-roots configuration: a
# fresh result is served directly, a stale one is served immediately while
# one background thread re-walks, and a cold configuration walks once in
# the calling thread while concurrent callers wait for that same walk.
SCAN_WALK_CACHE_TTL_SEC = 30.0


def _walk_now() -> float:
    """Aging clock for the walk cache — module-level so tests can pin it."""
    return time.monotonic()


class _ScanWalkState:
    """Cache line for one resolved-roots configuration."""

    __slots__ = ("cond", "result", "at", "walking")

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.result: dict[str, Path] | None = None
        self.at = 0.0
        self.walking = False


_scan_walk_states: dict[tuple[str, ...], _ScanWalkState] = {}
_scan_walk_states_lock = threading.Lock()


def _scan_walk_state(key: tuple[str, ...]) -> _ScanWalkState:
    with _scan_walk_states_lock:
        state = _scan_walk_states.get(key)
        if state is None:
            state = _ScanWalkState()
            _scan_walk_states[key] = state
        return state


def _complete_walk(state: _ScanWalkState, result: dict[str, Path] | None) -> None:
    """Publish a finished walk and release its waiters; failure keeps stale."""
    with state.cond:
        if result is not None:
            state.result = result
            state.at = _walk_now()
        state.walking = False
        state.cond.notify_all()


def _refresh_walk(state: _ScanWalkState, roots: list[Path]) -> None:
    """Background re-walk: a failure keeps the stale result serving."""
    try:
        result = _walk_scan_roots(roots)
    except Exception:
        result = None
    _complete_walk(state, result)


def _scan_workdirs_cached(roots: list[Path]) -> dict[str, Path]:
    """Cached scan-root walk result, single-flight per configuration."""
    state = _scan_walk_state(tuple(str(root) for root in roots))
    with state.cond:
        while True:
            if state.result is not None:
                if _walk_now() - state.at < SCAN_WALK_CACHE_TTL_SEC:
                    return dict(state.result)
                if not state.walking:
                    state.walking = True
                    try:
                        threading.Thread(
                            target=_refresh_walk, args=(state, roots), daemon=True
                        ).start()
                    except Exception:
                        # A failed start must neither fail the request nor
                        # wedge the cache: clear the flag, release waiters,
                        # and let the next stale request try again.
                        _complete_walk(state, None)
                return dict(state.result)
            if state.walking:
                state.cond.wait()
                continue
            state.walking = True
            break
    # Cold configuration: this thread runs the one first walk; concurrent
    # callers wait on the condition above for it.
    try:
        result = _walk_scan_roots(roots)
    except Exception:
        _complete_walk(state, None)
        raise
    _complete_walk(state, result)
    return dict(result)


def _start_scan_warmup(roots: list[Path]) -> None:
    """Warm the scan-walk cache in a background thread at startup.

    Optimisation only: if the thread cannot be started the server must
    still come up — the first /api/runs performs the same cold walk in
    its own thread.
    """
    if not roots:
        return
    try:
        threading.Thread(target=_scan_workdirs_cached, args=(roots,), daemon=True).start()
    except Exception:  # startup hardening: a failed warm-up is never fatal
        pass


def find_lane_workdirs() -> list[Path]:
    """Workdirs carrying a ``.hermes-lane`` dir, bounded depth, no symlinks.

    Discovery is registry-first: every workdir the lane launcher recorded
    is in scope wherever it lives (re-read on every call), and configured
    scan roots add bounded walk-in discovery on top (for runs that predate
    the registry), cached for SCAN_WALK_CACHE_TTL_SEC. A workdir appearing
    under two sources is reported once. Cached scan workdirs are
    re-validated on every call — a ``.hermes-lane`` that went missing or
    became a symlink inside the cache window drops out immediately.
    """
    found: dict[str, Path] = {}
    for workdir in _registry_workdirs():
        found.setdefault(str(workdir), workdir)
    for key, workdir in _scan_workdirs_cached(_resolved_scan_roots()).items():
        lane = workdir / LANE_DIR_NAME
        try:
            if not workdir.is_dir() or not lane.is_dir() or lane.is_symlink():
                continue
        except OSError:
            continue
        found.setdefault(key, workdir)
    return sorted(found.values())


def _live_session_dir(workdir: Path) -> Path | None:
    """The session dir the lane is currently running in, from live.json."""
    live = workdir / LANE_DIR_NAME / "live.json"
    try:
        if not live.is_file():
            return None
        payload = json.loads(live.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session_dir = payload.get("session_dir")
    if not isinstance(session_dir, str) or not session_dir:
        return None
    try:
        return Path(session_dir).resolve()
    except OSError:
        return None


def list_runs() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    now_s = now_ms / 1000.0
    runs: list[dict[str, Any]] = []
    for workdir in find_lane_workdirs():
        lane_root = workdir / LANE_DIR_NAME
        live_dir = _live_session_dir(workdir)
        try:
            lanes = sorted(lane_root.iterdir())
        except OSError:
            continue
        for lane_dir in lanes:
            try:
                if not lane_dir.is_dir() or lane_dir.is_symlink():
                    continue
            except OSError:
                continue
            try:
                transcripts = [
                    p
                    for p in lane_dir.glob("*.jsonl")
                    if p.is_file() and not p.is_symlink()
                ]
            except OSError:
                continue
            if not transcripts:
                continue
            try:
                live_file = (
                    max(transcripts, key=lambda p: p.stat().st_mtime)
                    if live_dir is not None and lane_dir.resolve() == live_dir
                    else None
                )
            except OSError:
                live_file = None
            for entry in transcripts:
                try:
                    st = entry.stat()
                except OSError:
                    continue
                mtime = st.st_mtime
                run: dict[str, Any] = {
                    "workdir": str(workdir),
                    "lane": lane_dir.name,
                    "session_file": str(entry),
                    "size": st.st_size,
                    "mtime": mtime,
                    "live": live_file is not None and entry == live_file,
                    "active": (now_s - mtime) < ACTIVE_WINDOW_SEC,
                }
                run.update(extract_run_metadata(entry))
                runs.append(run)
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return {"now_ms": now_ms, "runs": runs}


# ---------------------------------------------------------------------------
# Transcript resolution (path-traversal safe)
# ---------------------------------------------------------------------------


def resolve_transcript(raw: str) -> Path | None:
    """Resolve an absolute transcript path, or None when it is not servable.

    Servable means: absolute, no ``..`` parts, ``.jsonl`` suffix, shaped like
    ``<workdir>/.hermes-lane/<lane>/<file>.jsonl``, an existing regular file,
    and — after symlink resolution — inside a KNOWN workdir: one the lane
    launcher registered, or one contained in a configured scan root.
    """
    if not raw or "\x00" in raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    if ".." in candidate.parts:
        return None
    if candidate.suffix != ".jsonl":
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    lane_dir = resolved.parent
    lane_root = lane_dir.parent
    if lane_root.name != LANE_DIR_NAME:
        return None
    workdir = lane_root.parent
    try:
        if not resolved.is_file():
            return None
    except OSError:
        return None
    for root in _resolved_scan_roots():
        if workdir == root or root in workdir.parents:
            return resolved
    for known in _registry_workdirs():
        if workdir == known:
            return resolved
    return None


# ---------------------------------------------------------------------------
# OMP transcript parsing
# ---------------------------------------------------------------------------

PREFIX_BYTES = 32 * 1024

# Lane bookkeeping injected around the real conversation: never rendered.
_NOISE_CUSTOM_TYPES = frozenset({"date-cwd-reminder", "system-prompt-digest"})


def parse_json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def is_noise_event(obj: dict[str, Any]) -> bool:
    """Prompt bookkeeping the transcript never renders; dropped at parse time."""
    return (
        obj.get("type") == "custom"
        and obj.get("customType") in _NOISE_CUSTOM_TYPES
    )


def collapse_title(text: str, max_len: int = 96) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:max_len] if len(collapsed) > max_len else collapsed


_TASK_HEADING_RE = re.compile(r"(?im)^#\s+Task\s*$")


def extract_task_section(text: str) -> str | None:
    """The ``# Task`` body of a lane prompt — the meaningful run title.

    Lane prompts lead with role instructions and injected skills; the task
    the lane actually ran sits under the LAST ``# Task`` heading.
    """
    matches = list(_TASK_HEADING_RE.finditer(text))
    if not matches:
        return None
    section = text[matches[-1].end():].strip()
    if not section:
        return None
    return collapse_title(section)


def _content_texts(content: Any) -> list[str]:
    """Text of every text part in an OMP message content list."""
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
    return texts


def first_user_text(obj: dict[str, Any]) -> str | None:
    """First text of a user ``message`` event (the lane's initial prompt)."""
    if obj.get("type") != "message":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    for text in _content_texts(message.get("content")):
        return text
    return None


def extract_run_metadata(path: Path) -> dict[str, str]:
    """Title, model, and cwd from the head of an OMP session transcript.

    Title precedence: the last non-empty ``title`` event (OMP writes these
    asynchronously) → the ``# Task`` section of the first user prompt → the
    collapsed first user text. Model comes from ``model_change``, cwd from
    the ``session`` event.
    """
    title_event: str | None = None
    user_title: str | None = None
    task_title: str | None = None
    model: str | None = None
    cwd: str | None = None

    try:
        with open(path, "rb") as fh:
            raw = fh.read(PREFIX_BYTES)
    except OSError:
        return {}

    if not raw:
        return {}
    if not raw.endswith(b"\n"):
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return {}
        raw = raw[: last_nl + 1]

    for line in raw.decode("utf-8", errors="replace").splitlines():
        obj = parse_json_line(line)
        if obj is None:
            continue

        if obj.get("type") == "title":
            title = str(obj.get("title") or "").strip()
            if title:
                title_event = collapse_title(title)
            continue

        if cwd is None and obj.get("type") == "session":
            value = str(obj.get("cwd") or "").strip()
            if value:
                cwd = value
            continue

        if model is None and obj.get("type") == "model_change":
            value = str(obj.get("model") or "").strip()
            if value:
                model = value
            continue

        if user_title is None:
            text = first_user_text(obj)
            if text:
                task_title = extract_task_section(text)
                user_title = collapse_title(text)

    extra: dict[str, str] = {}
    title = title_event or task_title or user_title
    if title:
        extra["title"] = title
    if model:
        extra["model"] = model
    if cwd:
        extra["cwd"] = cwd
    return extra


# ---------------------------------------------------------------------------
# Bounded jsonl paging (snapshot-correct head pages and forward tail polls)
# ---------------------------------------------------------------------------


def cap_lines_json(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not lines:
        return lines
    payload = json.dumps({"lines": lines})
    if len(payload.encode("utf-8")) <= MAX_RESPONSE_BYTES:
        return lines
    lo, hi = 0, len(lines)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = json.dumps({"lines": lines[-mid:]})
        if len(candidate.encode("utf-8")) <= MAX_RESPONSE_BYTES:
            lo = mid
        else:
            hi = mid - 1
    return lines[-lo:] if lo > 0 else []


# Hard bound on raw bytes scanned per backward request so a page can never
# walk an unbounded noise region.
RAW_SCAN_CAP_BYTES = 8 * 1024 * 1024


def read_last_n_lines(
    path: Path, n: int, before: int = 0
) -> tuple[list[dict[str, Any]], int, bool, int]:
    """Read up to n KEPT events from a window ending `before` bytes before EOF.

    Noise events are dropped at parse time, so n counts kept lines only.
    `before` pages backwards: it is the value a previous call returned, so
    the window ends exactly where the older page's lines began. Returns
    (lines, next_before, has_more, tail_offset) — next_before is what the
    next older call passes; has_more is False once the file start is reached.

    tail_offset is the live-tail cursor captured from the SAME file snapshot
    this read used (one stat(), no later re-probe): the byte offset just past
    the last complete newline, so a tail reader starting there can never skip
    bytes appended after this snapshot. When the snapshot ends mid-line (a
    live session mid-append), the partial line is excluded from the page and
    tail_offset points at its start, so tail polling rereads it once complete.
    """
    n = max(1, min(n, 1000))
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0, False, 0
    if size == 0:
        return [], 0, False, 0

    try:
        before = int(before)
    except (TypeError, ValueError):
        before = 0
    before = max(0, min(before, size))
    end = size - before

    # Scan backwards in chunks, parsing each byte range once. `pending` holds
    # the left-edge bytes of a line whose start lies in an older chunk — an
    # older read completes it, so it is deferred, not truncated. `cursor` is
    # the byte boundary between processed and unprocessed bytes, so every
    # kept line's start is exact even when noise lines are skipped between
    # kept ones. A window whose right edge cuts an unfinished line (a live
    # session mid-append) drops that line rather than guess at partial JSON.
    chunk_size = 65536
    pos = end
    pending = b""
    cursor = end
    kept: list[tuple[dict[str, Any], int]] = []  # newest-first
    drop_tail = False
    first_chunk = True
    # Tail cursor for THIS snapshot: just past the window's last complete
    # newline. A partial line at the snapshot's right edge moves it back to
    # that line's start so tail polling rereads the line once completed.
    tail_offset = end
    with open(path, "rb") as fh:
        if end > 0:
            fh.seek(end - 1)
            drop_tail = fh.read(1) != b"\n"
        while pos > 0 and len(kept) < n and end - pos < RAW_SCAN_CAP_BYTES:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            segments = (fh.read(read_size) + pending).split(b"\n")
            pending = segments[0]
            complete = segments[1:]
            if drop_tail and complete:
                tail = complete.pop()
                cursor -= len(tail)
                tail_offset = end - len(tail)
                drop_tail = False
            elif first_chunk and complete and complete[-1] == b"":
                complete.pop()  # split artifact of the window's trailing newline
            first_chunk = False
            for line in reversed(complete):
                cursor -= len(line) + 1
                obj = parse_json_line(line.decode("utf-8", errors="replace"))
                if obj is not None and not is_noise_event(obj):
                    kept.append((obj, cursor))
                    if len(kept) >= n:
                        break
        if pos == 0 and pending and not drop_tail and len(kept) < n:
            # Scanned to the file start, so `pending` is the first line,
            # complete and newline-terminated unless the file has none at all.
            cursor -= len(pending) + 1
            obj = parse_json_line(pending.decode("utf-8", errors="replace"))
            if obj is not None and not is_noise_event(obj):
                kept.append((obj, cursor))

    kept.reverse()
    lines = cap_lines_json([obj for obj, _ in kept])
    kept = kept[-len(lines):] if lines else []
    if drop_tail:
        # The whole scanned window held no newline, so the partial line's
        # start was never seen (pathological: a single line larger than the
        # scan). Re-read from the file start rather than guess a mid-line cut.
        tail_offset = 0
    # The first kept line's start (not the raw chunk-read position) becomes
    # the next page boundary, so paging never splits or repeats an event —
    # even when noise lines sit between pages. has_more tracks whether the
    # scan itself reached the file start: leading noise must not fake more
    # pages, and a cap-stopped scan (pos > 0) must advertise one.
    first_start = kept[0][1] if kept else pos
    return [obj for obj, _ in kept], size - first_start, pos > 0, tail_offset


def count_kept_lines(path: Path) -> int:
    """Count events a page can render: parsed lines minus dropped noise."""
    total = 0
    with open(path, "rb") as fh:
        for raw in fh:
            obj = parse_json_line(raw.decode("utf-8", errors="replace"))
            if obj is not None and not is_noise_event(obj):
                total += 1
    return total


# Bytes read per scan step below: small enough that a capped page stops just
# past its first omitted event instead of materializing the whole remainder.
TAIL_READ_CHUNK_BYTES = 64 * 1024

# json.dumps({"lines": events}) with default separators is exactly
# '{"lines": [' + ', '.join(dumps(e) for e in events) + ']}', so a page's
# encoded size is EMPTY payload + sum(event bytes) + 2 per separator. The
# running total accounts for that envelope exactly, so each event is
# serialized exactly once and the growing page is never re-serialized.
_EMPTY_LINES_PAYLOAD_BYTES = len(json.dumps({"lines": []}).encode("utf-8"))
_ITEM_SEPARATOR_BYTES = len(", ".encode("utf-8"))  # json.dumps default


def read_from_offset(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    """Read the oldest fitting page of kept events starting at `offset`.

    Scans forward incrementally from `offset`, bounded by the file size
    snapshot taken on entry — bytes appended after that snapshot are left
    for the next poll, never chased. When the next kept event would push the
    JSON page past MAX_RESPONSE_BYTES the scan stops and the returned cursor
    stays at that omitted event's byte start, so polling resumes there and
    every kept event is delivered in order, exactly once. A partial final
    line is never consumed. Total read volume across a full drain is linear
    in file size: each page reads its own span plus at most one chunk
    overshoot, and only the first omitted event is ever reread.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return offset, []

    if offset < 0:
        offset = 0
    if offset > size:
        return size, []

    kept: list[dict[str, Any]] = []
    payload_bytes = _EMPTY_LINES_PAYLOAD_BYTES
    cursor = offset
    leftover = b""
    pos = offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        while pos < size:
            data = fh.read(min(TAIL_READ_CHUNK_BYTES, size - pos))
            if not data:
                break  # truncated since the snapshot
            pos += len(data)
            segments = (leftover + data).split(b"\n")
            leftover = segments.pop()
            for raw_line in segments:
                line_start = cursor
                cursor += len(raw_line) + 1
                obj = parse_json_line(raw_line.decode("utf-8", errors="replace"))
                if obj is None or is_noise_event(obj):
                    continue  # noise/invalid bytes are consumed, never rendered
                event_bytes = len(json.dumps(obj).encode("utf-8"))
                added = event_bytes if not kept else _ITEM_SEPARATOR_BYTES + event_bytes
                if kept and payload_bytes + added > MAX_RESPONSE_BYTES:
                    # Cap reached: stop BEFORE this event. The cursor stays
                    # at its byte start, so the next poll rereads exactly
                    # this one omitted line — the only reread per page.
                    return line_start, kept
                kept.append(obj)
                payload_bytes += added
                if payload_bytes > MAX_RESPONSE_BYTES:
                    # A first kept event larger than the soft cap still ships
                    # alone, so the cursor advances instead of stalling.
                    return cursor, kept
    # `leftover` holds a partial final line: never advance the cursor
    # through bytes that do not yet end in a newline.
    return cursor, kept


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class OmpViewerHandler(BaseHTTPRequestHandler):
    server_version = "omp-viewer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_headers_only(self, status: int, nbytes: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(nbytes))
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json_response(self, status: int, payload: dict[str, Any], head_only: bool = False) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        if head_only:
            self._send_headers_only(status, len(body), "application/json; charset=utf-8")
        else:
            self._send(status, body, "application/json; charset=utf-8")
        return body

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def _dispatch(self, head_only: bool = False) -> None:
        started = time.perf_counter()
        status = 500
        nbytes = 0
        try:
            parsed = urlparse(self.path)
            route = parsed.path
            qs = parse_qs(parsed.query)

            if route == "/":
                status, nbytes = self._handle_index(head_only=head_only)
            elif route == "/DejaVuSansMono.ttf":
                status, nbytes = self._handle_font_file(FONT_PATH, head_only=head_only)
            elif route == "/Inter-Regular.ttf":
                status, nbytes = self._handle_font_file(INTER_REGULAR_PATH, head_only=head_only)
            elif route == "/Inter-Medium.ttf":
                status, nbytes = self._handle_font_file(INTER_MEDIUM_PATH, head_only=head_only)
            elif route == "/api/runs":
                status, nbytes = self._handle_runs(head_only=head_only)
            elif route == "/api/run":
                status, nbytes = self._handle_run(qs, head_only=head_only)
            elif route == "/api/poll":
                status, nbytes = self._handle_poll(qs, head_only=head_only)
            else:
                status, nbytes = self._handle_not_found(head_only=head_only)
        except Exception:
            status = 500
            nbytes = 0
            if not head_only:
                self._send(500, b"Internal Server Error", "text/plain; charset=utf-8")
            else:
                self._send_headers_only(500, 0, "text/plain; charset=utf-8")
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            request_log_line(self.command, self.path, status, nbytes, duration_ms)

    def _handle_index(self, head_only: bool = False) -> tuple[int, int]:
        try:
            with open(UI_PATH, "rb") as fh:
                body = fh.read()
        except OSError:
            if head_only:
                self._send_headers_only(500, 17, "text/plain; charset=utf-8")
            else:
                self._send(500, b"ui.html not found", "text/plain; charset=utf-8")
            return 500, 0
        if head_only:
            self._send_headers_only(200, len(body), "text/html; charset=utf-8")
        else:
            self._send(200, body, "text/html; charset=utf-8")
        return 200, len(body)

    def _handle_font_file(self, font_path: str, head_only: bool = False) -> tuple[int, int]:
        """Serve a bundled font file for ui.html's @font-face (exact route → fixed path)."""
        try:
            with open(font_path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._handle_not_found(head_only=head_only)
        if head_only:
            self._send_headers_only(200, len(body), "font/ttf")
        else:
            self._send(200, body, "font/ttf")
        return 200, len(body)

    def _handle_runs(self, head_only: bool = False) -> tuple[int, int]:
        body = self._json_response(200, list_runs(), head_only=head_only)
        return 200, len(body)

    def _resolve_path_param(self, qs: dict[str, list[str]], head_only: bool) -> tuple[Path | None, int, int]:
        """Shared 400/404 gate for the two transcript endpoints."""
        raw = qs.get("path", [""])[0]
        if not raw:
            body = self._json_response(400, {"error": "invalid path parameter"}, head_only=head_only)
            return None, 400, len(body)
        path = resolve_transcript(unquote(raw))
        if path is None:
            body = self._json_response(404, {"error": "transcript not found"}, head_only=head_only)
            return None, 404, len(body)
        return path, 0, 0

    def _handle_run(self, qs: dict[str, list[str]], head_only: bool = False) -> tuple[int, int]:
        path, status, nbytes = self._resolve_path_param(qs, head_only)
        if path is None:
            return status, nbytes

        try:
            n = int(qs.get("lines", ["200"])[0])
        except ValueError:
            n = 200
        n = max(1, min(n, 1000))

        try:
            before = int(qs.get("before", ["0"])[0])
        except ValueError:
            before = 0

        lines, next_before, has_more, tail_offset = read_last_n_lines(path, n, before)
        payload: dict[str, Any] = {
            "lines": lines,
            "before": next_before,
            "has_more": has_more,
        }
        if before == 0:
            # First page doubles as the live-tail anchor: the cursor comes
            # from the same snapshot as `lines`, so bytes appended between
            # this response and the client's first poll are never skipped.
            # Older pages (before > 0) are mid-file windows and carry no
            # tail cursor.
            payload["tail_offset"] = tail_offset
        # total_lines re-reads the whole file, so only the first (uncapped)
        # request pays for it.
        if qs.get("total", ["0"])[0] in ("1", "true", "True"):
            payload["total_lines"] = count_kept_lines(path)
        body = self._json_response(200, payload, head_only=head_only)
        return 200, len(body)

    def _handle_poll(self, qs: dict[str, list[str]], head_only: bool = False) -> tuple[int, int]:
        path, status, nbytes = self._resolve_path_param(qs, head_only)
        if path is None:
            return status, nbytes

        try:
            offset = int(qs.get("offset", ["0"])[0])
        except ValueError:
            offset = 0

        wait = qs.get("wait", ["0"])[0] in ("1", "true", "True")

        deadline = time.monotonic() + 25.0 if wait else time.monotonic()
        new_offset, lines = read_from_offset(path, offset)

        while wait and not lines and time.monotonic() < deadline:
            time.sleep(0.5)
            new_offset, lines = read_from_offset(path, offset)

        body = self._json_response(200, {"offset": new_offset, "lines": lines}, head_only=head_only)
        return 200, len(body)

    def _handle_not_found(self, head_only: bool = False) -> tuple[int, int]:
        if head_only:
            self._send_headers_only(404, 9, "text/plain; charset=utf-8")
        else:
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
        return 404, 9


def main() -> None:
    global SCAN_ROOTS, SERVER_BIND, SERVER_PORT, LOG_FILE

    parser = argparse.ArgumentParser(description="Live viewer for OMP delegate-lane session transcripts")
    parser.add_argument("--port", type=int, default=None, help="Listen port (default 8790)")
    parser.add_argument("--bind", default=None, help="Bind address (default 127.0.0.1)")
    parser.add_argument(
        "--scan-root",
        action="append",
        default=None,
        help="Directory scanned (bounded depth) for .hermes-lane sessions; repeatable",
    )
    args = parser.parse_args()

    SERVER_PORT = args.port if args.port is not None else int(os.environ.get("OMP_VIEWER_PORT", "8790"))
    SERVER_BIND = args.bind if args.bind is not None else os.environ.get("OMP_VIEWER_BIND", "127.0.0.1")
    if args.scan_root:
        SCAN_ROOTS = list(args.scan_root)
    elif os.environ.get("OMP_VIEWER_SCAN_ROOTS"):
        SCAN_ROOTS = [
            root.strip()
            for root in os.environ["OMP_VIEWER_SCAN_ROOTS"].split(os.pathsep)
            if root.strip()
        ]
    LOG_FILE = default_log_file()

    # Cold start: warm the scan-walk cache in the background, so the first
    # /api/runs waits on this ONE in-flight walk instead of starting its own.
    _start_scan_warmup(_resolved_scan_roots())


    httpd = ThreadingHTTPServer((SERVER_BIND, SERVER_PORT), OmpViewerHandler)
    print(
        f"omp-viewer listening on http://{SERVER_BIND}:{SERVER_PORT}  scan-roots={SCAN_ROOTS}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
