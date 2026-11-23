"""The omp viewer server: lane scan, path safety, omp event parsing, paging.

Covers the four contract surfaces of ``plugins/omp_viewer/viewer/server.py``:

- run discovery: ``.hermes-lane/<lane>/*.jsonl`` under bounded scan roots,
  with ``live.json`` marking the currently running lane session;
- transcript resolution: absolute ``path`` params are shape-checked
  (``<workdir>/.hermes-lane/<lane>/<file>.jsonl``) and resolve-contained
  inside a configured scan root — traversal and escapes are rejected;
- omp event parsing: transcript metadata (title from the task section of
  the first user prompt, model from ``model_change``, cwd from ``session``)
  and the noise filter (``date-cwd-reminder`` / ``system-prompt-digest``
  bookkeeping never renders); wire events such as ``turn_end`` assistant
  text, ``error`` events, and ``toolCall``/``toolResult`` messages round-trip
  unmodified;
- head→poll handoff: ``/api/run``'s first page carries ``tail_offset`` from
  the SAME file snapshot, and ``/api/poll`` pages a capped backlog forward
  losslessly in order.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import plugins.omp_viewer.viewer.server as server


# ── omp event fixtures (shapes learned from real .hermes-lane transcripts) ──


def _ts() -> str:
    return "2026-11-15T16:19:33.677Z"


def _session_event(cwd: str) -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": "01a1e001-b31b-76a6-9194-1fe9ec3abe27",
        "timestamp": _ts(),
        "cwd": cwd,
    }


def _model_change(model: str = "litellm/backend_dev") -> dict:
    return {
        "type": "model_change",
        "id": "36070938",
        "parentId": None,
        "timestamp": _ts(),
        "model": model,
        "resolvedModelIsFallback": False,
    }


def _user_event(text: str) -> dict:
    return {
        "type": "message",
        "id": "eebf10f6",
        "parentId": "ac01e871",
        "timestamp": _ts(),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_event(text: str) -> dict:
    return {
        "type": "message",
        "id": "a1",
        "parentId": "eebf10f6",
        "timestamp": _ts(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _thinking_event(text: str = "planning") -> dict:
    return {
        "type": "message",
        "id": "a0",
        "parentId": "eebf10f6",
        "timestamp": _ts(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": text,
                    "thinkingSignature": "abc123",
                }
            ],
        },
    }


def _tool_call_event(call_id: str = "tool_R9saFMoa1Y5xomFt3t1S3vkc") -> dict:
    return {
        "type": "message",
        "id": "a2",
        "parentId": "a1",
        "timestamp": _ts(),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": "read",
                    "arguments": {"path": "."},
                    "intent": "Inspecting candidate worktree layout",
                }
            ],
        },
    }


def _tool_result_event(
    call_id: str = "tool_R9saFMoa1Y5xomFt3t1S3vkc",
    text: str = ".\n  - AGENTS.md",
    is_error: bool = False,
) -> dict:
    return {
        "type": "message",
        "id": "a3",
        "parentId": "a2",
        "timestamp": _ts(),
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "read",
            "content": [{"type": "text", "text": text}],
            "details": {"isDirectory": True},
            "isError": is_error,
            "timestamp": 1794759604926,
        },
    }


def _turn_end_event(text: str) -> dict:
    """omp wire stream event: carries the turn's final assistant text."""
    return {
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _error_event(message: str = "boom", status: int = 500) -> dict:
    return {
        "type": "error",
        "error": {"message": message, "errorStatus": status},
    }


def _noise_event() -> dict:
    """Prompt bookkeeping the transcript never renders."""
    return {
        "type": "custom",
        "customType": "date-cwd-reminder",
        "data": {"reminder": "<system-reminder>Today…</system-reminder>"},
        "id": "ac01e871",
        "parentId": "5b63b197",
        "timestamp": _ts(),
    }


def _digest_noise_event() -> dict:
    return {
        "type": "custom",
        "customType": "system-prompt-digest",
        "data": {"sha256": "00" * 32},
        "id": "d1",
        "parentId": None,
        "timestamp": _ts(),
    }


def _write_events(path: Path, events: list[dict]) -> int:
    """Append events as jsonl; returns the file size after the writes."""
    with open(path, "ab") as fh:
        for ev in events:
            fh.write(json.dumps(ev).encode("utf-8") + b"\n")
    return path.stat().st_size


def _assistant_text(ev: dict) -> str:
    return ev["message"]["content"][0]["text"]


@pytest.fixture
def scan_root(tmp_path: Path, monkeypatch) -> Path:
    """Point the server at a tmp scan root."""
    monkeypatch.setattr(server, "SCAN_ROOTS", [str(tmp_path)])
    return tmp_path


def _make_session(
    root: Path,
    workdir: str,
    lane: str,
    name: str,
    events: list[dict],
) -> Path:
    lane_dir = root / workdir / ".hermes-lane" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / name
    _write_events(path, events)
    return path


@pytest.fixture
def session_file(scan_root: Path) -> Path:
    return _make_session(
        scan_root,
        "proj-a",
        "backend_dev",
        "2026-11-15T16-19-30-203Z_01a1e001-b31b-76a6-9194-1fe9ec3abe27.jsonl",
        [
            _session_event(str(scan_root / "proj-a")),
            _model_change(),
            _user_event("# Task\nFix the quota backoff"),
            _assistant_event("one"),
        ],
    )


# ── run discovery ─────────────────────────────────────────────────────────


def test_list_runs_discovers_lane_transcripts(scan_root: Path) -> None:
    first = _make_session(
        scan_root, "proj-a", "backend_dev", "a_00000000-0000-0000-0000-000000000001.jsonl",
        [_user_event("# Task\nAlpha task")],
    )
    second = _make_session(
        scan_root, "proj-b", "frontend_dev", "b_00000000-0000-0000-0000-000000000002.jsonl",
        [_user_event("# Task\nBeta task")],
    )
    import os

    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))

    runs = server.list_runs()["runs"]
    assert [r["session_file"] for r in runs] == [str(second), str(first)]
    by_file = {r["session_file"]: r for r in runs}
    ra = by_file[str(first)]
    assert ra["workdir"] == str(scan_root / "proj-a")
    assert ra["lane"] == "backend_dev"
    assert ra["live"] is False
    assert ra["size"] > 0
    assert ra["title"] == "Alpha task"
    rb = by_file[str(second)]
    assert rb["lane"] == "frontend_dev"


