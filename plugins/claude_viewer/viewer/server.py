#!/usr/bin/env python3
"""claude-viewer — live web UI for Hermes delegate_claude_agent stream-json runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# One response may carry a run's whole visible transcript: after noise
# filtering, a ~5.6MB run serializes to ~1.1MB of kept events, which the old
# 512KB cap truncated mid-run. Larger runs still trim (and page) past this.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(SCRIPT_DIR, "ui.html")
FONT_PATH = os.path.join(SCRIPT_DIR, "DejaVuSansMono.ttf")
INTER_REGULAR_PATH = os.path.join(SCRIPT_DIR, "Inter-Regular.ttf")
INTER_MEDIUM_PATH = os.path.join(SCRIPT_DIR, "Inter-Medium.ttf")
LOG_FILE = os.path.expanduser("~/.hermes/logs/claude-viewer.log")
LOG_LOCK = threading.Lock()

LOG_DIR: str = ""
SERVER_BIND = "0.0.0.0"
SERVER_PORT = 8787


def default_log_dir() -> str:
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return os.path.join(hermes_home, "claude-runs")
    return os.path.expanduser("~/.hermes/claude-runs")


def request_log_line(method: str, path: str, status: int, nbytes: int, duration_ms: int) -> None:
    line = f"{method} {path} {status} {nbytes} {duration_ms}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with LOG_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)


def is_safe_filename(name: str) -> bool:
    if not name:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return name == os.path.basename(name)


def resolve_log_file(filename: str) -> Path | None:
    if not is_safe_filename(filename):
        return None
    path = Path(LOG_DIR) / filename
    try:
        resolved = path.resolve()
        log_root = Path(LOG_DIR).resolve()
        if resolved.parent != log_root:
            return None
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return resolved


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
    """Pure telemetry the UI never renders; dropped at parse time."""
    return obj.get("type") == "system" and obj.get("subtype") == "thinking_tokens"


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
# walk an unbounded noise region. Sized above the largest run file (~5.6MB):
# noise dominates runs so heavily that a smaller cap truncates the first page
# of the very files the filter exists to fix.
RAW_SCAN_CAP_BYTES = 8 * 1024 * 1024


def read_last_n_lines(
    path: Path, n: int, before: int = 0
) -> tuple[list[dict[str, Any]], int, bool, int]:
    """Read up to n KEPT JSON events from a window ending `before` bytes before EOF.

    Noise events are dropped at parse time (they never render), so n counts
    kept lines only — a 200-line page renders ~200 lines, not ~6. `before`
    pages backwards: it is the value a previous call returned, so the window
    ends exactly where the older page's lines began. Returns (lines,
    next_before, has_more, tail_offset) — next_before is what the next older
    call passes and has_more is False once the file start has been reached.

    tail_offset is the live-tail cursor captured from the SAME file snapshot
    this read used (one stat(), no later re-probe): the byte offset just past
    the last complete newline, so a tail reader starting there can never skip
    bytes appended after this snapshot. When the snapshot ends mid-line (a
    live tail mid-append), the partial line is excluded from the page and
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
    # tail mid-append) drops that line rather than guess at partial JSON.
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


PREFIX_BYTES = 32 * 1024


def collapse_title(text: str, max_len: int = 72) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:max_len] if len(collapsed) > max_len else collapsed


def extract_assistant_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            text = str(block["text"]).strip()
            if text:
                return text
    return None


SKIP_TITLE_PREFIXES = (
    "API Error",
    "Failed to authenticate",
    "Goal condition is limited",
)
SKIP_TITLE_CONTAINS = (
    "Usage limit reached",
    "Request rejected (429)",
    "You've reached your weekly",
)


def is_skip_title(text: str) -> bool:
    lowered = text.lower()
    if lowered.startswith(tuple(p.lower() for p in SKIP_TITLE_PREFIXES)):
        return True
    return any(phrase.lower() in lowered for phrase in SKIP_TITLE_CONTAINS)


def extract_user_prompt_text(content: Any) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text if text else None
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            continue
        if block.get("type") == "text" and block.get("text"):
            text = str(block["text"]).strip()
            if text:
                return text
    return None


def enrich_run_metadata(path: Path) -> dict[str, str]:
    assistant_title: str | None = None
    user_title: str | None = None
    project: str | None = None

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

        if obj.get("type") == "system" and obj.get("subtype") == "thinking_tokens":
            continue

        if project is None and obj.get("type") == "system" and obj.get("subtype") == "init":
            cwd = obj.get("cwd")
            if cwd:
                cwd_str = str(cwd).rstrip("/\\")
                base = os.path.basename(cwd_str)
                if base:
                    project = base

        if assistant_title is None and obj.get("type") == "assistant":
            message = obj.get("message")
            if isinstance(message, dict):
                text = extract_assistant_text(message.get("content"))
                if text:
                    title = collapse_title(text)
                    if not is_skip_title(title):
                        assistant_title = title

        if user_title is None and obj.get("type") == "user":
            message = obj.get("message")
            if isinstance(message, dict):
                text = extract_user_prompt_text(message.get("content"))
                if text:
                    title = collapse_title(text)
                    if not is_skip_title(title):
                        user_title = title

        if project and assistant_title:
            break

    extra: dict[str, str] = {}
    if assistant_title:
        extra["title"] = assistant_title
    elif user_title:
        extra["title"] = user_title
    if project:
        extra["project"] = project
    return extra


