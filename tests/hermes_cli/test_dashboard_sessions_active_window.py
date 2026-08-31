"""Behavioral tests for the rolling activity window on ``GET /api/sessions``.

``active_within_hours`` is what backs the Sessions page's Overview: every
LOGICAL session with effective activity in the last N hours. Effective means
compression-chain projected (a fresh continuation keeps its old root
qualifying) and the cutoff is computed once on the server from one frozen
``time.time()``, so these tests pin the server clock and stamp rows relative
to it — including the exact-boundary case, which real clocks can't hit
deterministically.
"""

from types import SimpleNamespace

import pytest

from hermes_state import SessionDB

# Frozen "now" for the dashboard server. Rows are stamped relative to this so
# cutoff arithmetic (NOW - hours*3600) lands on exact, comparable floats.
NOW = 1_800_000_000.0
HOUR = 3_600.0
DAY = 24 * HOUR


@pytest.fixture()
def state_db(_isolate_hermes_home, monkeypatch):
    """Isolated state.db shared by the test and the dashboard app.

    Also freezes the router module's ``time`` so the server-side cutoff is
    ``NOW - hours*3600`` exactly — the boundary test depends on that.
    """
    import hermes_state
    from hermes_cli.web_routers import sessions as sessions_router
    from hermes_constants import get_hermes_home

    db_path = get_hermes_home() / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        sessions_router, "time", SimpleNamespace(time=lambda: NOW)
    )
    db = SessionDB(db_path=db_path)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(state_db):
    from starlette.testclient import TestClient

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _set_started_at(db, session_id, started_at):
    """Backdate a session row.

    Sessions without messages have no heartbeat either, so effective
    ``last_active`` falls back to ``started_at`` — enough to place a row
    precisely inside or outside the window.
    """
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?", (started_at, session_id)
        )
        db._conn.commit()


def _set_message_timestamps(db, session_id, timestamp):
    """Stamp every message of a session with an exact timestamp."""
    with db._lock:
        db._conn.execute(
            "UPDATE messages SET timestamp=? WHERE session_id=?",
            (timestamp, session_id),
        )
        db._conn.commit()


def _window(client, **params):
    """GET /api/sessions with the 24h window applied."""
    query = "&".join(
        f"{key}={value}" if value is not None else key for key, value in params.items()
    )
    response = client.get(f"/api/sessions?order=recent&active_within_hours=24&{query}")
    assert response.status_code == 200
    return response.json()


def test_window_keeps_fresh_row_and_drops_row_older_than_24h(client, state_db):
    state_db.create_session("fresh", "cli")
    state_db.create_session("stale", "cli")
    _set_started_at(state_db, "fresh", NOW - HOUR)
    _set_started_at(state_db, "stale", NOW - 3 * DAY)

    payload = _window(client, limit=20, offset=0)

    assert [s["id"] for s in payload["sessions"]] == ["fresh"]
    assert payload["total"] == 1


def test_window_includes_activity_exactly_at_the_cutoff(client, state_db):
    state_db.create_session("on-the-boundary", "cli")
    # Effective last_active == NOW - 24h == the cutoff the server computes.
    _set_started_at(state_db, "on-the-boundary", NOW - DAY)
    state_db.create_session("just_missed", "cli")
    _set_started_at(state_db, "just_missed", NOW - DAY - 1)

    payload = _window(client, limit=20, offset=0)

    assert [s["id"] for s in payload["sessions"]] == ["on-the-boundary"]
    assert payload["total"] == 1


def test_window_counts_continuation_activity_on_its_logical_root(client, state_db):
    # An old conversation that auto-compressed and was continued minutes ago:
    # the root's own timestamps are far outside the window, the tip's are not.
    state_db.create_session("old-root", "cli")
    _set_started_at(state_db, "old-root", NOW - 5 * DAY)
    state_db.end_session("old-root", "compression")
    state_db.create_session("live-tip", "cli", parent_session_id="old-root")
    state_db.append_message("live-tip", "user", "continuing the same conversation")
    _set_started_at(state_db, "live-tip", NOW - 2 * HOUR)
    _set_message_timestamps(state_db, "live-tip", NOW - 10 * 60)
    # Control: a session that really has been idle for days stays hidden.
    state_db.create_session("idle-root", "cli")
    _set_started_at(state_db, "idle-root", NOW - 4 * DAY)

    payload = _window(client, limit=20, offset=0)

    # One row for the logical conversation, projected forward to the tip.
    assert [s["id"] for s in payload["sessions"]] == ["live-tip"]
    assert payload["sessions"][0]["_lineage_root_id"] == "old-root"
    assert payload["total"] == 1