def test_list_runs_skips_excluded_dirs_and_symlinks(
    scan_root: Path, tmp_path: Path
) -> None:
    for excluded in ("node_modules", ".git", ".venv"):
        _make_session(
            scan_root, f"pkg/{excluded}/proj", "backend_dev", "x.jsonl",
            [_user_event("hidden")],
        )
    # A symlinked workdir must not be followed (nor duplicate the target).
    _make_session(scan_root, "real-proj", "backend_dev", "y.jsonl", [_user_event("real")])
    link = scan_root / "linked-proj"
    try:
        link.symlink_to(scan_root / "real-proj", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    runs = server.list_runs()["runs"]
    files = [r["session_file"] for r in runs]
    assert files == [str(scan_root / "real-proj" / ".hermes-lane" / "backend_dev" / "y.jsonl")]


def test_list_runs_depth_bound(scan_root: Path) -> None:
    """maxdepth 3: a workdir four levels below a scan root is out of scope."""
    _make_session(
        scan_root, "a/b/c/deep-proj", "backend_dev", "deep.jsonl", [_user_event("deep")]
    )
    _make_session(
        scan_root, "a/b/shallow-proj", "backend_dev", "shallow.jsonl", [_user_event("ok")]
    )
    files = [r["session_file"] for r in server.list_runs()["runs"]]
    assert any("shallow.jsonl" in f for f in files)
    assert not any("deep.jsonl" in f for f in files)


def test_list_runs_live_flag_from_live_json(scan_root: Path) -> None:
    workdir = scan_root / "proj-live"
    lane_dir = workdir / ".hermes-lane" / "backend_dev"
    lane_dir.mkdir(parents=True)
    older = lane_dir / "2026-11-15T01-00-00-000Z_00000000-0000-0000-0000-0000000000aa.jsonl"
    newer = lane_dir / "2026-11-15T02-00-00-000Z_00000000-0000-0000-0000-0000000000bb.jsonl"
    _write_events(older, [_user_event("old")])
    _write_events(newer, [_user_event("new")])
    import os

    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))
    (workdir / ".hermes-lane" / "live.json").write_text(
        json.dumps({"session_dir": str(lane_dir), "model": "backend_dev"}) + "\n",
        encoding="utf-8",
    )

    runs = {r["session_file"]: r for r in server.list_runs()["runs"]}
    assert runs[str(newer)]["live"] is True
    assert runs[str(older)]["live"] is False


def test_list_runs_empty_when_no_lanes(scan_root: Path) -> None:
    (scan_root / "plain-project").mkdir()
    assert server.list_runs()["runs"] == []


# ── transcript metadata: omp event parsing ────────────────────────────────


def test_metadata_title_prefers_task_section(scan_root: Path) -> None:
    """Lane prompts lead with boilerplate; the task section is the title."""
    prompt = (
        "# Lane instructions\nYou are the backend lane. Own API behavior.\n\n"
        "Lots of injected skill prose.\n\n"
        "# Task\nImplement the omp_viewer plugin exactly as the brief specifies\n"
        "with tests first.\n"
    )
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [_user_event(prompt)])
    meta = server.extract_run_metadata(path)
    assert meta["title"] == "Implement the omp_viewer plugin exactly as the brief specifies with tests first."


def test_metadata_title_falls_back_to_first_user_text(scan_root: Path) -> None:
    path = _make_session(
        scan_root, "p", "backend_dev", "s.jsonl",
        [_user_event("Review the cursor-agent recovery patch")],
    )
    assert server.extract_run_metadata(path)["title"] == "Review the cursor-agent recovery patch"


