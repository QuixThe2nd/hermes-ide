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


# ── read_from_offset: lossless forward tail pagination ───────────────────
#
# A hidden tab can leave tail polling suspended while output accumulates.
# When the backlog exceeds MAX_RESPONSE_BYTES the cap must page forward from
# the oldest kept event — never keep a newest suffix while advancing the
# cursor past the omitted prefix (the Codex P2 these tests pin down).


def _drain_tail(path: Path) -> tuple[list[str], list[int]]:
    """Page read_from_offset from 0 to EOF. Returns (kept texts, offsets)."""
    texts: list[str] = []
    offsets: list[int] = []
    offset = 0
    size = path.stat().st_size
    for _ in range(1000):
        new_offset, lines = server.read_from_offset(path, offset)
        texts.extend(l["message"]["content"][0]["text"] for l in lines)
        offsets.append(new_offset)
        if new_offset >= size:
            return texts, offsets
        assert new_offset > offset  # every page makes progress
        offset = new_offset
    raise AssertionError("tail pagination did not converge")


def test_capped_tail_pages_reconstruct_the_full_backlog_in_order(
    log_file: Path, monkeypatch
) -> None:
    """A multi-page backlog larger than the cap rebuilds every kept event in
    original order — no loss, no duplication. Under the old
    `cap_lines_json(parsed)` behavior this fails on the missing prefix: the
    oldest events are dropped while the cursor advances past them."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    _write_events(log_file, [_event(f"event {i}") for i in range(30)])
    assert log_file.stat().st_size > 512  # genuinely larger than the cap

    texts, offsets = _drain_tail(log_file)
    assert texts == [f"event {i}" for i in range(30)]
    assert len(offsets) > 1  # genuinely multi-page


def test_capped_first_page_is_the_oldest_prefix_not_the_newest_suffix(
    log_file: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    events = [_event(f"event {i}") for i in range(30)]
    _write_events(log_file, events)

    offset, page1 = server.read_from_offset(log_file, 0)
    first_texts = [l["message"]["content"][0]["text"] for l in page1]
    assert first_texts == [f"event {i}" for i in range(len(page1))]
    assert first_texts[0] == "event 0"
    assert "event 29" not in first_texts  # not the old newest suffix

    # The cursor stops at the byte end of the last returned event, so the
    # next page starts exactly at the first omitted event.
    end_of_last = sum(len(json.dumps(e).encode("utf-8")) + 1 for e in events[: len(page1)])
    assert offset == end_of_last
    assert offset < log_file.stat().st_size  # more kept rows remain


def test_capped_tail_offsets_are_monotonic_and_stop_at_last_represented_event(
    log_file: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    events = [_event(f"event {i}") for i in range(30)]
    _write_events(log_file, events)

    offset = 0
    seen = 0
    prev = -1
    size = log_file.stat().st_size
    while offset < size:
        new_offset, lines = server.read_from_offset(log_file, offset)
        assert lines
        assert new_offset > prev  # strictly monotonic across pages
        prev = new_offset
        seen += len(lines)
        byte_end = sum(len(json.dumps(e).encode("utf-8")) + 1 for e in events[:seen])
        assert new_offset == byte_end  # never past an unrepresented event
        if new_offset < size:
            # The next page's first event is exactly the one after the last
            # represented event — the offset stopped at its predecessor.
            _, following = server.read_from_offset(log_file, new_offset)
            assert following[0]["message"]["content"][0]["text"] == f"event {seen}"
        offset = new_offset
    assert seen == 30


def test_interleaved_noise_never_skips_kept_events_across_pages(
    log_file: Path, monkeypatch
) -> None:
    """Noise between kept rows may be reread but never renders and never
    causes a kept event to be skipped — on any page."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    noise = {"type": "system", "subtype": "thinking_tokens", "n": 1}
    events: list[dict] = []
    for i in range(20):
        events.append(noise)
        events.append(_event(f"kept {i}"))
    events.extend([noise, noise])  # trailing noise after the last kept row
    _write_events(log_file, events)

    texts, offsets = _drain_tail(log_file)
    assert texts == [f"kept {i}" for i in range(20)]
    assert offsets[-1] == log_file.stat().st_size  # noise bytes still consumed


