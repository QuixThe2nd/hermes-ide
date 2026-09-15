#!/usr/bin/env python3
"""The snapshot/delta seam of the feed, deterministically.

last_id is a durable high-water cursor captured BEFORE the poll's rows
are read, so a message committed in between — after the cursor capture,
before the row query — is inside that same response (its id lifts the
cursor past it) and is replayed by no later poll: every event appears
exactly once, none is lost to the seam. The seam is driven here not by
timing luck but by a SQL constant whose .format() — the exact call
load_feed makes between the cursor capture and the row query — commits
a message through a second connection first. A timestamp or a mutable
activity field could never prove this: only the row-id cursor can.

The delta path is held to the same rule, the cursor never regresses,
and a row written between two polls the ordinary way is still replayed
by exactly one later poll.
"""

import importlib.util
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

_MODULE_SEQ = itertools.count()

SID = "20260903_110000_feed00001"


class BarrierSQL(str):
    """A SQL constant that commits a message row at the exact moment
    .format() runs. load_feed formats FEED_LAST_ID_CHAIN_SQL first (the
    cursor capture), then one of the row queries — so patching a row
    query's constant with this lands the commit strictly between the
    two, without sleeping, spinning or luck."""

    def __new__(cls, sql, db, sid, text):
        obj = super().__new__(cls, sql)
        obj._db = db
        obj._sid = sid
        obj._text = text
        obj.fired = False
        return obj

    def format(self, **kwargs):
        if not self.fired:
            self.fired = True
            con = sqlite3.connect(self._db)
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?,'user',?,?)",
                (self._sid, self._text, time.time()))
            con.commit()
            con.close()
        return str.format(self, **kwargs)