def test_metadata_title_event_wins_when_set(scan_root: Path) -> None:
    """OMP writes async title events; a non-empty one beats the prompt."""
    path = _make_session(
        scan_root, "p", "backend_dev", "s.jsonl",
        [
            {"type": "title", "v": 1, "title": "", "updatedAt": _ts(), "pad": " " * 100},
            _user_event("# Task\nTask text title"),
            {"type": "title", "v": 1, "title": "Repair quota fallback", "updatedAt": _ts()},
        ],
    )
    assert server.extract_run_metadata(path)["title"] == "Repair quota fallback"


def test_metadata_reads_model_change_and_session(scan_root: Path) -> None:
    path = _make_session(
        scan_root, "proj-x", "backend_dev", "s.jsonl",
        [
            _session_event("/srv/lane/proj-x"),
            _model_change("litellm/backend_dev"),
            _user_event("hello"),
        ],
    )
    meta = server.extract_run_metadata(path)
    assert meta["model"] == "litellm/backend_dev"
    assert meta["cwd"] == "/srv/lane/proj-x"


def test_metadata_tolerates_partial_and_garbage_prefix(scan_root: Path) -> None:
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    with open(path, "ab") as fh:
        fh.write(b"not json\n")
        fh.write(b'{"type": "message", broken\n')
        fh.write(json.dumps(_user_event("still works")).encode() + b"\n")
    meta = server.extract_run_metadata(path)
    assert meta["title"] == "still works"


def test_turn_end_error_and_tool_events_round_trip(scan_root: Path) -> None:
    """Wire-format events (turn_end text, error, toolCall/toolResult) are
    kept verbatim: the UI renders them, the server must not mangle them."""
    events = [
        _tool_call_event(),
        _tool_result_event(),
        _tool_result_event(text="permission denied", is_error=True),
        _turn_end_event("final report text"),
        _error_event("provider exploded", 500),
    ]
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", events)
    _offset, lines = server.read_from_offset(path, 0)
    assert lines == events


def test_noise_events_are_dropped(scan_root: Path) -> None:
    events = [
        _assistant_event("kept"),
        _noise_event(),
        _digest_noise_event(),
    ]
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", events)
    _offset, lines = server.read_from_offset(path, 0)
    assert [_assistant_text(ev) for ev in lines] == ["kept"]


# ── path resolution safety ────────────────────────────────────────────────


def test_resolve_transcript_accepts_a_valid_session_file(
    scan_root: Path, session_file: Path
) -> None:
    assert server.resolve_transcript(str(session_file)) == session_file.resolve()


def test_resolve_transcript_rejects_bad_shapes(
    scan_root: Path, session_file: Path
) -> None:
    bad = [
        "",                                          # empty
        "relative/path.jsonl",                       # not absolute
        str(session_file) + "/../" + session_file.name,  # ".." part
        str(scan_root / "proj-a" / ".hermes-lane" / "live.json"),  # not a .jsonl
        str(scan_root / "proj-a" / ".hermes-lane" / "loose.jsonl"),  # no lane dir
        str(session_file) + ".missing",              # wrong suffix
        str(scan_root / "proj-a" / ".hermes-lane" / "backend_dev" / "ghost.jsonl"),  # not a file
    ]
    for raw in bad:
        assert server.resolve_transcript(raw) is None, raw

def test_resolve_transcript_rejects_paths_outside_scan_roots(
    tmp_path: Path, monkeypatch
) -> None:
    outside = tmp_path / "outside"
    lane = outside / ".hermes-lane" / "backend_dev"
    lane.mkdir(parents=True)
    target = lane / "s.jsonl"
    _write_events(target, [_user_event("x")])
    monkeypatch.setattr(server, "SCAN_ROOTS", [str(tmp_path / "inside")])
    assert server.resolve_transcript(str(target)) is None