def test_incomplete_trailing_line_is_withheld_then_delivered_after_completion(
    log_file: Path,
) -> None:
    _write_events(log_file, [_event("complete")])
    raw = json.dumps(_event("still writing")).encode("utf-8")
    with open(log_file, "ab") as fh:
        fh.write(raw[:15])  # no newline yet

    offset, lines = server.read_from_offset(log_file, 0)
    assert [l["message"]["content"][0]["text"] for l in lines] == ["complete"]
    assert offset < log_file.stat().st_size  # partial line not consumed

    with open(log_file, "ab") as fh:
        fh.write(raw[15:25])
    mid_offset, mid_lines = server.read_from_offset(log_file, offset)
    assert mid_lines == []
    assert mid_offset == offset  # cursor holds at the partial line's start

    with open(log_file, "ab") as fh:
        fh.write(raw[25:] + b"\n")
    final_offset, final_lines = server.read_from_offset(log_file, offset)
    assert [l["message"]["content"][0]["text"] for l in final_lines] == ["still writing"]
    assert final_offset == log_file.stat().st_size


def test_oversized_single_event_is_delivered_and_advances_the_cursor(
    log_file: Path, monkeypatch
) -> None:
    """One event larger than the cap ships alone (soft page cap) so the
    cursor advances instead of returning empty pages forever."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    big = _event("x" * 2000)
    assert len(json.dumps({"lines": [big]}).encode("utf-8")) > 512
    _write_events(log_file, [big, _event("after")])

    offset1, page1 = server.read_from_offset(log_file, 0)
    assert [l["message"]["content"][0]["text"] for l in page1] == ["x" * 2000]
    assert offset1 == len(json.dumps(big).encode("utf-8")) + 1
    assert offset1 < log_file.stat().st_size

    offset2, page2 = server.read_from_offset(log_file, offset1)
    assert [l["message"]["content"][0]["text"] for l in page2] == ["after"]
    assert offset2 == log_file.stat().st_size


# ── read_from_offset: bounded linear-time scanning (Codex P2) ────────────
#
# The scan must be incremental: a capped page reads only through its first
# omitted event, and draining N pages costs O(file size) total input — not
# the old fh.read()-to-EOF that rescanned and re-materialized the whole
# remainder on every page (O(pages × file)).


class _ReadMeter:
    """Counts bytes the server reads from run logs, via its open() global."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_open = builtins.open
        self.bytes_read = 0

        def metering_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if not (isinstance(mode, str) and "r" in mode and "b" in mode):
                return fh
            meter = self

            class _Wrapped:
                def read(self, n: int = -1) -> bytes:
                    data = fh.read(n)
                    meter.bytes_read += len(data)
                    return data

                def __getattr__(self, name: str):
                    return getattr(fh, name)

                def __enter__(self):
                    return self

                def __exit__(self, *exc) -> bool:
                    fh.close()
                    return False

            return _Wrapped()

        monkeypatch.setattr(server, "open", metering_open, raising=False)


def _padded_events(count: int, pad: int = 64) -> list[dict]:
    return [_event(f"event {i:02d} " + "y" * pad) for i in range(count)]


