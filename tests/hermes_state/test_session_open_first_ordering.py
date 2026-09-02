"""Opt-in open-before-closed ordering for ``list_sessions_rich``.

The Mission Control Sessions page asks for open conversations to be grouped
above ended ones. The grouping is a SQL-level concern: it must apply before
LIMIT/OFFSET (so it holds across pages), classify by the *surfaced* row (a
compression root's state is its live tip's, not the always-ended root's),
and keep the requested order inside each group. Callers that don't opt in
keep the existing ordering exactly.
"""

import time

import pytest

from hermes_state import SessionDB

try:
    from hermes_state import _COMPRESSION_TIP_MAX_STEPS
except ImportError:
    # Pre-fix builds named no constant (the bound was a local literal 100).
    # Falling back keeps this regression runnable against them — where it
    # reproduces the ordering failure the shared cap fixed.
    _COMPRESSION_TIP_MAX_STEPS = 100


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _seed(db, sid, started, ended=None, end_reason=None, parent=None,
          model_config=None):
    """Create a session with deterministic timestamps (SQL defaults aren't)."""
    db.create_session(
        sid, source="cli", parent_session_id=parent, model_config=model_config,
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = ?,"
        " message_count = 1 WHERE id = ?",
        (started, ended, end_reason, sid),
    )


def _interleaved(db):
    """Closed and open sessions alternating by start time.

    In the default newest-started-first order every closed row precedes an
    open one, so any grouping applied after pagination would show a closed
    row above an open one on page 1.
    """
    base = time.time() - 10_000
    rows = [
        ("c-newest", base + 100, base + 110, "done"),
        ("o-newest", base + 90, None, None),
        ("c-mid", base + 80, base + 85, "done"),
        ("o-mid", base + 70, None, None),
        ("c-old", base + 60, base + 65, "done"),
        ("o-old", base + 50, None, None),
    ]
    for sid, started, ended, reason in rows:
        _seed(db, sid, started, ended, reason)
    db._conn.commit()
    return rows


def _ids(rows):
    return [s["id"] for s in rows]


def _assert_open_before_closed(rows):
    """Invariant: no row with ``ended_at`` set precedes an open row."""
    closed_flags = [s["ended_at"] is not None for s in rows]
    assert closed_flags == sorted(closed_flags)


def test_open_first_groups_open_before_closed_across_pages(db):
    _interleaved(db)

    # Default is unchanged: pure newest-started-first, closed rows leading.
    assert _ids(db.list_sessions_rich(limit=10)) == [
        "c-newest", "o-newest", "c-mid", "o-mid", "c-old", "o-old",
    ]

    pages = [
        _ids(db.list_sessions_rich(limit=2, offset=off, open_first=True))
        for off in (0, 2, 4, 6)
    ]
    assert pages[0] == ["o-newest", "o-mid"]
    assert pages[1] == ["o-old", "c-newest"]
    assert pages[2] == ["c-mid", "c-old"]
    assert pages[3] == []
    # Intra-group order stays newest-started-first.
    assert _ids(db.list_sessions_rich(limit=10, open_first=True)) == [
        "o-newest", "o-mid", "o-old", "c-newest", "c-mid", "c-old",
    ]


def test_open_first_groups_open_before_closed_in_recent_mode(db):
    _interleaved(db)

    pages = [
        _ids(db.list_sessions_rich(
            limit=2, offset=off, order_by_last_active=True, open_first=True,
        ))
        for off in (0, 2, 4)
    ]
    assert pages[0] == ["o-newest", "o-mid"]
    assert pages[1] == ["o-old", "c-newest"]
    assert pages[2] == ["c-mid", "c-old"]
    # Intra-group order stays newest-active-first.
    assert _ids(db.list_sessions_rich(
        limit=10, order_by_last_active=True, open_first=True,
    )) == ["o-newest", "o-mid", "o-old", "c-newest", "c-mid", "c-old"]


def test_open_first_is_opt_in_default_ordering_unchanged(db):
    _interleaved(db)

    for kwargs in ({}, {"order_by_last_active": True}):
        rows = db.list_sessions_rich(limit=10, **kwargs)
        assert _ids(rows) == [
            "c-newest", "o-newest", "c-mid", "o-mid", "c-old", "o-old",
        ]


def _compression_pair(db, root_started, tip_started, tip_ended, tip_reason):
    """A compression root and its continuation tip, timestamps pinned."""
    db.create_session("root", source="cli")
    db.create_session("tip", source="cli", parent_session_id="root")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?,"
        " end_reason = 'compression', message_count = 1 WHERE id = 'root'",
        (root_started, root_started + 10),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?,"
        " end_reason = ?, message_count = 1 WHERE id = 'tip'",
        (tip_started, tip_ended, tip_reason),
    )
    db._conn.commit()