def test_resolve_transcript_rejects_symlink_escape(
    scan_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A symlink inside the scan root resolving outside it is rejected."""
    workdir = scan_root / "proj"
    monkeypatch.setattr(server, "SCAN_ROOTS", [str(workdir)])
    secret = tmp_path / "secret"
    lane = secret / ".hermes-lane" / "backend_dev"
    lane.mkdir(parents=True)
    target = lane / "s.jsonl"
    _write_events(target, [_user_event("secret")])
    link_lane = workdir / ".hermes-lane" / "backend_dev"
    link_lane.mkdir(parents=True)
    link = link_lane / "escape.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert server.resolve_transcript(str(link)) is None


# ── read_last_n_lines: tail_offset snapshot semantics ─────────────────────


def test_tail_offset_is_eof_for_a_newline_terminated_snapshot(session_file: Path) -> None:
    size = session_file.stat().st_size
    lines, _before, _has_more, tail_offset = server.read_last_n_lines(session_file, 200)
    assert tail_offset == size == session_file.stat().st_size
    assert [_assistant_text(ev) for ev in lines if ev["type"] == "message" and ev["message"]["role"] == "assistant"] == ["one"]


def test_tail_offset_backs_up_to_a_partial_tail_line(session_file: Path) -> None:
    complete_size = session_file.stat().st_size
    raw = json.dumps(_assistant_event("half-written")).encode("utf-8")
    with open(session_file, "ab") as fh:
        fh.write(raw[:20])  # no newline yet

    lines, _before, _has_more, tail_offset = server.read_last_n_lines(session_file, 200)
    assert tail_offset == complete_size
    assert tail_offset < session_file.stat().st_size

    with open(session_file, "ab") as fh:
        fh.write(raw[20:] + b"\n")
    new_offset, lines = server.read_from_offset(session_file, tail_offset)
    assert [_assistant_text(ev) for ev in lines] == ["half-written"]
    assert new_offset == session_file.stat().st_size


def test_tail_offset_spans_a_partial_line_across_chunk_boundaries(session_file: Path) -> None:
    complete_size = session_file.stat().st_size
    big = _assistant_event("x" * 200_000)
    raw = json.dumps(big).encode("utf-8")
    with open(session_file, "ab") as fh:
        fh.write(raw[:150_000])

    lines, _before, _has_more, tail_offset = server.read_last_n_lines(session_file, 200)
    assert tail_offset == complete_size

    with open(session_file, "ab") as fh:
        fh.write(raw[150_000:] + b"\n")
    _offset, lines = server.read_from_offset(session_file, tail_offset)
    assert [_assistant_text(ev) for ev in lines] == [big["message"]["content"][0]["text"]]


def test_tail_offset_ignores_trailing_noise_lines(scan_root: Path) -> None:
    path = _make_session(scan_root, "p", "backend_dev", "n.jsonl", [])
    size = _write_events(path, [_assistant_event("one"), _noise_event()])
    lines, _before, _has_more, tail_offset = server.read_last_n_lines(path, 200)
    assert [_assistant_text(ev) for ev in lines] == ["one"]
    assert tail_offset == size


def test_tail_offset_is_zero_for_an_empty_file(scan_root: Path) -> None:
    path = _make_session(scan_root, "p", "backend_dev", "empty.jsonl", [])
    lines, _before, has_more, tail_offset = server.read_last_n_lines(path, 200)
    assert lines == [] and has_more is False and tail_offset == 0


# ── HTTP surface ──────────────────────────────────────────────────────────


@pytest.fixture
def http(scan_root: Path, monkeypatch):
    """The real handler on an ephemeral port, rooted at the tmp scan root."""
    monkeypatch.setattr(server, "LOG_FILE", str(scan_root / "omp-viewer.log"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.OmpViewerHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(route: str) -> dict:
        return json.loads(urllib.request.urlopen(base + route).read())

    try:
        yield get, base
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_index_serves_the_omp_ui(http) -> None:
    get, base = http
    body = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert "<title>OMP viewer</title>" in body


def test_runs_endpoint_lists_discovered_sessions(http, session_file: Path) -> None:
    get, _base = http
    payload = get("/api/runs")
    files = [r["session_file"] for r in payload["runs"]]
    assert str(session_file) in files


def test_run_endpoint_rejects_traversal_and_foreign_paths(http) -> None:
    get, base = http
    for route in (
        "/api/run?path=../../etc/passwd",
        "/api/run?path=/etc/passwd",
        "/api/run?path=",
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + route)
        assert excinfo.value.code in (400, 404), route


def test_head_first_page_carries_the_snapshot_cursor(http, session_file: Path) -> None:
    get, _base = http
    size = session_file.stat().st_size
    page = get(f"/api/run?path={session_file}&lines=200&total=1")
    assert page["tail_offset"] == size
    assert len(page["lines"]) >= 1
    assert page["total_lines"] >= 1

    older = get(f"/api/run?path={session_file}&lines=1&before={page['before']}")
    assert "tail_offset" not in older


def test_poll_covers_events_appended_after_the_snapshot(http, session_file: Path) -> None:
    get, _base = http
    page = get(f"/api/run?path={session_file}&lines=200")
    cursor = page["tail_offset"]

    _write_events(session_file, [_assistant_event("gap A"), _assistant_event("gap B")])

    polled = get(f"/api/poll?path={session_file}&offset={cursor}")
    texts = [_assistant_text(ev) for ev in polled["lines"]]
    assert texts == ["gap A", "gap B"]
    assert polled["offset"] == session_file.stat().st_size

    again = get(f"/api/poll?path={session_file}&offset={polled['offset']}")
    assert again["lines"] == []


def test_poll_cursor_rereads_a_partial_line_completed_after_snapshot(
    http, session_file: Path
) -> None:
    get, _base = http
    raw = json.dumps(_assistant_event("completed later")).encode("utf-8")
    complete_size = session_file.stat().st_size
    with open(session_file, "ab") as fh:
        fh.write(raw[:10])

    page = get(f"/api/run?path={session_file}&lines=200")
    assert page["tail_offset"] == complete_size

    with open(session_file, "ab") as fh:
        fh.write(raw[10:20])
    mid = get(f"/api/poll?path={session_file}&offset={page['tail_offset']}")
    assert mid["lines"] == []

    with open(session_file, "ab") as fh:
        fh.write(raw[20:] + b"\n")
    done = get(f"/api/poll?path={session_file}&offset={page['tail_offset']}")
    assert [_assistant_text(ev) for ev in done["lines"]] == ["completed later"]


# ── read_from_offset: lossless forward tail pagination ────────────────────


def _drain_tail(path: Path) -> tuple[list[str], list[int]]:
    texts: list[str] = []
    offsets: list[int] = []
    offset = 0
    size = path.stat().st_size
    for _ in range(1000):
        new_offset, lines = server.read_from_offset(path, offset)
        texts.extend(_assistant_text(ev) for ev in lines)
        offsets.append(new_offset)
        if new_offset >= size:
            return texts, offsets
        assert new_offset > offset
        offset = new_offset
    raise AssertionError("tail pagination did not converge")


def test_capped_tail_pages_reconstruct_the_full_backlog_in_order(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    _write_events(path, [_assistant_event(f"event {i}") for i in range(30)])
    assert path.stat().st_size > 512

    texts, offsets = _drain_tail(path)
    assert texts == [f"event {i}" for i in range(30)]
    assert len(offsets) > 1


def test_capped_first_page_is_the_oldest_prefix_not_the_newest_suffix(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    events = [_assistant_event(f"event {i}") for i in range(30)]
    _write_events(path, events)

    offset, page1 = server.read_from_offset(path, 0)
    first_texts = [_assistant_text(ev) for ev in page1]
    assert first_texts == [f"event {i}" for i in range(len(page1))]
    assert first_texts[0] == "event 0"
    assert "event 29" not in first_texts

    end_of_last = sum(len(json.dumps(e).encode("utf-8")) + 1 for e in events[: len(page1)])
    assert offset == end_of_last
    assert offset < path.stat().st_size


def test_capped_tail_offsets_are_monotonic_and_stop_at_last_represented_event(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    events = [_assistant_event(f"event {i}") for i in range(30)]
    _write_events(path, events)

    offset = 0
    seen = 0
    prev = -1
    size = path.stat().st_size
    while offset < size:
        new_offset, lines = server.read_from_offset(path, offset)
        assert lines
        assert new_offset > prev
        prev = new_offset
        seen += len(lines)
        byte_end = sum(len(json.dumps(e).encode("utf-8")) + 1 for e in events[:seen])
        assert new_offset == byte_end
        if new_offset < size:
            _, following = server.read_from_offset(path, new_offset)
            assert _assistant_text(following[0]) == f"event {seen}"
        offset = new_offset
    assert seen == 30


def test_interleaved_noise_never_skips_kept_events_across_pages(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    events: list[dict] = []
    for i in range(20):
        events.append(_noise_event())
        events.append(_assistant_event(f"kept {i}"))
    events.extend([_digest_noise_event(), _noise_event()])
    _write_events(path, events)

    texts, offsets = _drain_tail(path)
    assert texts == [f"kept {i}" for i in range(20)]
    assert offsets[-1] == path.stat().st_size


def test_incomplete_trailing_line_is_withheld_then_delivered_after_completion(
    scan_root: Path,
) -> None:
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    _write_events(path, [_assistant_event("complete")])
    raw = json.dumps(_assistant_event("still writing")).encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(raw[:15])

    offset, lines = server.read_from_offset(path, 0)
    assert [_assistant_text(ev) for ev in lines] == ["complete"]
    assert offset < path.stat().st_size

    with open(path, "ab") as fh:
        fh.write(raw[15:25])
    mid_offset, mid_lines = server.read_from_offset(path, offset)
    assert mid_lines == []
    assert mid_offset == offset

    with open(path, "ab") as fh:
        fh.write(raw[25:] + b"\n")
    final_offset, final_lines = server.read_from_offset(path, offset)
    assert [_assistant_text(ev) for ev in final_lines] == ["still writing"]
    assert final_offset == path.stat().st_size


def test_oversized_single_event_is_delivered_and_advances_the_cursor(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    big = _assistant_event("x" * 2000)
    assert len(json.dumps({"lines": [big]}).encode("utf-8")) > 512
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    _write_events(path, [big, _assistant_event("after")])

    offset1, page1 = server.read_from_offset(path, 0)
    assert [_assistant_text(ev) for ev in page1] == ["x" * 2000]
    assert offset1 == len(json.dumps(big).encode("utf-8")) + 1
    assert offset1 < path.stat().st_size

    offset2, page2 = server.read_from_offset(path, offset1)
    assert [_assistant_text(ev) for ev in page2] == ["after"]
    assert offset2 == path.stat().st_size


# ── read_from_offset: bounded linear-time scanning ────────────────────────


class _ReadMeter:
    """Counts bytes the server reads from transcripts, via its open() global."""

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
    return [_assistant_event(f"event {i:02d} " + "y" * pad) for i in range(count)]


def test_capped_first_page_stops_reading_at_the_first_omitted_event(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    monkeypatch.setattr(server, "TAIL_READ_CHUNK_BYTES", 256, raising=False)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    events = _padded_events(30)
    _write_events(path, events)
    size = path.stat().st_size
    assert size > 4 * 512

    meter = _ReadMeter(monkeypatch)
    offset, page1 = server.read_from_offset(path, 0)
    assert page1
    assert len(page1) < len(events)

    omitted_end = sum(
        len(json.dumps(e).encode("utf-8")) + 1 for e in events[: len(page1) + 1]
    )
    assert meter.bytes_read <= omitted_end + server.TAIL_READ_CHUNK_BYTES
    assert meter.bytes_read < size // 2
    assert offset == omitted_end - (len(json.dumps(events[len(page1)]).encode("utf-8")) + 1)


def test_draining_all_pages_has_linear_total_read_volume(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    monkeypatch.setattr(server, "TAIL_READ_CHUNK_BYTES", 256, raising=False)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    events = _padded_events(30)
    _write_events(path, events)
    size = path.stat().st_size
    line_lens = [len(json.dumps(e).encode("utf-8")) + 1 for e in events]
    max_line = max(line_lens)

    meter = _ReadMeter(monkeypatch)
    texts: list[str] = []
    offset = 0
    pages = 0
    while offset < size:
        new_offset, lines = server.read_from_offset(path, offset)
        assert lines, "every page of an all-kept file returns events"
        assert new_offset > offset
        texts.extend(_assistant_text(ev) for ev in lines)
        offset = new_offset
        pages += 1
        assert pages < 100

    assert texts == [f"event {i:02d} " + "y" * 64 for i in range(30)]
    assert pages > 1

    bound = size + pages * (server.TAIL_READ_CHUNK_BYTES + max_line)
    assert meter.bytes_read <= bound
    assert meter.bytes_read < 2 * size


def test_tail_read_does_not_chase_bytes_appended_after_the_snapshot(
    scan_root: Path, monkeypatch
) -> None:
    import builtins

    real_open = builtins.open
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    size_before = _write_events(path, [_assistant_event("one"), _assistant_event("two")])

    appended = False

    def appending_open(file, mode="r", *args, **kwargs):
        nonlocal appended
        if isinstance(mode, str) and "rb" in mode and not appended:
            appended = True
            _write_events(path, [_assistant_event("late")])
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(server, "open", appending_open, raising=False)

    offset, lines = server.read_from_offset(path, 0)
    assert appended
    assert [_assistant_text(ev) for ev in lines] == ["one", "two"]
    assert offset == size_before

    offset2, lines2 = server.read_from_offset(path, offset)
    assert [_assistant_text(ev) for ev in lines2] == ["late"]
    assert offset2 == path.stat().st_size


def test_invalid_json_and_bad_utf8_lines_are_consumed_never_rendered(
    scan_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 512)
    path = _make_session(scan_root, "p", "backend_dev", "s.jsonl", [])
    _write_events(path, [_assistant_event("before")])
    with open(path, "ab") as fh:
        fh.write(b"this is not json\n")
        fh.write(b'{"type": "message", broken\n')
        fh.write(b"\xff\xfe undecodable\n")
        fh.write(b"\n")
    _write_events(path, [_assistant_event("after")])

    texts, offsets = _drain_tail(path)
    assert texts == ["before", "after"]
    assert offsets[-1] == path.stat().st_size


# ── scan-walk cache ───────────────────────────────────────────────────────


def _counted_listings(scan_root: Path, monkeypatch) -> dict:
    """Count directory listings under the scan root, by either walk style."""
    import os

    counts = {"iterdir": 0, "scandir": 0}
    real_iterdir = Path.iterdir
    real_scandir = os.scandir

    def counting_iterdir(self):
        counts["iterdir"] += 1
        return real_iterdir(self)

    def counting_scandir(path):
        if str(path).startswith(str(scan_root)):
            counts["scandir"] += 1
        return real_scandir(path)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    monkeypatch.setattr(os, "scandir", counting_scandir)
    return counts


def test_second_call_within_window_does_not_rewalk_the_scan_root(
    scan_root: Path, monkeypatch
) -> None:
    """A second find_lane_workdirs() inside the freshness window walks nothing."""
    _make_session(scan_root, "proj", "backend_dev", "a.jsonl", [_user_event("x")])
    counts = _counted_listings(scan_root, monkeypatch)

    first = server.find_lane_workdirs()
    after_first = dict(counts)
    second = server.find_lane_workdirs()

    assert second == first
    assert counts == after_first, "second call inside the freshness window rewalked the scan root"


def test_stale_scan_result_is_served_immediately_and_refreshed_in_background(
    scan_root: Path, monkeypatch
) -> None:
    """Past the window: serve the stale result now, rewalk in the background."""
    _make_session(scan_root, "first", "backend_dev", "a.jsonl", [])
    now = {"t": 1_000.0}
    monkeypatch.setattr(server, "_walk_now", lambda: now["t"])
    real_walk = server._walk_scan_roots
    walks = {"count": 0}
    refreshed = threading.Event()

    def counting_walk(roots):
        result = real_walk(roots)
        walks["count"] += 1
        return result

    monkeypatch.setattr(server, "_walk_scan_roots", counting_walk)

    # The walk returning is not publication: _refresh_walk hands the result
    # to _complete_walk, which stores it and releases waiters. Wait on the
    # publication seam so the refreshed result is observable before we
    # assert on it.
    real_complete = server._complete_walk

    def publishing_complete(state, result):
        real_complete(state, result)
        if walks["count"] >= 2:
            refreshed.set()

    monkeypatch.setattr(server, "_complete_walk", publishing_complete)

    first = server.find_lane_workdirs()
    assert walks["count"] == 1

    now["t"] += server.SCAN_WALK_CACHE_TTL_SEC + 1.0
    new_workdir = _make_session(scan_root, "second", "backend_dev", "b.jsonl", []).parents[2]

    stale = server.find_lane_workdirs()
    assert stale == first, "stale result must be served without waiting for the refresh walk"

    assert refreshed.wait(timeout=10), "no background refresh walk ran after the window"
    assert new_workdir in server.find_lane_workdirs()


def test_failed_refresh_thread_start_serves_stale_and_recovers(
    scan_root: Path, monkeypatch
) -> None:
    """A refresh thread that fails to start neither fails the request nor
    wedges the cache: the stale result is served, and a later request
    schedules the refresh again."""
    _make_session(scan_root, "first", "backend_dev", "a.jsonl", [])
    now = {"t": 1_000.0}
    monkeypatch.setattr(server, "_walk_now", lambda: now["t"])

    first = server.find_lane_workdirs()
    now["t"] += server.SCAN_WALK_CACHE_TTL_SEC + 1.0

    real_thread = threading.Thread

    class ExplodingThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", ExplodingThread)
    stale = server.find_lane_workdirs()
    assert stale == first
    monkeypatch.setattr(threading, "Thread", real_thread)

    refreshed = threading.Event()
    real_complete = server._complete_walk

    def publishing_complete(state, result):
        real_complete(state, result)
        if result is not None:
            refreshed.set()

    monkeypatch.setattr(server, "_complete_walk", publishing_complete)

    new_workdir = _make_session(scan_root, "second", "backend_dev", "b.jsonl", []).parents[2]
    assert server.find_lane_workdirs() == first, "stale result must be served while re-scheduling"
    assert refreshed.wait(timeout=10), "no background refresh ran after the failed start"
    assert new_workdir in server.find_lane_workdirs()


def test_cached_workdir_with_symlinked_lane_dir_is_dropped_inside_window(
    scan_root: Path, monkeypatch
) -> None:
    """A cached workdir whose .hermes-lane becomes a symlink inside the
    window is re-validated out of find_lane_workdirs() and list_runs()."""
    _make_session(scan_root, "proj", "backend_dev", "a.jsonl", [_user_event("x")])
    workdir = scan_root / "proj"
    counts = _counted_listings(scan_root, monkeypatch)

    first = server.find_lane_workdirs()
    assert workdir in first
    after_first = dict(counts)

    target = scan_root / "swapped-target"
    (target / "backend_dev").mkdir(parents=True)
    _write_events(target / "backend_dev" / "evil.jsonl", [_user_event("evil")])
    (workdir / server.LANE_DIR_NAME).rename(workdir / "lane-real")
    (workdir / server.LANE_DIR_NAME).symlink_to(target, target_is_directory=True)

    second = server.find_lane_workdirs()
    assert workdir not in second
    runs = server.list_runs()["runs"]
    assert all(run["workdir"] != str(workdir) for run in runs)
    assert not any(run["session_file"].endswith("evil.jsonl") for run in runs)
    assert counts == after_first, "re-validation rewalked the scan root"


def test_cached_workdir_deleted_inside_window_is_dropped(
    scan_root: Path,
) -> None:
    """A cached workdir deleted inside the window disappears from the next
    answer — the cache never resurrects it."""
    import shutil

    _make_session(scan_root, "proj", "backend_dev", "a.jsonl", [_user_event("x")])
    workdir = scan_root / "proj"
    assert workdir in server.find_lane_workdirs()

    shutil.rmtree(workdir)
    assert workdir not in server.find_lane_workdirs()


def test_workdir_revalidation_inside_window_does_not_rewalk_the_scan_root(
    scan_root: Path, monkeypatch
) -> None:
    """Re-validating a cached workdir is a stat pair, never a re-walk."""
    import shutil

    _make_session(scan_root, "proj", "backend_dev", "a.jsonl", [_user_event("x")])
    workdir = scan_root / "proj"
    counts = _counted_listings(scan_root, monkeypatch)

    server.find_lane_workdirs()
    shutil.rmtree(workdir)
    after_delete = dict(counts)

    assert workdir not in server.find_lane_workdirs()
    assert counts == after_delete, "re-validation rewalked the scan root"

def test_registry_workdir_appears_immediately_within_the_freshness_window(
    tmp_path: Path, monkeypatch
) -> None:
    """The registry half is read fresh on every call — never cache-delayed."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    scan_root = tmp_path / "scan"
    scan_root.mkdir()
    monkeypatch.setattr(server, "SCAN_ROOTS", [str(scan_root)])
    assert server.find_lane_workdirs() == []

    workdir = tmp_path / "elsewhere" / "reg-proj"
    (workdir / server.LANE_DIR_NAME).mkdir(parents=True)
    registry_dir = home / server.RUNS_DIR_NAME
    registry_dir.mkdir()
    (registry_dir / "run-1.json").write_text(
        json.dumps({"version": 1, "workdir": str(workdir)}), encoding="utf-8"
    )

    found = server.find_lane_workdirs()
    assert workdir.resolve() in found


def test_new_transcript_in_a_known_workdir_appears_on_the_next_call(
    scan_root: Path,
) -> None:
    """The per-workdir transcript listing is never behind the walk cache."""
    first_file = _make_session(scan_root, "proj", "backend_dev", "a.jsonl", [_user_event("one")])
    files = {r["session_file"] for r in server.list_runs()["runs"]}
    assert files == {str(first_file)}

    second_file = _make_session(scan_root, "proj", "backend_dev", "b.jsonl", [_user_event("two")])
    files = {r["session_file"] for r in server.list_runs()["runs"]}
    assert files == {str(first_file), str(second_file)}


def test_changing_scan_roots_never_serves_the_other_roots_result(
    tmp_path: Path, monkeypatch
) -> None:
    """The cache is keyed by the resolved roots, not by process lifetime."""
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    _make_session(root_a, "proj-a", "backend_dev", "a.jsonl", [])
    _make_session(root_b, "proj-b", "backend_dev", "b.jsonl", [])

    monkeypatch.setattr(server, "SCAN_ROOTS", [str(root_a)])
    first = server.find_lane_workdirs()
    assert [p.name for p in first] == ["proj-a"]

    monkeypatch.setattr(server, "SCAN_ROOTS", [str(root_b)])
    assert server.find_lane_workdirs() == [root_b.resolve() / "proj-b"]


def test_concurrent_callers_trigger_a_single_walk(scan_root: Path, monkeypatch) -> None:
    """N threads on a cold cache get one shared walk, not N competing ones."""
    for i in range(4):
        _make_session(scan_root, f"proj-{i}", "backend_dev", "a.jsonl", [])
    real_walk = server._walk_scan_roots
    walks = {"count": 0}
    entered = threading.Event()
    release = threading.Event()

    def gated_walk(roots):
        walks["count"] += 1
        entered.set()
        release.wait(timeout=10)
        return real_walk(roots)

    monkeypatch.setattr(server, "_walk_scan_roots", gated_walk)

    n_threads = 8
    barrier = threading.Barrier(n_threads + 1)
    results: list = []

    def worker() -> None:
        barrier.wait(timeout=10)
        results.append(server.find_lane_workdirs())

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    assert entered.wait(timeout=10), "no walk started"
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "a caller thread did not finish"

    assert walks["count"] == 1, f"expected one walk, got {walks['count']}"
    expected = server.find_lane_workdirs()
    assert len(results) == n_threads
    assert all(result == expected for result in results)


def _reference_walk(root: Path) -> list[Path]:
    """The pre-cache pathlib walk, kept here as the parity oracle."""
    found: dict[str, Path] = {}
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        try:
            lane = current / server.LANE_DIR_NAME
            has_lane = lane.is_dir() and not lane.is_symlink()
        except OSError:
            has_lane = False
        if has_lane:
            found.setdefault(str(current), current)
        if depth >= server.SCAN_MAX_DEPTH:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
            except OSError:
                continue
            if entry.name in server.SKIP_DIR_NAMES:
                continue
            stack.append((entry, depth + 1))
    return sorted(found.values())


def test_scan_walk_matches_the_pathlib_reference_walk(
    scan_root: Path, monkeypatch
) -> None:
    """The scandir walk returns exactly what the pathlib walk did, per case."""
    import os

    # Depth bound: level 3 counts, level 4 is out of scope.
    (scan_root / "depth1" / "depth2" / "depth3" / server.LANE_DIR_NAME).mkdir(parents=True)
    (scan_root / "depth1" / "depth2" / "depth3" / "depth4" / server.LANE_DIR_NAME).mkdir(
        parents=True
    )
    # Skipped names are never descended into.
    (scan_root / "node_modules" / "dep" / server.LANE_DIR_NAME).mkdir(parents=True)
    (scan_root / ".git" / "hooks" / server.LANE_DIR_NAME).mkdir(parents=True)
    # A symlinked directory is never followed (nor duplicated).
    (scan_root / "real-proj" / server.LANE_DIR_NAME).mkdir(parents=True)
    (scan_root / "linked-proj").symlink_to(
        scan_root / "real-proj", target_is_directory=True
    )
    # A symlinked .hermes-lane never counts.
    (scan_root / "symlinked-lane").mkdir()
    (scan_root / "symlinked-lane" / server.LANE_DIR_NAME).symlink_to(
        scan_root / "real-proj" / server.LANE_DIR_NAME, target_is_directory=True
    )
    # An unreadable directory is skipped (simulated: chmod is a no-op for root).
    unreadable = scan_root / "unreadable"
    (unreadable / server.LANE_DIR_NAME).mkdir(parents=True)
    real_scandir = os.scandir
    real_listdir = os.listdir

    def denying_scandir(path):
        if Path(path) == unreadable:
            raise PermissionError("denied")
        return real_scandir(path)

    def denying_listdir(path):
        if Path(path) == unreadable:
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", denying_scandir)
    monkeypatch.setattr(os, "listdir", denying_listdir)

    expected = {
        scan_root / "depth1" / "depth2" / "depth3",
        scan_root / "real-proj",
    }
    found = server.find_lane_workdirs()
    assert set(found) == expected
    assert found == _reference_walk(scan_root)