def test_capped_first_page_stops_reading_at_the_first_omitted_event(
    log_file: Path, monkeypatch
) -> None:
    """Page one's read volume is bounded by the first omitted event plus one
    chunk — far less than the multi-page backlog behind it. This is the test
    the old full-remainder behavior fails, and it fails on read volume: the
    old fh.read() pulled the ENTIRE remainder (the whole file here) on page
    one alone, blowing past both bounds below."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    # Optional constant: pre-chunk servers don't define it, so raising=False
    # lets the mutation run reach the bytes-read assertions below.
    monkeypatch.setattr(server, "TAIL_READ_CHUNK_BYTES", 256, raising=False)
    events = _padded_events(30)
    _write_events(log_file, events)
    size = log_file.stat().st_size
    assert size > 4 * 512  # genuinely a multi-page backlog

    meter = _ReadMeter(monkeypatch)
    offset, page1 = server.read_from_offset(log_file, 0)
    assert page1
    assert len(page1) < len(events)

    # The scan stops at the first omitted kept event, so reads cover at most
    # that event's byte end rounded up to the chunk being read.
    omitted_end = sum(
        len(json.dumps(e).encode("utf-8")) + 1 for e in events[: len(page1) + 1]
    )
    assert meter.bytes_read <= omitted_end + server.TAIL_READ_CHUNK_BYTES
    # Far less than the remaining backlog the old code read in full.
    assert meter.bytes_read < size // 2
    # And the returned cursor still sits exactly at that omitted event.
    assert offset == omitted_end - (len(json.dumps(events[len(page1)]).encode("utf-8")) + 1)


def test_draining_all_pages_has_linear_total_read_volume(
    log_file: Path, monkeypatch
) -> None:
    """Total input across a full drain is the file size plus at most one
    chunk overshoot and one omitted-line reread per page — linear in the
    file, not the old O(pages × remainder) rescan. Under the old behavior
    this fails on volume: page k reread ~size - offset_k bytes, totaling
    roughly pages × size / 2."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    # Optional constant, as above: absent on the old full-remainder server.
    monkeypatch.setattr(server, "TAIL_READ_CHUNK_BYTES", 256, raising=False)
    events = _padded_events(30)
    _write_events(log_file, events)
    size = log_file.stat().st_size
    line_lens = [len(json.dumps(e).encode("utf-8")) + 1 for e in events]
    max_line = max(line_lens)

    meter = _ReadMeter(monkeypatch)
    texts: list[str] = []
    offset = 0
    pages = 0
    while offset < size:
        new_offset, lines = server.read_from_offset(log_file, offset)
        assert lines, "every page of an all-kept file returns events"
        assert new_offset > offset
        texts.extend(l["message"]["content"][0]["text"] for l in lines)
        offset = new_offset
        pages += 1
        assert pages < 100

    # Exactly-once, in order — the cap never loses or repeats an event.
    assert texts == [f"event {i:02d} " + "y" * 64 for i in range(30)]
    assert pages > 1

    # Linear bound: file bytes once, plus per page one chunk of overshoot
    # and at most one reread omitted line (bounded by the longest line).
    bound = size + pages * (server.TAIL_READ_CHUNK_BYTES + max_line)
    assert meter.bytes_read <= bound
    # Emphatically sub-quadratic: well under two full passes over the file.
    assert meter.bytes_read < 2 * size


def test_tail_read_does_not_chase_bytes_appended_after_the_snapshot(
    log_file: Path, monkeypatch
) -> None:
    """The read is bounded by the opening stat(): bytes landing after that
    snapshot are left for the next poll, not consumed mid-scan."""
    import builtins

    real_open = builtins.open
    size_before = _write_events(log_file, [_event("one"), _event("two")])

    appended = False

    def appending_open(file, mode="r", *args, **kwargs):
        nonlocal appended
        if isinstance(mode, str) and "rb" in mode and not appended:
            appended = True
            _write_events(log_file, [_event("late")])
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(server, "open", appending_open, raising=False)

    offset, lines = server.read_from_offset(log_file, 0)
    assert appended  # the append really happened mid-call
    assert [l["message"]["content"][0]["text"] for l in lines] == ["one", "two"]
    assert offset == size_before  # snapshot bound, not the live EOF

    # The next poll from the snapshot cursor picks the late event up.
    offset2, lines2 = server.read_from_offset(log_file, offset)
    assert [l["message"]["content"][0]["text"] for l in lines2] == ["late"]
    assert offset2 == log_file.stat().st_size


def test_invalid_json_and_bad_utf8_lines_are_consumed_never_rendered(
    log_file: Path, monkeypatch
) -> None:
    """Unparseable lines (invalid JSON, undecodable UTF-8, blank lines) are
    consumed safely — they never render and never block or reorder kept
    events across capped pages."""
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    _write_events(log_file, [_event("before")])
    with open(log_file, "ab") as fh:
        fh.write(b"this is not json\n")
        fh.write(b'{"type": "assistant", broken\n')
        fh.write(b"\xff\xfe undecodable\n")
        fh.write(b"\n")
    _write_events(log_file, [_event("after")])

    texts, offsets = _drain_tail(log_file)
    assert texts == ["before", "after"]
    assert offsets[-1] == log_file.stat().st_size  # garbage bytes consumed
