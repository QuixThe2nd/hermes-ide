#!/usr/bin/env python3
"""Core-owned session projection and global open-first order, across
multiple databases, through the public HTTP surfaces.

The listing is served by the core projection
(SessionDB.list_sessions_rich with open_first): compression tips,
pinned backfill, branch/reset visibility and hidden/delegate filtering
are core's to decide, and this file holds the server to it. Rows merge
across the main and profile DBs with one deterministic global key —
every open conversation before every closed one, canonical
last-active order inside each partition, stable (profile, session id)
tie-breakers — and the 24h window bounds only conversations that have
come to rest. Chain behavior is exercised end to end at 11, 51 and 101
edges (the shared tip-walk cap): transcript, search, feed cursor and
archive all act on the canonical chain, never one segment.
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


def load_server(tmp, main_db, profile_glob):
    """One isolated server.py module per test class instance."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_proj_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = profile_glob
    return mod


class ProjectionCase(unittest.TestCase):
    """A main DB plus one profile DB behind a real HTTP server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-proj-")
        self.home = os.path.join(self.tmp, "home")
        self.main_db = os.path.join(self.home, "state.db")
        self.work_db = os.path.join(self.home, "profiles", "work",
                                    "state.db")
        for path in (self.main_db, self.work_db):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con = sqlite3.connect(path)
            con.executescript(SESSION_SCHEMA)
            con.commit()
            con.close()
        self.mod = load_server(
            self.tmp, self.main_db,
            os.path.join(self.home, "profiles", "*", "state.db"))
        # Discord stays off the network; no launches happen here.
        for patcher in (unittest.mock.patch.object(
                self.mod, "load_discord_token", return_value=""),
                unittest.mock.patch.object(
                    self.mod, "fetch_active_thread_ids",
                    return_value=([], None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = time.time()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- seeding ---------------------------------------------------

    def seed(self, db, sid, *, started, ended=None, reason=None,
             parent=None, archived=0, pinned=0, source="cli",
             title=None, reset_from=None, branched_from=None,
             delegate=None, model_config=None):
        """Insert one session row with pinned timestamps."""
        cfg = dict(model_config or {})
        if reset_from is not None:
            cfg["_reset_from"] = reset_from
        if branched_from is not None:
            cfg["_branched_from"] = branched_from
        if delegate is not None:
            cfg["_delegate_from"] = delegate
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " ended_at, end_reason, parent_session_id, archived, pinned,"
            " message_count, last_activity_at, model_config)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, source, title or sid, started, ended, reason, parent,
             archived, pinned, 1,
             ended if ended is not None else started + 5,
             json.dumps(cfg) if cfg else None))
        con.commit()
        con.close()

    def message(self, db, sid, text, ts=None, role="user"):
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)", (sid, role, text, ts or time.time()))
        con.commit()
        con.close()

    def chain(self, db, root, base, edges, *, tip_ended=None,
              tip_reason=None):
        """root -> n1 -> ... -> n<edges>, interior members
        compression-ended. Returns the member ids in spine order."""
        ids = ["%s-n%d" % (root, i) for i in range(1, edges + 1)]
        parent = root
        for i, sid in enumerate(ids, start=1):
            started = base + i
            if i < edges:
                self.seed(db, sid, started=started,
                          ended=started + 0.5, reason="compression",
                          parent=parent)
            else:
                self.seed(db, sid, started=started, ended=tip_ended,
                          reason=tip_reason, parent=parent)
            parent = sid
        return ids

    # ---- HTTP helpers ----------------------------------------------

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def rows_in(self, body):
        """Leading token of every conversation row's data-q blob in a
        rendered page, lowercased (the blob is lowercased at render)."""
        return [m.lower() for m in re.findall(
            r'<article class="conv[^>]*data-q="([A-Za-z0-9_.-]+)[ "]',
            body)]

    def inbox_rows(self, path="/"):
        _status, body = self.get(path)
        return self.rows_in(body)

    def feed(self, sid, after=0, profile="default"):
        status, body = self.get(
            "/s/%s/%s/feed?after=%d" % (profile, sid, after))
        self.assertEqual(status, 200)
        return json.loads(body)

    def transcript_texts(self, page):
        """Texts inside the transcript <ol class="msgs"> only."""
        main = re.search(r'<ol class="msgs">(.*?)</ol>', page, re.S)
        self.assertIsNotNone(main)
        return re.findall(r'<p class="text">(.*?)</p>', main.group(1))

    def archived_flags(self, db, *sids):
        con = sqlite3.connect(db)
        try:
            return {sid: con.execute(
                "SELECT archived FROM sessions WHERE id = ?",
                (sid,)).fetchone()[0] for sid in sids}
        finally:
            con.close()


class TestGlobalOrderAcrossDBs(ProjectionCase):
    """One deterministic global key over every discovered DB."""

    def test_open_before_closed_with_disagreeing_timestamps(self):
        # The profile's archived row is the NEWEST conversation
        # overall; it still renders after every open row. Within the
        # open partition the fixed section order (Active, then
        # Open-completed, then Open-unfinished) is preserved
        # presentation on top of the global order.
        base = self.now - 1000
        self.seed(self.main_db, "d-open-answered", started=base + 90)
        self.message(self.main_db, "d-open-answered", "q")
        self.message(self.main_db, "d-open-answered", "a",
                     role="assistant")
        self.seed(self.main_db, "d-open-unfinished", started=base + 10)
        self.seed(self.main_db, "d-closed", started=base + 50,
                  ended=base + 55, reason="done", archived=1)
        self.seed(self.work_db, "w-closed-newest", started=base + 100,
                  ended=base + 105, reason="done", archived=1)
        order = self.inbox_rows()
        # Closed partition last, in canonical last-active order
        # (newest closed row first inside the partition).
        self.assertEqual(order[-2:], ["w-closed-newest", "d-closed"])
        # Every open row before every closed one, whatever the
        # timestamps say — and the completed open row keeps its
        # section slot ahead of the unfinished one.
        self.assertEqual(order[:2],
                         ["d-open-answered", "d-open-unfinished"])

    def test_canonical_last_active_order_within_partitions(self):
        base = self.now - 1000
        # last_activity descends against started order.
        self.seed(self.main_db, "d1", started=base + 10)
        self.seed(self.main_db, "d2", started=base + 20)
        self.seed(self.work_db, "w1", started=base + 30)
        con = sqlite3.connect(self.main_db)
        con.execute("UPDATE sessions SET last_activity_at = ?"
                    " WHERE id = 'd1'", (base + 200,))
        con.execute("UPDATE sessions SET last_activity_at = ?"
                    " WHERE id = 'd2'", (base + 100,))
        con.commit()
        con.close()
        con = sqlite3.connect(self.work_db)
        con.execute("UPDATE sessions SET last_activity_at = ?"
                    " WHERE id = 'w1'", (base + 150,))
        con.commit()
        con.close()
        self.assertEqual(self.inbox_rows(), ["d1", "w1", "d2"])

    def test_duplicate_ids_across_profiles_both_survive(self):
        base = self.now - 1000
        self.seed(self.main_db, "20260902_150000_dupl0000",
                  started=base + 50, title="default copy")
        self.seed(self.work_db, "20260902_150000_dupl0000",
                  started=base + 60, title="work copy")
        self.assertIn("20260902_150000_dupl0000", self.inbox_rows())
        # Both pages answer for their own profile's copy.
        for profile, title in (("default", "default copy"),
                               ("work", "work copy")):
            _status, page = self.get(
                "/s/%s/20260902_150000_dupl0000" % profile)
            self.assertIn(title, page)

    def test_identical_timestamps_order_is_stable(self):
        base = self.now - 1000
        stamp = base + 42
        for db, sids in ((self.main_db, ("b-second", "a-first")),
                         (self.work_db, ("c-third", "z-fourth"))):
            for sid in sids:
                con = sqlite3.connect(db)
                con.execute(
                    "INSERT INTO sessions (id, source, title,"
                    " started_at, last_activity_at, message_count)"
                    " VALUES (?,'cli',?,?,?,1)",
                    (sid, sid, stamp, stamp))
                con.commit()
                con.close()
        want = self.inbox_rows()
        self.assertEqual(want, ["a-first", "b-second",
                                "c-third", "z-fourth"])
        for _ in range(3):
            self.assertEqual(self.inbox_rows(), want)


class TestWindowAndPins(ProjectionCase):
    """The 24h window bounds only conversations at rest."""

    def seed_window(self):
        day = 24 * 3600
        base = self.now - day
        # Rested rows around the seam: inside, just outside, far
        # outside. "Rested" = projected tip ended or archived; the
        # window bounds only those.
        self.seed(self.main_db, "rested_inside", started=base + 600,
                  ended=base + 700, reason="done")
        self.seed(self.main_db, "rested_seam",
                  started=base - 300, ended=base - 200, reason="done")
        self.seed(self.main_db, "rested_outside",
                  started=base - 4000, ended=base - 3900, reason="done")
        # An archived row inside the window: the Closed partition.
        self.seed(self.main_db, "closed_arch", started=base + 800,
                  ended=base + 900, reason="done", archived=1)
        # An open conversation far older than the window stays listed.
        self.seed(self.work_db, "open_ancient",
                  started=base - 100 * day)
        # A pinned conversation the window would drop stays reachable.
        self.seed(self.work_db, "pinned_ancient",
                  started=base - 50 * day, ended=base - 50 * day + 10,
                  reason="done", pinned=1)
        con = sqlite3.connect(self.work_db)
        con.execute("UPDATE sessions SET last_activity_at = ?"
                    " WHERE id IN ('open_ancient','pinned_ancient')",
                    (base - 40 * day,))
        con.commit()
        con.close()
        return base

    def test_window_seam_and_inclusions(self):
        self.seed_window()
        rows = self.inbox_rows()
        self.assertIn("rested_inside", rows)
        self.assertNotIn("rested_seam", rows)
        self.assertNotIn("rested_outside", rows)
        # Open and pinned rows older than the window are included.
        self.assertIn("open_ancient", rows)
        self.assertIn("pinned_ancient", rows)
        # The archived row renders in the Closed partition, after both:
        # a pin the window would have dropped stays reachable without
        # landing below a closed row.
        self.assertEqual(rows[-1], "closed_arch")
        self.assertLess(rows.index("pinned_ancient"),
                        rows.index("closed_arch"))
        self.assertLess(rows.index("open_ancient"),
                        rows.index("closed_arch"))

    def test_old_pinned_row_reachable_by_direct_page(self):
        self.seed_window()
        _status, _page = self.get("/s/work/pinned_ancient")
        self.assertEqual(_status, 200)
        _status, _page = self.get("/s/work/rested_outside")
        # Outside the window and unpinned: no inbox row, and the direct
        # page stays gone for it (nothing is ever deleted from the DB).
        self.assertEqual(_status, 404)


class TestVisibility(ProjectionCase):
    """Core owns which rows surface at all."""

    def test_compression_root_and_segments_never_surface(self):
        base = self.now - 1000
        self.seed(self.main_db, "rootA", started=base + 100,
                  ended=base + 110, reason="compression")
        self.seed(self.main_db, "midA", started=base + 120,
                  ended=base + 125, reason="compression",
                  parent="rootA")
        self.seed(self.main_db, "tipA", started=base + 130,
                  parent="midA")
        rows = self.inbox_rows()
        self.assertEqual(rows, ["tipa"])

    def test_branch_and_reset_children_surface_as_own_conversations(self):
        base = self.now - 1000
        self.seed(self.main_db, "br-base", started=base + 100,
                  ended=base + 105, reason="compression")
        self.seed(self.main_db, "br-off", started=base + 90,
                  branched_from="br-base", parent="br-base")
        self.seed(self.main_db, "rs-base", started=base + 80,
                  ended=base + 85, reason="done")
        self.seed(self.main_db, "rs-new", started=base + 86,
                  reset_from="rs-base", parent="rs-base")
        rows = set(self.inbox_rows())
        # The branch child and the reset child each surface as their
        # own conversation, and each root keeps its own row (the
        # branch root has no continuation: its only child is a branch).
        self.assertEqual(rows, {"br-base", "br-off",
                                "rs-base", "rs-new"})

    def test_delegate_and_subagent_children_are_hidden(self):
        base = self.now - 1000
        self.seed(self.main_db, "parent1", started=base + 100)
        self.seed(self.main_db, "deleg1", started=base + 110,
                  parent="parent1", delegate="parent1")
        self.seed(self.work_db, "sub1", started=base + 120,
                  source="subagent", parent="parent1")
        rows = set(self.inbox_rows())
        self.assertEqual(rows, {"parent1"})


class TestChains(ProjectionCase):
    """Chain behavior at 11, 51 and 101 edges."""

    EDGES = (11, 51, 101)

    def build(self, edges):
        # Well inside the 24h window, one hour per chain so the chains
        # seeded across subTests never collide or cross the seam.
        base = self.now - 3600 - edges * 200
        root = "chain%d" % edges
        self.seed(self.main_db, root, started=base,
                  ended=base + 1, reason="compression")
        members = self.chain(self.main_db, root, base, edges)
        self.message(self.main_db, root, "root message of %d" % edges)
        mid = members[len(members) // 2]
        self.message(self.main_db, mid, "mid message of %d" % edges)
        tip = members[-1]
        self.message(self.main_db, tip, "tip message of %d" % edges)
        # The core tip walk advances at most 100 steps, so on the
        # 101-edge chain the projected live tip is member #100 — one
        # short of the seeded terminal, which sits beyond the horizon.
        surfaced = members[99] if edges == 101 else tip
        return root, members, mid, tip, surfaced

    def test_only_the_projected_tip_surfaces(self):
        for edges in self.EDGES:
            with self.subTest(edges=edges):
                root, members, _mid, _tip, surfaced = self.build(edges)
                rows = self.inbox_rows()
                self.assertIn(surfaced, rows)
                hidden = [root] + [m for m in members if m != surfaced]
                for sid in hidden:
                    self.assertNotIn(sid, rows)

    def test_transcript_search_and_feed_cover_the_chain(self):
        for edges in self.EDGES:
            with self.subTest(edges=edges):
                root, members, mid, tip, surfaced = self.build(edges)
                horizon = edges == 101  # at the shared 100-step cap
                # Root id opens the whole projected conversation...
                _status, page = self.get("/s/default/%s" % root)
                texts = self.transcript_texts(page)
                self.assertIn("root message of %d" % edges, texts)
                self.assertIn("mid message of %d" % edges, texts)
                if horizon:
                    # The terminal past the cap is not part of the
                    # projected chain; nothing of it leaks in.
                    self.assertNotIn("tip message of %d" % edges, texts)
                else:
                    self.assertIn("tip message of %d" % edges, texts)
                # ...and so does any mid member (same chain, same cap).
                _status, page = self.get("/s/default/%s" % mid)
                mid_texts = self.transcript_texts(page)
                self.assertIn("root message of %d" % edges, mid_texts)
                if horizon:
                    self.assertNotIn("tip message of %d" % edges,
                                     mid_texts)
                else:
                    self.assertIn("tip message of %d" % edges, mid_texts)
                # Search by the root's id/title finds the projected row.
                _status, page = self.get("/?q=%s" % root)
                self.assertIn(self.rows_in(page)[0], members)
                # Its search blob carries the root's id: the row knows
                # the conversation it belongs to.
                blobs = re.findall(
                    r'<article class="conv[^>]*data-q="([^"]*)"',
                    self.get("/")[1])
                blob = next((b for b in blobs if surfaced in b), None)
                self.assertIsNotNone(blob)
                self.assertIn(root, blob)
                # The feed snapshots the projected chain under one
                # cursor: every visible message, nothing past the cap.
                snap = self.feed(root)
                self.assertEqual(len(snap["messages"]),
                                 2 if horizon else 3)
                self.assertGreaterEqual(snap["last_id"],
                                        2 if horizon else 3)
                # Closing the conversation the user sees — the
                # projected row — flips every member of its chain.
                token = re.search(
                    r'<meta name="mission-control-csrf"'
                    r' content="([^"]*)"', self.get("/new")[1]).group(1)
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/s/default/%s/close"
                    % (self.port, surfaced), data=b"{}",
                    headers={"Content-Type": "application/json",
                             "X-CSRF-Token": token,
                             "Origin": "http://127.0.0.1:%d"
                                       % self.port},
                    method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                # root + every member the projection can walk to:
                # edges+1 rows normally, one fewer at the cap (the
                # walk from the projected row never reaches the
                # terminal seeded past it).
                self.assertEqual(payload["affected"],
                                 edges if horizon else edges + 1)
                flags = self.archived_flags(
                    self.main_db, root, mid, surfaced)
                self.assertEqual(set(flags.values()), {1})
                if horizon:
                    # The terminal beyond the cap is its own unarchived
                    # row: the close acted on the canonical chain only.
                    self.assertEqual(
                        self.archived_flags(self.main_db, tip)[tip], 0)


class TestChainArchiveAndReopen(ProjectionCase):
    """Close/reopen act on the canonical chain and nothing else."""

    def test_close_via_tip_reopen_via_root(self):
        base = self.now - 1000
        self.seed(self.main_db, "rootB", started=base + 100,
                  ended=base + 110, reason="compression")
        self.seed(self.main_db, "tipB", started=base + 120,
                  parent="rootB")
        self.seed(self.main_db, "bystander", started=base + 130)
        self.message(self.main_db, "rootB", "root text")
        self.message(self.main_db, "tipB", "tip text")
        token = re.search(
            r'<meta name="mission-control-csrf" content="([^"]*)"',
            self.get("/new")[1]).group(1)

        def post(path):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (self.port, path), data=b"{}",
                headers={"Content-Type": "application/json",
                         "X-CSRF-Token": token,
                         "Origin": "http://127.0.0.1:%d" % self.port},
                method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        payload = post("/s/default/tipB/close")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["affected"], 2)
        flags = self.archived_flags(self.main_db, "rootB", "tipB",
                                    "bystander")
        self.assertEqual(flags, {"rootB": 1, "tipB": 1, "bystander": 0})
        # The closed conversation renders in the Closed section...
        rows = self.inbox_rows()
        self.assertEqual(rows[-1], "tipb")
        # ...and reopening via the ROOT restores every member.
        payload = post("/s/default/rootB/reopen")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["affected"], 2)
        self.assertEqual(
            self.archived_flags(self.main_db, "rootB", "tipB"),
            {"rootB": 0, "tipB": 0})
        # The feed still serves the conversation after the round trip.
        snap = self.feed("rootB")
        self.assertEqual(snap["last_id"], 2)


if __name__ == "__main__":
    unittest.main()
