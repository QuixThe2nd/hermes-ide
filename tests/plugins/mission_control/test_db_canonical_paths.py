#!/usr/bin/env python3
"""Canonical database-path discipline, mutation-tested on a real
filesystem.

Every database this server touches is resolved to its canonical real
path at discovery, that value is the only one any later connect,
archive, transcript, spawn or mutation path may open, and the
connection boundary re-derives the canonical path and compares file
identity — so a symlink that escapes the home (or a swap between the
check and the open) cannot redirect a read or a write outside the
configured home. The sentinel below is a real SQLite file OUTSIDE the
home: it must be neither read (its row never surfaces) nor changed
(byte-identical after every scenario), no matter how it is linked in,
swapped in, or aliased.
"""

import hashlib
import importlib.util
import itertools
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

_MODULE_SEQ = itertools.count()

SID = "20260902_140000_aaaa1111"


def load_server(tmp, main_db, profile_glob):
    spec = importlib.util.spec_from_file_location(
        "mc_server_canon_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = profile_glob
    return mod


def make_db(path, sid=SID, title="row", minutes_ago=5):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at) VALUES (?,'cli',?,?,?)",
            (sid, title, now - 60 * minutes_ago - 60,
             now - 60 * minutes_ago))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,'user',?,?)",
            (sid, "body of " + title, now - 60 * minutes_ago))
        con.commit()
    finally:
        con.close()


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class CanonicalCase(unittest.TestCase):
    """A fixture home plus an OUTSIDE sentinel DB that must never be
    read or written by anything the server does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-canon-")
        self.home = os.path.join(self.tmp, "home")
        self.main_db = os.path.join(self.home, "state.db")
        make_db(self.main_db, sid=SID, title="main row")
        self.profiles = os.path.join(self.home, "profiles")
        # The sentinel: a real DB outside the home, with its own row.
        self.outside = os.path.join(self.tmp, "outside", "state.db")
        make_db(self.outside, sid="20260902_140000_sentinel",
                title="SENTINEL-outside-row")
        self.outside_digest = digest(self.outside)
        self.mod = load_server(
            self.tmp, self.main_db,
            os.path.join(self.profiles, "*", "state.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- probes ----------------------------------------------------

    def discovered(self):
        return {name: path for path, name in self.mod.discover_dbs()}

    def inbox_ids(self):
        rows, _notes = self.mod.load_sessions(time.time())
        return [r["id"] for r in rows], "\n".join(
            "%s" % n for n in _notes)

    def assert_sentinel_untouched(self, also=()):
        """The outside DB is byte-identical and nothing mentions it."""
        self.assertEqual(digest(self.outside), self.outside_digest)
        ids, notes = self.inbox_ids()
        self.assertNotIn("20260902_140000_sentinel", ids)
        self.assertNotIn("SENTINEL-outside-row", notes)
        for text in also:
            self.assertNotIn("SENTINEL-outside-row", text)
            self.assertNotIn("20260902_140000_sentinel", text)


class TestEscapingSymlinks(CanonicalCase):
    """A candidate whose path resolves outside the home is never
    served — and never opened, so the target is not even read."""

    def test_initially_escaping_profile_symlink_is_never_served(self):
        p1 = os.path.join(self.profiles, "p1")
        os.makedirs(p1)
        os.symlink(self.outside, os.path.join(p1, "state.db"))
        self.assertEqual(self.discovered(), {"default": self.main_db})
        self.assert_sentinel_untouched()

    def test_initially_escaping_main_db_is_never_served(self):
        # A whole-home spelling whose MAIN_DB resolves outside.
        home2 = os.path.join(self.tmp, "home2")
        os.makedirs(home2)
        os.symlink(self.outside, os.path.join(home2, "state.db"))
        mod = load_server(
            self.tmp, os.path.join(home2, "state.db"),
            os.path.join(home2, "profiles", "*", "state.db"))
        rows, _notes = mod.load_sessions(time.time())
        self.assertEqual([r["id"] for r in rows], [])
        self.assertNotIn("default",
                         {name for _p, name in mod.discover_dbs()})
        self.assert_sentinel_untouched()

    def test_escaping_directory_component_is_never_served(self):
        # The profile DIRECTORY is the symlink, not the DB file.
        os.makedirs(self.profiles, exist_ok=True)
        os.symlink(os.path.join(self.tmp, "outside"),
                   os.path.join(self.profiles, "p2"))
        self.assertEqual(self.discovered(), {"default": self.main_db})
        self.assert_sentinel_untouched()


class TestSwapAfterDiscovery(CanonicalCase):
    """A candidate that checked out at discovery but was swapped
    before the open is told apart by file identity."""

    def setUp(self):
        super().setUp()
        self.p1 = os.path.join(self.profiles, "p1")
        make_db(os.path.join(self.p1, "state.db"),
                sid="20260902_140000_bbbb2222", title="profile row")
        # Discovery pass over the valid layout.
        self.assertIn("p1", self.discovered())

    def test_swapped_profile_db_is_refused_at_connect(self):
        # The check/open race: capture the discovered mapping while the
        # layout is still valid, swap the file for a symlink to the
        # outside sentinel, then connect through the STALE mapping.
        stale = self.discovered()
        self.assertIn("p1", stale)
        os.remove(os.path.join(self.p1, "state.db"))
        os.symlink(self.outside, os.path.join(self.p1, "state.db"))
        with self.assertRaises(sqlite3.OperationalError):
            self.mod.load_chat(
                "p1", "20260902_140000_bbbb2222", stale)
        # A fresh discovery pass refuses the swapped spelling outright.
        dbs = self.discovered()
        self.assertNotIn("p1", dbs)
        # ...and a page load against the still-valid default DB never
        # reads the swapped target (its sentinel row stays unread).
        chat = self.mod.load_chat("default", SID, dbs)
        self.assertIn("main row", self.mod.render_chat(chat))
        self.assert_sentinel_untouched()

    def test_identity_swap_same_path_different_file(self):
        # Replace the file in place with a DIFFERENT database: canonical
        # containment still holds, but the identity no longer matches
        # the discovery pass, so the connection is refused.
        other = os.path.join(self.tmp, "other.db")
        make_db(other, sid="20260902_140000_ffff6666", title="other row")
        os.replace(other, os.path.join(self.p1, "state.db"))
        with self.assertRaises(sqlite3.OperationalError):
            self.mod._connect_db(os.path.join(self.p1, "state.db"))
        self.assert_sentinel_untouched()

    def test_write_flavor_never_creates_a_swapped_target(self):
        # Point the served spelling at a NONEXISTENT file outside the
        # home: mode=rw may flip an existing database, never conjure
        # one, so the archive flip fails and the target stays absent.
        gone = os.path.join(self.tmp, "outside", "gone.db")
        os.remove(os.path.join(self.p1, "state.db"))
        os.symlink(gone, os.path.join(self.p1, "state.db"))
        status, _payload = self.mod.set_session_archived(
            "p1", "20260902_140000_bbbb2222",
            {"default": self.main_db,
             "p1": os.path.join(self.p1, "state.db")},
            True)
        self.assertNotEqual(status, 200)
        self.assertFalse(os.path.exists(gone))
        self.assert_sentinel_untouched()


class TestAliasesAndValidLayouts(CanonicalCase):
    """Path aliases dedupe; valid main and named DBs both serve."""

    def test_profile_directory_alias_dedupes(self):
        real = os.path.join(self.profiles, "realp")
        make_db(os.path.join(real, "state.db"),
                sid="20260902_140000_cccc3333", title="real row")
        os.symlink(real, os.path.join(self.profiles, "aliasp"))
        dbs = self.discovered()
        self.assertIn("realp", dbs)
        self.assertNotIn("aliasp", dbs)

    def test_main_db_via_profile_glob_is_not_double_served(self):
        # The profile glob can never reach the main DB, but a profile
        # symlink pointing AT the main DB is an alias and dedupes.
        os.makedirs(self.profiles, exist_ok=True)
        os.symlink(self.home, os.path.join(self.profiles, "mirror"))
        dbs = self.discovered()
        names = list(dbs)
        self.assertEqual(names.count("default"), 1)
        self.assertNotIn("mirror", dbs)

    def test_valid_main_and_named_profile_dbs_both_serve(self):
        make_db(os.path.join(self.profiles, "work", "state.db"),
                sid="20260902_140000_dddd4444", title="work row")
        dbs = self.discovered()
        self.assertEqual(sorted(dbs), ["default", "work"])
        ids, notes = self.inbox_ids()
        self.assertIn(SID, ids)
        self.assertIn("20260902_140000_dddd4444", ids)
        self.assertNotIn("SENTINEL", notes)
        # Transcript + feed out of the named profile work canonically.
        chat = self.mod.load_chat(
            "work", "20260902_140000_dddd4444", dbs)
        self.assertIn("body of work row",
                      self.mod.render_chat(chat))
        feed = self.mod.load_feed(
            "work", "20260902_140000_dddd4444", dbs, 0)
        self.assertEqual(feed["last_id"], 1)
        self.assert_sentinel_untouched()

    def test_canonical_spelling_is_what_gets_served(self):
        # Reach the same home through a symlinked path: discovery
        # answers the resolved real path, never the alias spelling.
        alias_home = os.path.join(self.tmp, "alias-home")
        os.symlink(self.home, alias_home)
        mod = load_server(
            self.tmp, os.path.join(alias_home, "state.db"),
            os.path.join(alias_home, "profiles", "*", "state.db"))
        for path, _name in mod.discover_dbs():
            self.assertEqual(path, os.path.realpath(path))
            self.assertTrue(os.path.isabs(path))
        self.assertEqual({name: path for path, name in
                          mod.discover_dbs()}["default"],
                         os.path.realpath(self.main_db))

    def test_missing_main_db_is_named_in_a_note_only(self):
        mod = load_server(
            self.tmp, os.path.join(self.home, "gone.db"),
            os.path.join(self.profiles, "*", "state.db"))
        make_db(os.path.join(self.profiles, "p9", "state.db"),
                sid="20260902_140000_eeee5555", title="p9 row")
        rows, notes = mod.load_sessions(time.time())
        self.assertIn("20260902_140000_eeee5555", [r["id"] for r in rows])
        self.assertTrue(any("default" in n for n in notes))
        self.assertFalse(os.path.exists(os.path.join(self.home, "gone.db")))


if __name__ == "__main__":
    unittest.main()