def test_open_first_classifies_compression_root_by_tip_state(db):
    """A compressed-and-continued conversation is open while its tip lives.

    The root row itself is always ended (``end_reason='compression'``);
    classifying the surfaced conversation by the root would bury a live
    chat among the closed rows.
    """
    base = time.time() - 5_000
    _seed(db, "plain-closed", base + 95, base + 99, "done")
    _compression_pair(
        db,
        root_started=base + 80,
        tip_started=base + 85,
        tip_ended=None,
        tip_reason=None,
    )
    _seed(db, "plain-open", base + 60)
    db._conn.commit()

    rows = db.list_sessions_rich(limit=10, open_first=True)
    # The projected tip surfaces (not the root) and groups with the open rows.
    assert _ids(rows) == ["tip", "plain-open", "plain-closed"]
    _assert_open_before_closed(rows)


def test_open_first_keeps_compression_root_closed_when_tip_ended(db):
    base = time.time() - 5_000
    _seed(db, "plain-open", base + 95)
    _compression_pair(
        db,
        root_started=base + 80,
        tip_started=base + 85,
        tip_ended=base + 90,
        tip_reason="done",
    )
    db._conn.commit()

    rows = db.list_sessions_rich(limit=10, open_first=True)
    assert _ids(rows) == ["plain-open", "tip"]
    _assert_open_before_closed(rows)


def test_open_first_classification_matches_surfaced_state_with_stale_sibling(db):
    """Classification must pick the same continuation the Python walk picks.

    A stale closed sibling beside the real continuation is the case a naive
    "any open chain member" classifier gets wrong in the other direction —
    here the preferred child decides, exactly like ``get_compression_tip``.
    """
    base = time.time() - 5_000
    db.create_session("root-s", source="cli")
    db.create_session("schain", source="cli", parent_session_id="root-s")
    db.create_session("sstale", source="cli", parent_session_id="root-s")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?,"
        " end_reason = 'compression', message_count = 1 WHERE id = 'root-s'",
        (base + 90, base + 95),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?,"
        " end_reason = 'ws_orphan_reap', message_count = 1 WHERE id = 'sstale'",
        (base + 96, base + 97),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, message_count = 1"
        " WHERE id = 'schain'",
        (base + 98,),
    )
    _seed(db, "plain-closed", base + 50, base + 55, "done")
    db._conn.commit()

    # Precondition: the surfaced row for the lineage is the open continuation.
    assert db.get_compression_tip("root-s") == "schain"

    rows = db.list_sessions_rich(limit=10, open_first=True)
    assert _ids(rows) == ["schain", "plain-closed"]
    _assert_open_before_closed(rows)


def test_open_first_pinned_backfill_stays_before_closed_rows(db):
    """A back-filled open pin must not land below a page of closed rows."""
    base = time.time() - 5_000
    _seed(db, "c3", base + 90, base + 95, "done")
    _seed(db, "c2", base + 80, base + 85, "done")
    _seed(db, "c1", base + 70, base + 75, "done")
    _seed(db, "pin", base + 10)
    db._conn.commit()
    db.set_session_pinned("pin", True)

    # Open-first order is [pin, c3, c2, c1]; page 2 (offset=2) is closed-only,
    # so the pinned open conversation only returns via the back-fill.
    rows = db.list_sessions_rich(
        limit=2, offset=2, open_first=True, include_pinned=True,
    )
    assert _ids(rows) == ["pin", "c2", "c1"]
    _assert_open_before_closed(rows)


def _deep_chain(db, root_id, base_started, *, edges):
    """A straight compression chain ``root -> n1 -> ... -> n<edges>``.

    Interior members are compression-ended (that is what lets the tip walk
    advance through them); the terminal member stays open. Returns the member
    ids in spine order.
    """
    ids = [f"{root_id}-n{i}" for i in range(1, edges + 1)]
    parent = root_id
    for i, sid in enumerate(ids, start=1):
        started = base_started + i
        if i < edges:
            _seed(db, sid, started, started + 1, "compression", parent=parent)
        else:
            _seed(db, sid, started, parent=parent)
        parent = sid
    return ids


