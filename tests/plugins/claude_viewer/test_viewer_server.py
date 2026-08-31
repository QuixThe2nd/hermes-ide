"""The viewer server's head→tail handoff: /api/head's snapshot cursor.

`/api/head` returns `tail_offset`, captured from the SAME file snapshot the
page was read from — never a later stat(). The UI starts tail polling there,
so bytes appended between the snapshot and the first poll are delivered, not
skipped. The cursor sits just past the last complete newline: a snapshot
ending mid-line (a live run mid-append) backs the cursor up to that partial
line's start so tail polling rereads it once complete.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import plugins.claude_viewer.viewer.server as server


def _event(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write_events(path: Path, events: list[dict]) -> int:
    """Append events as jsonl; returns the file size after the writes."""
    with open(path, "ab") as fh:
        for ev in events:
            fh.write(json.dumps(ev).encode("utf-8") + b"\n")
    return path.stat().st_size


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "run.jsonl"


# ── read_last_n_lines: the cursor comes from the read's own snapshot ────


def test_tail_offset_is_eof_for_a_newline_terminated_snapshot(log_file: Path) -> None:
    size = _write_events(log_file, [_event("one"), _event("two"), _event("three")])
    lines, _before, _has_more, tail_offset = server.read_last_n_lines(log_file, 200)
    assert [l["message"]["content"][0]["text"] for l in lines] == ["one", "two", "three"]
    assert tail_offset == size == log_file.stat().st_size


def test_tail_offset_backs_up_to_a_partial_tail_line(log_file: Path) -> None:
    complete_size = _write_events(log_file, [_event("one"), _event("two")])
    partial = json.dumps(_event("half-written"))[:20].encode("utf-8")  # no newline yet
    with open(log_file, "ab") as fh:
        fh.write(partial)

    lines, _before, _has_more, tail_offset = server.read_last_n_lines(log_file, 200)
    # The partial line is neither in the page nor in the cursor's past.
    assert [l["message"]["content"][0]["text"] for l in lines] == ["one", "two"]
    assert tail_offset == complete_size
    assert tail_offset < log_file.stat().st_size

    # Once the line completes, tail polling from the cursor rereads it whole.
    with open(log_file, "ab") as fh:
        fh.write(json.dumps(_event("half-written")).encode("utf-8")[20:] + b"\n")
    new_offset, lines = server.read_from_offset(log_file, tail_offset)
    assert [l["message"]["content"][0]["text"] for l in lines] == ["half-written"]
    assert new_offset == log_file.stat().st_size


def test_tail_offset_spans_a_partial_line_across_chunk_boundaries(log_file: Path) -> None:
    """A partial line larger than one 64KB read chunk is still excluded
    exactly: the cursor lands on its start, not mid-line."""
    complete_size = _write_events(log_file, [_event("one")])
    big = _event("x" * 200_000)
    raw = json.dumps(big).encode("utf-8")
    with open(log_file, "ab") as fh:
        fh.write(raw[:150_000])   # partial, spanning multiple 64KB chunks

    lines, _before, _has_more, tail_offset = server.read_last_n_lines(log_file, 200)
    assert [l["message"]["content"][0]["text"] for l in lines] == ["one"]
    assert tail_offset == complete_size

    with open(log_file, "ab") as fh:
        fh.write(raw[150_000:] + b"\n")
    _offset, lines = server.read_from_offset(log_file, tail_offset)
    assert [l["message"]["content"][0]["text"] for l in lines] == [big["message"]["content"][0]["text"]]


def test_tail_offset_ignores_trailing_noise_lines(log_file: Path) -> None:
    """Noise events are dropped from the page but still occupy bytes: the
    cursor stays past them so they are never re-polled."""
    size = _write_events(log_file, [
        _event("one"),
        {"type": "system", "subtype": "thinking_tokens", "n": 5},
    ])
    lines, _before, _has_more, tail_offset = server.read_last_n_lines(log_file, 200)
    assert [l["message"]["content"][0]["text"] for l in lines] == ["one"]
    assert tail_offset == size


def test_tail_offset_is_zero_for_an_empty_file(log_file: Path) -> None:
    log_file.touch()
    lines, _before, has_more, tail_offset = server.read_last_n_lines(log_file, 200)
    assert lines == [] and has_more is False and tail_offset == 0


# ── /api/head over HTTP: the field rides the first page only ─────────────


@pytest.fixture
def http(tmp_path: Path, monkeypatch):
    """The real handler on an ephemeral port, rooted at a tmp log dir."""
    monkeypatch.setattr(server, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(server, "LOG_FILE", str(tmp_path / "viewer.log"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ClaudeViewerHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield lambda route: json.loads(urllib.request.urlopen(base + route).read())
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_head_first_page_carries_the_snapshot_cursor(http, log_file: Path) -> None:
    size = _write_events(log_file, [_event("one"), _event("two")])
    page = http(f"/api/head?file={log_file.name}&lines=200&total=1")
    assert page["tail_offset"] == size
    assert len(page["lines"]) == 2

    # History pages are mid-file windows: no tail cursor rides along.
    older = http(f"/api/head?file={log_file.name}&lines=1&before={page['before']}")
    assert "tail_offset" not in older


def test_head_cursor_covers_events_appended_after_the_snapshot(http, log_file: Path) -> None:
    """The gap, end to end: events appended after the /api/head response are
    returned by the first tail poll from the returned cursor — while a later
    size probe (the old anchor) would have skipped them forever."""
    _write_events(log_file, [_event("head one"), _event("head two")])
    page = http(f"/api/head?file={log_file.name}&lines=200")
    cursor = page["tail_offset"]

    # Bytes land after the snapshot but before the client's first tail poll.
    _write_events(log_file, [_event("gap A"), _event("gap B")])

    polled = http(f"/api/tail?file={log_file.name}&offset={cursor}")
    texts = [l["message"]["content"][0]["text"] for l in polled["lines"]]
    assert texts == ["gap A", "gap B"]                    # nothing skipped
    assert polled["offset"] == log_file.stat().st_size

    # Polling again from the advanced offset returns nothing: no duplicates.
    again = http(f"/api/tail?file={log_file.name}&offset={polled['offset']}")
    assert again["lines"] == []

    # The pre-fix anchor — a size probe taken now — lies past the gap events.
    later_size = log_file.stat().st_size
    assert later_size > cursor
    skipped = http(f"/api/tail?file={log_file.name}&offset={later_size}")
    assert skipped["lines"] == []                          # i.e. they would be lost


def test_head_cursor_rereads_a_partial_line_completed_after_snapshot(http, log_file: Path) -> None:
    complete_size = _write_events(log_file, [_event("head one")])
    raw = json.dumps(_event("completed later")).encode("utf-8")
    with open(log_file, "ab") as fh:
        fh.write(raw[:10])   # snapshot ends mid-line

    page = http(f"/api/head?file={log_file.name}&lines=200")
    assert page["tail_offset"] == complete_size
    assert len(page["lines"]) == 1

    # Mid-completion the line is still held back entirely...
    with open(log_file, "ab") as fh:
        fh.write(raw[10:20])
    mid = http(f"/api/tail?file={log_file.name}&offset={page['tail_offset']}")
    assert mid["lines"] == []
    assert mid["offset"] == page["tail_offset"]

    # ...and once the newline lands the whole line arrives exactly once.
    with open(log_file, "ab") as fh:
        fh.write(raw[20:] + b"\n")
    done = http(f"/api/tail?file={log_file.name}&offset={page['tail_offset']}")
    texts = [l["message"]["content"][0]["text"] for l in done["lines"]]
    assert texts == ["completed later"]