def load_server(tmp, main_db):
    spec = importlib.util.spec_from_file_location(
        "mc_server_barrier_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class FeedBarrierCase(unittest.TestCase):
    """One session behind a real HTTP feed endpoint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-barrier-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, message_count) VALUES"
            " (?,'cli','barrier fixture',?, ?, 2)", (SID, now - 60, now))
        for i, text in enumerate(("first message", "second message")):
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?,'user',?,?)",
                (SID, text, now - 60 + i))
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        # The feed must stay off the network and off subprocesses: the
        # clarify bridge is keyed off (no key configured upstream) and
        # Discord surfaces are pinned to empty.
        for patcher in (unittest.mock.patch.object(
                self.mod, "clarify_api_key", return_value=""),
                unittest.mock.patch.object(
                    self.mod, "load_discord_token", return_value=""),
                unittest.mock.patch.object(
                    self.mod, "fetch_active_thread_ids",
                    return_value=([], None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----------------------------------------------------

    def feed(self, after=0):
        url = "http://127.0.0.1:%d/s/default/%s/feed?after=%d" % (
            self.port, SID, after)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise AssertionError("feed poll failed: %d %s" % (
                exc.code, exc.read().decode("utf-8", "replace")))
        obj = json.loads(body)
        self.assertTrue(obj["ok"])
        return obj

    @staticmethod
    def texts(poll):
        return [m.get("text", "") for m in poll["messages"]]

    def row_id(self, text):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT id FROM messages WHERE session_id = ? AND"
                " content = ?", (SID, text)).fetchone()[0]
        finally:
            con.close()

    def row_count(self, text):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT count(*) FROM messages WHERE content = ?",
                (text,)).fetchone()[0]
        finally:
            con.close()

    def patch_barrier(self, attribute, text):
        """Point one row-query SQL constant at the barrier seam."""
        original = getattr(self.mod, attribute)
        barrier = BarrierSQL(original, self.db, SID, text)
        setattr(self.mod, attribute, barrier)
        self.addCleanup(setattr, self.mod, attribute, original)
        return barrier


class TestSnapshotSeam(FeedBarrierCase):
    """A row committed after the cursor capture, before the snapshot
    rows are read, lands inside that snapshot — exactly once."""

    def test_barrier_row_is_in_the_snapshot_exactly_once(self):
        barrier = self.patch_barrier("CHAT_PAGE_CHAIN_SQL",
                                     "BARRIER-snapshot-row")
        poll = self.feed(0)
        self.assertTrue(barrier.fired, "seam never ran")
        self.assertEqual(self.texts(poll).count("BARRIER-snapshot-row"), 1)
        # The cursor was lifted past it: it is part of the snapshot's
        # own high-water mark, not something a later poll must rescue.
        self.assertGreaterEqual(poll["last_id"],
                                self.row_id("BARRIER-snapshot-row"))
        # The next delta after that cursor replays nothing...
        nxt = self.feed(poll["last_id"])
        self.assertEqual(
            [t for t in self.texts(nxt) if t], [])
        # ...and the cursor holds still rather than regressing.
        self.assertEqual(nxt["last_id"], poll["last_id"])
        # A cold rebuild of the same conversation sees it once too.
        self.assertEqual(self.texts(self.feed(0)).count(
            "BARRIER-snapshot-row"), 1)
        # Exactly one barrier row was ever committed.
        self.assertEqual(self.row_count("BARRIER-snapshot-row"), 1)

    def test_snapshot_still_carries_the_rows_that_predate_it(self):
        self.patch_barrier("CHAT_PAGE_CHAIN_SQL", "BARRIER-with-company")
        texts = self.texts(self.feed(0))
        for text in ("first message", "second message",
                     "BARRIER-with-company"):
            self.assertEqual(texts.count(text), 1)


class TestDeltaSeam(FeedBarrierCase):
    """The same seam on a catch-up poll: a row committed after the
    cursor capture, before the delta rows are read, arrives in that
    delta — exactly once."""

    def test_barrier_row_is_in_the_delta_exactly_once(self):
        first = self.feed(0)
        self.assertEqual(len(first["messages"]), 2)
        barrier = self.patch_barrier("FEED_AFTER_CHAIN_SQL",
                                     "BARRIER-delta-row")
        delta = self.feed(first["last_id"])
        self.assertTrue(barrier.fired, "seam never ran")
        self.assertEqual(self.texts(delta).count("BARRIER-delta-row"), 1)
        self.assertGreaterEqual(delta["last_id"],
                                self.row_id("BARRIER-delta-row"))
        self.assertGreater(delta["last_id"], first["last_id"])
        # Nothing after the new cursor: not lost, not duplicated.
        nxt = self.feed(delta["last_id"])
        self.assertEqual([t for t in self.texts(nxt) if t], [])
        self.assertEqual(nxt["last_id"], delta["last_id"])
        self.assertEqual(self.row_count("BARRIER-delta-row"), 1)

    def test_delta_seam_row_is_not_replayed_by_a_later_snapshot(self):
        first = self.feed(0)
        self.patch_barrier("FEED_AFTER_CHAIN_SQL", "BARRIER-once-only")
        delta = self.feed(first["last_id"])
        self.assertEqual(self.texts(delta).count("BARRIER-once-only"), 1)
        # The full snapshot the page would rebuild from shows the row
        # once — the same single event, not a second one.
        self.assertEqual(self.texts(self.feed(0)).count(
            "BARRIER-once-only"), 1)
        self.assertEqual(self.row_count("BARRIER-once-only"), 1)


class TestCursorDiscipline(FeedBarrierCase):
    """The cursor is monotonic, and ordinary between-poll writes are
    replayed by exactly one later poll."""

    def test_cursor_never_regresses_across_a_mixed_sequence(self):
        barrier = self.patch_barrier("CHAT_PAGE_CHAIN_SQL",
                                     "BARRIER-monotonic")
        polls = [self.feed(0)]
        # A row written between polls the ordinary way: the next delta
        # carries it once.
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,'user','written between polls',?)",
            (SID, time.time()))
        con.commit()
        con.close()
        polls.append(self.feed(polls[-1]["last_id"]))
        polls.append(self.feed(polls[-1]["last_id"]))
        cursors = [p["last_id"] for p in polls]
        for older, newer in zip(cursors, cursors[1:]):
            self.assertGreaterEqual(newer, older)
        every = [t for p in polls for t in self.texts(p)]
        self.assertEqual(every.count("BARRIER-monotonic"), 1)
        self.assertEqual(every.count("written between polls"), 1)
        self.assertEqual(every.count("first message"), 1)
        self.assertTrue(barrier.fired)


if __name__ == "__main__":
    unittest.main()