def test_window_total_and_pagination_agree(client, state_db):
    # 25 in-window conversations plus stragglers far outside it.
    for i in range(25):
        session_id = f"recent-{i:02d}"
        state_db.create_session(session_id, "cli")
        _set_started_at(state_db, session_id, NOW - (i + 1) * 60)
    for i in range(3):
        session_id = f"ancient-{i}"
        state_db.create_session(session_id, "cli")
        _set_started_at(state_db, session_id, NOW - (2 + i) * DAY)

    first = _window(client, limit=20, offset=0)
    second = _window(client, limit=20, offset=20)
    beyond = _window(client, limit=20, offset=40)

    assert first["total"] == second["total"] == beyond["total"] == 25
    assert len(first["sessions"]) == 20
    assert len(second["sessions"]) == 5
    assert beyond["sessions"] == []
    # Recency order is stable across pages: newest first, no row repeated.
    page_ids = [s["id"] for s in first["sessions"]]
    assert page_ids[0] == "recent-00"
    assert page_ids == sorted(page_ids, key=lambda sid: int(sid.split("-")[1]))
    assert len(set(page_ids) | {s["id"] for s in second["sessions"]}) == 25


def test_window_does_not_leak_old_pinned_sessions(client, state_db):
    state_db.create_session("pinned-days-ago", "cli")
    state_db.set_session_pinned("pinned-days-ago", True)
    _set_started_at(state_db, "pinned-days-ago", NOW - 30 * DAY)
    state_db.create_session("active-now", "cli")
    _set_started_at(state_db, "active-now", NOW - 5 * 60)

    windowed = _window(client, limit=20, offset=0)
    unwindowed = client.get("/api/sessions?order=recent&limit=20&offset=0")

    assert [s["id"] for s in windowed["sessions"]] == ["active-now"]
    assert windowed["total"] == 1
    # The pin still exists and still back-fills without the window — the
    # window is what excludes it, not the pin having silently vanished.
    assert unwindowed.status_code == 200
    unwindowed_ids = {s["id"] for s in unwindowed.json()["sessions"]}
    assert {"pinned-days-ago", "active-now"} <= unwindowed_ids


def test_window_composes_with_source_filter(client, state_db):
    state_db.create_session("fresh-cli", "cli")
    _set_started_at(state_db, "fresh-cli", NOW - HOUR)
    state_db.create_session("fresh-cron", "cron")
    _set_started_at(state_db, "fresh-cron", NOW - 2 * HOUR)
    state_db.create_session("old-cron", "cron")
    _set_started_at(state_db, "old-cron", NOW - 5 * DAY)

    cron_only = _window(client, limit=20, offset=0, source="cron")

    assert [s["id"] for s in cron_only["sessions"]] == ["fresh-cron"]
    assert cron_only["total"] == 1


def test_window_rejects_out_of_range_values(client):
    for bad_value in ("0", "-3", "abc", "10000"):
        response = client.get(f"/api/sessions?active_within_hours={bad_value}")
        assert response.status_code == 422, bad_value