# stream-json never carries the initial `-p` prompt itself, so a sidecar or
# prefix event may supply it; anything longer than this is truncated, not dropped.
MAX_PROMPT_CHARS = 100_000


def read_run_prompt(path: Path) -> str | None:
    """The run's initial `-p` prompt, or None when no source has it.

    Source 1: `<stem>.prompt.md` next to the jsonl (the primary Hermes store).
    Source 2: a jsonl prefix event — an explicit `hermes/initial_prompt`
    marker or a `user` message with a non-tool_result text block (real logs
    only ever carry tool_results, so this stays a fallback). Never invent a
    prompt from TASK.md/cwd.
    """
    sidecar = path.with_name(path.stem + ".prompt.md")
    try:
        resolved = sidecar.resolve()
        if resolved.parent == Path(LOG_DIR).resolve() and resolved.is_file():
            text = resolved.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text[:MAX_PROMPT_CHARS]
    except (OSError, ValueError):
        pass

    try:
        with open(path, "rb") as fh:
            raw = fh.read(PREFIX_BYTES)
    except OSError:
        return None
    if not raw.endswith(b"\n"):
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return None
        raw = raw[: last_nl + 1]

    for line in raw.decode("utf-8", errors="replace").splitlines():
        obj = parse_json_line(line)
        if obj is None:
            continue
        if obj.get("type") == "hermes" and obj.get("subtype") == "initial_prompt":
            text = str(obj.get("text") or "").strip()
            if text:
                return text[:MAX_PROMPT_CHARS]
        if obj.get("type") == "user":
            message = obj.get("message")
            if isinstance(message, dict):
                text = extract_user_prompt_text(message.get("content"))
                if text:
                    return text[:MAX_PROMPT_CHARS]
    return None


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


def list_runs() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    now_s = now_ms / 1000.0
    runs: list[dict[str, Any]] = []
    log_path = Path(LOG_DIR)
    if log_path.is_dir():
        for entry in log_path.glob("*.jsonl"):
            try:
                st = entry.stat()
            except OSError:
                continue
            mtime = st.st_mtime
            run: dict[str, Any] = {
                "file": entry.name,
                "size": st.st_size,
                "mtime": mtime,
                "active": (now_s - mtime) < 15,
            }
            run.update(enrich_run_metadata(entry))
            runs.append(run)
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return {"now_ms": now_ms, "runs": runs}


class ClaudeViewerHandler(BaseHTTPRequestHandler):
    server_version = "claude-viewer/1.0"

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
            elif route == "/api/head":
                status, nbytes = self._handle_head(qs, head_only=head_only)
            elif route == "/api/tail":
                status, nbytes = self._handle_tail(qs, head_only=head_only)
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

    def _handle_head(self, qs: dict[str, list[str]], head_only: bool = False) -> tuple[int, int]:
        filename = qs.get("file", [""])[0]
        if not is_safe_filename(filename):
            body = self._json_response(400, {"error": "invalid file parameter"}, head_only=head_only)
            return 400, len(body)

        path = resolve_log_file(filename)
        if path is None:
            body = self._json_response(404, {"error": "file not found"}, head_only=head_only)
            return 404, len(body)

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
        # `prompt` rides on every page (not `lines`) so history paging to the
        # start never duplicates it.
        payload: dict[str, Any] = {
            "lines": lines,
            "before": next_before,
            "has_more": has_more,
            "prompt": read_run_prompt(path),
        }
        if before == 0:
            # First page doubles as the live-tail anchor: the cursor comes
            # from the same snapshot as `lines`, so bytes appended between
            # this response and the client's first tail poll are never
            # skipped. Older pages (before > 0) are mid-file windows and
            # carry no tail cursor.
            payload["tail_offset"] = tail_offset
        # total_lines re-reads the whole file, so only the first (uncapped)
        # request pays for it.
        if qs.get("total", ["0"])[0] in ("1", "true", "True"):
            payload["total_lines"] = count_kept_lines(path)
        body = self._json_response(200, payload, head_only=head_only)
        return 200, len(body)

    def _handle_tail(self, qs: dict[str, list[str]], head_only: bool = False) -> tuple[int, int]:
        filename = qs.get("file", [""])[0]
        if not is_safe_filename(filename):
            body = self._json_response(400, {"error": "invalid file parameter"}, head_only=head_only)
            return 400, len(body)

        path = resolve_log_file(filename)
        if path is None:
            body = self._json_response(404, {"error": "file not found"}, head_only=head_only)
            return 404, len(body)

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
    global LOG_DIR, SERVER_BIND, SERVER_PORT

    parser = argparse.ArgumentParser(description="Live viewer for Claude Code stream-json runs")
    parser.add_argument("--port", type=int, default=None, help="Listen port (default 8787)")
    parser.add_argument("--bind", default=None, help="Bind address (default 0.0.0.0)")
    parser.add_argument("--log-dir", default=None, help="Directory containing *.jsonl run logs")
    args = parser.parse_args()

    SERVER_PORT = args.port if args.port is not None else int(os.environ.get("CLAUDE_VIEWER_PORT", "8787"))
    SERVER_BIND = args.bind if args.bind is not None else os.environ.get("CLAUDE_VIEWER_BIND", "0.0.0.0")
    LOG_DIR = os.path.abspath(args.log_dir if args.log_dir else default_log_dir())

    os.makedirs(LOG_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer((SERVER_BIND, SERVER_PORT), ClaudeViewerHandler)
    print(f"claude-viewer listening on http://{SERVER_BIND}:{SERVER_PORT}  log-dir={LOG_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