def test_open_first_deep_chain_classifies_by_capped_tip_not_terminal(db):
    """Chains past the shared cap classify by the member the projection surfaces.

    ``get_compression_tip()`` advances at most ``_COMPRESSION_TIP_MAX_STEPS``
    times, so on a 101-edge chain the surfaced conversation is member #100
    while the live terminal (#101) sits one step beyond the bound. The SQL
    walk shares the cap: before it did, ordering classified the root by
    terminal #101 (open) while the projection surfaced the compression-ended
    #100 (closed) — the conversation spent an open-group slot on page 1, and
    page 2 then led with a genuinely open conversation below a closed row.
    """
    base = time.time() - 10_000
    _seed(db, "deep", base + 95, base + 96, "compression")
    chain = _deep_chain(
        db, "deep", base + 95, edges=_COMPRESSION_TIP_MAX_STEPS + 1,
    )
    assert len(chain) == _COMPRESSION_TIP_MAX_STEPS + 1
    _seed(db, "plain-open", base + 100)
    _seed(db, "plain-open-2", base + 90)
    db._conn.commit()

    # Precondition: the walk stops at member #100 (the cap), not the open
    # terminal #101 — #100 is the row the listing surfaces and classifies by.
    assert db.get_compression_tip("deep") == chain[_COMPRESSION_TIP_MAX_STEPS - 1]
    assert chain[_COMPRESSION_TIP_MAX_STEPS] == "deep-n101"

    rows = db.list_sessions_rich(limit=10, open_first=True)
    assert _ids(rows) == ["plain-open", "plain-open-2", "deep-n100"]
    _assert_open_before_closed(rows)

    # Page selection is where a SQL misclassification bites: the client-side
    # regroup after projection cannot recover a row the boundary stranded.
    pages = [
        db.list_sessions_rich(limit=2, offset=off, open_first=True)
        for off in (0, 2, 4)
    ]
    assert _ids(pages[0]) == ["plain-open", "plain-open-2"]
    assert _ids(pages[1]) == ["deep-n100"]
    assert pages[2] == []
    _assert_open_before_closed([s for page in pages for s in page])


def test_open_first_reset_root_classifies_by_own_tip_not_parents_spine(db):
    """A listable reset root classifies by its own tip, not the spine it left.

    Reset children can pass the plain continuation-marker test (no branch,
    delegate, or tool markers) yet still surface as their own conversation,
    so the SQL walk must seed them as chain heads. Seeded only as spine
    members, the non-preferred reset child below would never be walked at all
    and would classify by its raw always-ended row — its live continuation
    would take a closed-group slot ahead of newer closed rows, stranding an
    open conversation below a page boundary of closed rows.
    """
    base = time.time() - 5_000
    _seed(db, "spine-root", base + 90, base + 91, "compression")
    # The parent's preferred continuation (the later start wins the tie) —
    # the reset child is deliberately NOT on this spine.
    _seed(db, "spine-mid", base + 96, base + 97, "compression",
          parent="spine-root")
    _seed(db, "reset-root", base + 92, base + 93, "compression",
          parent="spine-root", model_config={"_reset_from": "spine-root"})
    _seed(db, "reset-tip", base + 97, parent="reset-root")
    _seed(db, "plain-open", base + 100)
    _seed(db, "plain-closed", base + 94, base + 95, "done")
    db._conn.commit()

    # Precondition: each surfaced root resolves its own conversation.
    assert db.get_compression_tip("spine-root") == "spine-mid"
    assert db.get_compression_tip("reset-root") == "reset-tip"

    rows = db.list_sessions_rich(limit=10, open_first=True)
    assert _ids(rows) == ["plain-open", "reset-tip", "plain-closed", "spine-mid"]
    _assert_open_before_closed(rows)

    pages = [
        db.list_sessions_rich(limit=2, offset=off, open_first=True)
        for off in (0, 2, 4)
    ]
    assert _ids(pages[0]) == ["plain-open", "reset-tip"]
    assert _ids(pages[1]) == ["plain-closed", "spine-mid"]
    assert pages[2] == []
    _assert_open_before_closed([s for page in pages for s in page])


def test_open_first_branch_root_classifies_by_own_tip(db):
    """A listable branch root keeps its own tip even after compressing.

    Branch children are excluded from every continuation walk by their
    ``_branched_from`` marker, so they surface as separate conversations and
    classify by their own tip's state — never by the parent lineage's tip,
    and never dropped from the walk's heads: a branch that later compresses
    still has a live continuation to surface and group with the open rows.
    """
    base = time.time() - 5_000
    _seed(db, "branch-base", base + 100, base + 101, "compression")
    _seed(db, "base-tip", base + 95, parent="branch-base")
    _seed(db, "branch-off", base + 90, base + 91, "compression",
          parent="branch-base", model_config={"_branched_from": "branch-base"})
    _seed(db, "branch-tip", base + 97, parent="branch-off")
    _seed(db, "plain-closed", base + 94, base + 95, "done")
    db._conn.commit()

    # Precondition: the branch and its parent each resolve their own tip.
    assert db.get_compression_tip("branch-base") == "base-tip"
    assert db.get_compression_tip("branch-off") == "branch-tip"

    rows = db.list_sessions_rich(limit=10, open_first=True)
    assert _ids(rows) == ["base-tip", "branch-tip", "plain-closed"]
    _assert_open_before_closed(rows)

    pages = [
        db.list_sessions_rich(limit=2, offset=off, open_first=True)
        for off in (0, 2)
    ]
    assert _ids(pages[0]) == ["base-tip", "branch-tip"]
    assert _ids(pages[1]) == ["plain-closed"]
    _assert_open_before_closed([s for page in pages for s in page])