def test_omitting_the_window_keeps_unfiltered_behavior(client, state_db):
    """Callers that don't ask for a window must get the old listing back."""
    state_db.create_session("fresh", "cli")
    _set_started_at(state_db, "fresh", NOW - HOUR)
    state_db.create_session("stale", "cli")
    _set_started_at(state_db, "stale", NOW - 9 * DAY)

    response = client.get("/api/sessions?order=recent&limit=20&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert {s["id"] for s in payload["sessions"]} == {"fresh", "stale"}
    assert payload["total"] == 2


def _set_last_activity(state_db, session_id, active_at):
    """Pin a session's effective activity exactly (heartbeat path)."""
    with state_db._lock:
        state_db._conn.execute(
            "UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?",
            (active_at, active_at, session_id),
        )
        state_db._conn.commit()


def test_window_excludes_hidden_sessions_from_rows_total_and_pages(client, state_db):
    """Hidden in-window sessions must not affect rows, total, empty state, or pages.

    The list excludes hidden rows, so the paired count must too — otherwise
    ``total`` describes rows the endpoint can never return and the empty
    state/pagination lie about the window's contents.
    """
    for i in range(22):
        session_id = f"visible-{i:02d}"
        state_db.create_session(session_id, "cli")
        _set_last_activity(state_db, session_id, NOW - (i + 1) * 60)
    for i in range(3):
        session_id = f"hidden-{i}"
        state_db.create_session(session_id, "cli")
        _set_last_activity(state_db, session_id, NOW - (30 + i) * 60)
        state_db.set_session_hidden(session_id, True)

    page_one = _window(client, limit=20, offset=0)
    page_two = _window(client, limit=20, offset=20)

    # Rows: hidden conversations never surface on any page...
    listed = [s["id"] for s in page_one["sessions"] + page_two["sessions"]]
    assert len(page_one["sessions"]) == 20
    assert len(page_two["sessions"]) == 2
    assert not any(sid.startswith("hidden-") for sid in listed)
    # ...and total: agrees with the rows (25 would mean hidden rows count).
    assert page_one["total"] == page_two["total"] == 22

    # Empty state: hide EVERYTHING in-window and the endpoint reports a
    # genuinely empty window — zero rows AND zero total, never a phantom
    # "total > 0 with nothing to show" stranding.
    for i in range(22):
        state_db.set_session_hidden(f"visible-{i:02d}", True)
    emptied = _window(client, limit=20, offset=0)
    assert emptied["sessions"] == []
    assert emptied["total"] == 0


def test_window_upper_bounds_activity_at_server_now(client, state_db):
    """The window is the inclusive range ``[now - 24h, now]``.

    Future-dated activity must NOT be admitted forever: a skewed or hostile
    client clock planting "active tomorrow" rows would otherwise pin them to
    the top of the Overview until real time catches up.
    """
    cases = {
        # exactly at the server-computed cutoff: in (pinned separately above)
        "at_cutoff": NOW - DAY,
        "in_window": NOW - HOUR,
        "just_before_now": NOW - 1,
        # exactly at server now: still in — the bound is inclusive
        "at_now": NOW,
        # anything after server now: out, no matter how far
        "just_after_now": NOW + 1,
        "far_future": NOW + 7 * DAY,
    }
    for session_id, active_at in cases.items():
        state_db.create_session(session_id, "cli")
        _set_last_activity(state_db, session_id, active_at)

    payload = _window(client, limit=20, offset=0)

    assert [s["id"] for s in payload["sessions"]] == [
        "at_now", "just_before_now", "in_window", "at_cutoff",
    ]
    assert payload["total"] == 4


def test_window_uses_one_frozen_now_for_bounds_and_is_active(client, state_db, monkeypatch):
    """A windowed request must sample ``time.time()`` once and reuse that value.

    The single frozen ``now`` must drive both inclusive list/count bounds and
    the per-row ``is_active`` flag. Callers that omit the window keep the
    historical behavior of sampling a fresh ``now`` for ``is_active``.
    """
    from hermes_cli.web_routers import sessions as sessions_router

    state_db.create_session("live", "cli")
    state_db.create_session("stale", "cli")
    _set_last_activity(state_db, "live", NOW - 60)
    _set_last_activity(state_db, "stale", NOW - 400)

    calls = []

    def moving_time():
        # Each call advances by a full window width so a second read would
        # produce visibly different bounds and ``is_active`` results.
        idx = len(calls)
        calls.append(idx)
        return NOW + idx * 24 * HOUR

    monkeypatch.setattr(sessions_router, "time", SimpleNamespace(time=moving_time))

    payload = _window(client, limit=20, offset=0)

    # Exactly one server ``now`` per windowed request.
    assert len(calls) == 1
    # The same frozen value was forwarded to both the row query and the count,
    # so rows and total agree on one page.
    assert payload["total"] == len(payload["sessions"]) == 2
    by_id = {s["id"]: s for s in payload["sessions"]}
    # ``is_active`` is computed from the frozen NOW, not from a later read.
    assert by_id["live"]["is_active"] is True
    assert by_id["stale"]["is_active"] is False

    # Non-windowed callers still obtain a fresh ``now`` normally.
    calls.clear()
    response = client.get("/api/sessions?order=recent&limit=20&offset=0")
    assert response.status_code == 200
    assert len(calls) == 1


def test_window_rows_and_total_read_one_snapshot(client, state_db, monkeypatch):
    """An interleaved commit between row selection and count must not inflate total.

    The snapshot must cover both the row query and the count.  If the router
    reverts to two independent reads, the count will see the row committed
    after rows were already selected and ``total`` will disagree.
    """
    import sqlite3
    import threading

    from hermes_state import SessionDB

    state_db.create_session("first", "cli")
    _set_started_at(state_db, "first", NOW - HOUR)

    db_path = state_db.db_path
    barrier = threading.Barrier(2, timeout=10)
    original_list = SessionDB.list_sessions_rich
    original_count = SessionDB.session_count

    def _pausing_list(self, *args, **kwargs):
        result = original_list(self, *args, **kwargs)
        barrier.wait()
        return result

    def _pausing_count(self, *args, **kwargs):
        barrier.wait()
        return original_count(self, *args, **kwargs)

    monkeypatch.setattr(SessionDB, "list_sessions_rich", _pausing_list)
    monkeypatch.setattr(SessionDB, "session_count", _pausing_count)

    def _commit_second():
        barrier.wait()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, model_config) VALUES (?, ?, ?, ?)",
                ("second", "cli", NOW - HOUR, None),
            )
            conn.commit()
        finally:
            conn.close()
        barrier.wait()

    t = threading.Thread(target=_commit_second)
    t.start()
    try:
        payload = _window(client, limit=20, offset=0)
    finally:
        t.join()

    # The read snapshot was established during row selection, before the
    # interleaved commit, so rows and total agree on the pre-commit state.
    assert [s["id"] for s in payload["sessions"]] == ["first"]
    assert payload["total"] == 1


def test_window_count_stays_in_snapshot_after_activity_update(client, state_db, monkeypatch):
    """An interleaved activity update between rows and count must not drop total.

    All enrichment/count reads must stay inside the same snapshot as the row
    query, not re-read a newer database state.
    """
    import sqlite3
    import threading

    from hermes_state import SessionDB

    state_db.create_session("kept", "cli")
    _set_last_activity(state_db, "kept", NOW - HOUR)

    db_path = state_db.db_path
    barrier = threading.Barrier(2, timeout=10)
    original_list = SessionDB.list_sessions_rich
    original_count = SessionDB.session_count

    def _pausing_list(self, *args, **kwargs):
        result = original_list(self, *args, **kwargs)
        barrier.wait()
        return result

    def _pausing_count(self, *args, **kwargs):
        barrier.wait()
        return original_count(self, *args, **kwargs)

    monkeypatch.setattr(SessionDB, "list_sessions_rich", _pausing_list)
    monkeypatch.setattr(SessionDB, "session_count", _pausing_count)

    def _age_out():
        barrier.wait()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?",
                (NOW - 2 * DAY, NOW - 2 * DAY, "kept"),
            )
            conn.commit()
        finally:
            conn.close()
        barrier.wait()

    t = threading.Thread(target=_age_out)
    t.start()
    try:
        payload = _window(client, limit=20, offset=0)
    finally:
        t.join()

    assert [s["id"] for s in payload["sessions"]] == ["kept"]
    assert payload["total"] == 1


def test_window_order_created_vs_recent(client, state_db):
    """Windowed ``order`` must sort by the requested criterion.

    ``older-created-fresher`` was created first but has newer activity;
    ``newer-created`` was created later but has older activity.  The two
    orderings must therefore return opposite ID sequences while the total
    stays the same atomic snapshot.
    """
    state_db.create_session("older-created-fresher", "cli")
    _set_last_activity(state_db, "older-created-fresher", NOW - HOUR)
    _set_started_at(state_db, "older-created-fresher", NOW - 2 * DAY)

    state_db.create_session("newer-created", "cli")
    _set_last_activity(state_db, "newer-created", NOW - 5 * HOUR)
    _set_started_at(state_db, "newer-created", NOW - 1 * DAY)

    recent = client.get(
        "/api/sessions?order=recent&active_within_hours=24&limit=20&offset=0"
    )
    assert recent.status_code == 200
    recent_payload = recent.json()
    assert [s["id"] for s in recent_payload["sessions"]] == [
        "older-created-fresher",
        "newer-created",
    ]
    assert recent_payload["total"] == 2

    created = client.get(
        "/api/sessions?order=created&active_within_hours=24&limit=20&offset=0"
    )
    assert created.status_code == 200
    created_payload = created.json()
    assert [s["id"] for s in created_payload["sessions"]] == [
        "newer-created",
        "older-created-fresher",
    ]
    assert created_payload["total"] == 2
