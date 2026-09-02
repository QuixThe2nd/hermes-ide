#!/usr/bin/env python3
"""Isolation sentinels: nothing the suite exercises may read or change
a home it did not explicitly point at.

The suite's discipline under test: HERMES_HOME and the runtime path
globals (MAIN_DB, PROFILE_GLOB) are set before a module instance is
used, every instance starts with empty registries, and caches are
reset between scenarios. The sentinel here is a decoy "live" home —
HERMES_HOME actually points at it while the instance's globals point
at the fixture — and the full set of exercised read/write paths (inbox,
page, feed, lineage, archive sync, user close, subprocess spawn, real
HTTP GET) runs against the fixture. Afterwards the decoy DB is
byte-identical, its home grew no files, and its sentinel row never
surfaced in any response: it could not be read, and it could not be
changed.
"""

import hashlib
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
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

# Importing hermes_state (which server.py imports for the read-only
# SessionDB) materializes the ambient HERMES_HOME's directory tree at
# import time. Do that ONCE here — before any test points HERMES_HOME
# at its decoy — so loading a server module instance never writes the
# decoy: the import below runs against the suite's sandbox home, and
# every later exec_module finds hermes_state already in sys.modules.
# In a real `hermes mission_control serve` process the CLI has already
# imported hermes_state long before the plugin loads, so this mirrors
# production exactly.
import hermes_state  # noqa: E402,F401  (import side effect is the point)

STUB = '''#!/usr/bin/env python3
import os, sqlite3, sys, time
home = os.environ.get("HERMES_HOME", "")
with open(os.path.join(home, "spawned.txt"), "w") as fh:
    fh.write(home + "\\n")
db = os.path.join(home, "state.db")
if db:
    con = sqlite3.connect(db, timeout=10)
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp)"
        " VALUES ('spawn-marker','tool','routed',?)", (time.time(),))
    con.commit()
    con.close()
sys.exit(0)
'''


def load_server(tmp, main_db, profile_glob):
    spec = importlib.util.spec_from_file_location(
        "mc_server_iso_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = profile_glob
    return mod


class IsolationCase(unittest.TestCase):
    """A decoy live home (HERMES_HOME points here) plus a fixture home
    every module instance is pointed at instead."""

    def setUp(self):
        # The decoy "live" home: a canary DB with one sentinel row.
        self.decoy = tempfile.mkdtemp(prefix="iso-decoy-")
        self.addCleanup(shutil.rmtree, self.decoy, ignore_errors=True)
        self.decoy_db = os.path.join(self.decoy, "state.db")
        con = sqlite3.connect(self.decoy_db)
        con.executescript(SESSION_SCHEMA)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES ('SENTINEL-live-row','discord','live canary',"
            " ?,?,0,0)", (time.time() - 60, time.time()))
        con.commit()
        con.close()

        # The fixture home the instance will actually use.
        self.fixture = tempfile.mkdtemp(prefix="iso-fixture-")
        self.addCleanup(shutil.rmtree, self.fixture, ignore_errors=True)
        self.fixture_db = os.path.join(self.fixture, "state.db")
        con = sqlite3.connect(self.fixture_db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES ('sess_fix','cli','fixture row',?,?,0,0)",
            (now - 120, now - 5))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('sess_fix','user','fixture message',?)",
            (now - 120,))
        con.commit()
        con.close()

        # HERMES_HOME names the decoy — exactly the environment a real
        # deployment would run in — BEFORE the module instance exists.
        self._env = unittest.mock.patch.dict(
            os.environ, {"HERMES_HOME": self.decoy})
        self._env.start()
        self.addCleanup(self._env.stop)

        self.mod = load_server(
            self.fixture, self.fixture_db,
            os.path.join(self.fixture, "profiles", "*", "state.db"))
        stub = os.path.join(self.fixture, "hermes-stub")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)
        self.mod.HERMES_BIN = stub

        self._tok = unittest.mock.patch.object(
            self.mod, "load_discord_token", return_value="")
        self._tok.start()
        self.addCleanup(self._tok.stop)
        self._fetch = unittest.mock.patch.object(
            self.mod, "fetch_active_thread_ids",
            return_value=(["111222333444555666"], None))
        self._fetch.start()
        self.addCleanup(self._fetch.stop)

    # ---- sentinels -------------------------------------------------

    def decoy_digest(self):
        with open(self.decoy_db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def decoy_files(self):
        out = []
        for root, _dirs, names in os.walk(self.decoy):
            for name in names:
                out.append(os.path.relpath(os.path.join(root, name),
                                           self.decoy))
        return sorted(out)


class TestLiveHomeUntouchable(IsolationCase):
    """Every exercised path stays on the fixture paths."""

    def test_exercised_paths_never_read_or_write_the_live_home(self):
        digest_before = self.decoy_digest()
        files_before = self.decoy_files()

        seen = []

        def note(text):
            seen.append(text)

        # 1. inbox listing
        rows, _notes = self.mod.load_sessions(time.time())
        note(json.dumps([r["id"] for r in rows]))
        # 2. session page + feed
        dbs = {name: path for path, name in self.mod.discover_dbs()}
        chat = self.mod.load_chat("default", "sess_fix", dbs)
        note(self.mod.render_chat(chat))
        feed = self.mod.load_feed("default", "sess_fix", dbs, 0)
        note(json.dumps(feed["items"], default=str))
        # 3. lineage build (research_jobs under the fixture home)
        note(json.dumps(sorted(
            "%s:%s" % key for key in
            self.mod.lineage_index(time.time())["child_keys"])))
        # 4. archive sync pass (Discord patched)
        self.mod.discord_sync_once(time.time())
        # 5. a user close on the fixture session
        status, _payload = self.mod.set_session_archived(
            "default", "sess_fix", dbs, True)
        self.assertEqual(status, 200)
        # 6. a real subprocess routed through the discovered home
        code, _out, _err = self.mod.run_hermes(
            ["chat", "--oneshot", "-q", "probe"], home=self.mod.
            profile_home("default"))
        self.assertEqual(code, 0)
        # 7. the same surfaces over real HTTP
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        threading.Thread(target=httpd.serve_forever,
                         daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/" % httpd.server_address[1],
                    timeout=10) as resp:
                note(resp.read().decode("utf-8"))
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/s/default/sess_fix"
                    % httpd.server_address[1], timeout=10) as resp:
                note(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()

        # The decoy was never read: its sentinel row appears nowhere.
        for text in seen:
            self.assertNotIn("SENTINEL-live-row", text)
        self.assertNotIn("live canary", "".join(seen))
        # The decoy was never written: same bytes, same file set.
        self.assertEqual(self.decoy_digest(), digest_before)
        self.assertEqual(self.decoy_files(), files_before)
        # ...and the fixture paths DID move: the writes landed there.
        con = sqlite3.connect("file:%s?mode=ro" % self.fixture_db,
                              uri=True)
        try:
            self.assertEqual(con.execute(
                "SELECT archived FROM sessions WHERE id = 'sess_fix'"
            ).fetchone()[0], 1)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id ="
                " 'spawn-marker'").fetchone()[0], 1)
        finally:
            con.close()
        self.assertFalse(os.path.exists(
            os.path.join(self.decoy, "spawned.txt")))


class TestDiscoveryStaysInsideHome(IsolationCase):
    """A database outside the configured home — reached by a profile
    directory symlink or by a state.db symlink — is never discovered,
    never loaded, never written, and never reaches a response; the
    valid in-root DBs keep working beside it."""

    def setUp(self):
        super().setUp()
        # A third location: outside the configured home, with a marker
        # row a leak would surface.
        self.outside = tempfile.mkdtemp(prefix="iso-outside-")
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        self.outside_db = os.path.join(self.outside, "state.db")
        now = time.time()
        con = sqlite3.connect(self.outside_db)
        con.executescript(SESSION_SCHEMA)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES ('SENTINEL-outside-row','cli','outside marker',"
            " ?,?,0,0)", (now - 60, now))
        con.commit()
        con.close()

        # In-root control profile: a real directory and real state.db.
        inroot = os.path.join(self.fixture, "profiles", "real")
        os.makedirs(inroot, exist_ok=True)
        con = sqlite3.connect(os.path.join(inroot, "state.db"))
        con.executescript(SESSION_SCHEMA)
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES ('sess_inroot','cli','in-root row',?,?,0,0)",
            (now - 90, now - 5))
        con.commit()
        con.close()

        # Escape form 1: the whole profile directory is a symlink out.
        os.symlink(self.outside,
                   os.path.join(self.fixture, "profiles", "escape"))
        # Escape form 2: the profile directory is real, its state.db
        # is a symlink out.
        filed = os.path.join(self.fixture, "profiles", "escapefile")
        os.makedirs(filed, exist_ok=True)
        os.symlink(self.outside_db,
                   os.path.join(filed, "state.db"))
        # Broken scenery that must also pass without a crash: a broken
        # profile-directory symlink, a broken state.db symlink and an
        # empty profile directory.
        os.symlink(os.path.join(self.outside, "not-there"),
                   os.path.join(self.fixture, "profiles", "broken"))
        brokenfile = os.path.join(self.fixture, "profiles", "brokenfile")
        os.makedirs(brokenfile, exist_ok=True)
        os.symlink(os.path.join(self.outside, "gone.db"),
                   os.path.join(brokenfile, "state.db"))
        os.makedirs(os.path.join(self.fixture, "profiles", "emptydir"))

    def discovered_names(self):
        return {name for _path, name in self.mod.discover_dbs()}

    def outside_digest(self):
        with open(self.outside_db, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def outside_files(self):
        out = []
        for root, _dirs, names in os.walk(self.outside):
            for name in names:
                out.append(os.path.relpath(os.path.join(root, name),
                                           self.outside))
        return sorted(out)

    def test_discovery_drops_candidates_resolving_outside(self):
        names = self.discovered_names()
        self.assertNotIn("escape", names)
        self.assertNotIn("escapefile", names)
        self.assertNotIn("broken", names)
        self.assertNotIn("brokenfile", names)
        self.assertIn("default", names)
        self.assertIn("real", names)
        # an escaping profile has no trusted home either, so nothing
        # can be spawned or routed into it
        self.assertIsNone(self.mod.profile_home("escape"))
        self.assertIsNone(self.mod.profile_home("escapefile"))
        self.assertEqual(self.mod.profile_home("real"),
                         os.path.join(self.fixture, "profiles", "real"))

    def test_outside_marker_rows_never_surface_nor_load(self):
        rows, _notes = self.mod.load_sessions(time.time())
        ids = [r["id"] for r in rows]
        self.assertNotIn("SENTINEL-outside-row", ids)
        self.assertIn("sess_fix", ids)       # in-root main control
        self.assertIn("sess_inroot", ids)    # in-root profile control
        dbs = {name: path for path, name in self.mod.discover_dbs()}
        self.assertNotIn("escape", dbs)
        self.assertNotIn("escapefile", dbs)
        # an escaping profile is simply an unknown profile: nothing
        # about it can be loaded
        with self.assertRaises(KeyError):
            self.mod.load_chat("escape", "SENTINEL-outside-row", dbs)
        with self.assertRaises(KeyError):
            self.mod.load_chat("escapefile", "SENTINEL-outside-row",
                               dbs)
        # and the served inbox never shows the marker, over real HTTP
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        threading.Thread(target=httpd.serve_forever,
                         daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/" % httpd.server_address[1],
                    timeout=10) as resp:
                page = resp.read().decode("utf-8")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertNotIn("SENTINEL-outside-row", page)
        self.assertNotIn("outside marker", page)
        self.assertIn("in-root row", page)

    def test_outside_home_is_never_touched(self):
        digest_before = self.outside_digest()
        files_before = self.outside_files()
        self.mod.load_sessions(time.time())
        self.mod.discord_sync_once(time.time())
        self.mod.lineage_index(time.time())
        self.assertEqual(self.outside_digest(), digest_before)
        self.assertEqual(self.outside_files(), files_before)

    def test_main_db_symlinked_outside_is_not_served(self):
        link = os.path.join(self.fixture, "mainlink.db")
        os.symlink(self.outside_db, link)
        with unittest.mock.patch.object(self.mod, "MAIN_DB", link):
            names = self.discovered_names()
            self.assertNotIn("default", names)
            self.assertIn("real", names)
            self.assertEqual(self.mod.mission_control_ids(), [])
            rows, _notes = self.mod.load_sessions(time.time())
            self.assertNotIn("SENTINEL-outside-row",
                             [r["id"] for r in rows])
            self.assertIn("sess_inroot", [r["id"] for r in rows])

    def test_missing_racing_and_denied_candidates_never_crash(self):
        # A candidate that disappears between discovery passes is
        # simply gone on the next pass.
        raced = os.path.join(self.fixture, "profiles", "raced")
        os.makedirs(raced, exist_ok=True)
        raced_db = os.path.join(raced, "state.db")
        con = sqlite3.connect(raced_db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        self.assertIn("raced", self.discovered_names())
        os.remove(raced_db)
        self.assertNotIn("raced", self.discovered_names())
        # A missing main DB stays a load-time note, never a crash, and
        # the in-root profiles keep working around it.
        with unittest.mock.patch.object(
                self.mod, "MAIN_DB",
                os.path.join(self.fixture, "gone.db")):
            rows, notes = self.mod.load_sessions(time.time())
            self.assertIn("sess_inroot", [r["id"] for r in rows])
            self.assertTrue(any("default" in n for n in notes))
        # A resolution that fails outright (permission denied to even
        # resolve the candidate) rejects that candidate only.
        real_realpath = os.path.realpath

        def denied(path):
            if "escape" in path:
                raise OSError("permission denied")
            return real_realpath(path)

        with unittest.mock.patch("os.path.realpath", side_effect=denied):
            names = self.discovered_names()
        self.assertEqual(names, {"default", "real"})
        # The full inventory still answers a page render after all of it.
        rows, _notes = self.mod.load_sessions(time.time())
        self.assertIn("sess_fix", [r["id"] for r in rows])


class TestModuleStateResets(IsolationCase):
    """A fresh module instance shares nothing with a used one: jobs,
    notes, children, epochs and the lineage cache all start empty —
    the between-tests reset the suite relies on."""

    def test_fresh_instance_starts_empty_and_separate(self):
        # dirty the first instance the ways tests do
        with self.mod._jobs_lock:
            self.mod._jobs[("default", "sess_fix")] = {
                "started": time.time()}
        with self.mod._new_jobs_lock:
            self.mod._new_jobs["job1"] = {
                "state": self.mod.NEW_JOB_RUNNING, "session_id": None,
                "error": "", "created": time.time(), "finished": None}
        with self.mod._jobs_lock:
            self.mod._job_notes[("default", "sess_fix")] = "note"
        self.mod._archive_epochs[self.fixture_db] = 7
        self.mod.lineage_index(time.time())
        self.assertIsNotNone(self.mod._lineage_cache["index"])

        other = load_server(
            self.fixture, self.fixture_db,
            os.path.join(self.fixture, "profiles", "*", "state.db"))
        self.assertEqual(other._jobs, {})
        self.assertEqual(other._new_jobs, {})
        self.assertEqual(other._job_notes, {})
        self.assertEqual(other._archive_epochs, {})
        self.assertIsNone(other._lineage_cache["index"])
        self.assertEqual(other._lineage_cache["at"], 0.0)
        with other._children_lock:
            self.assertEqual(other._children, set())
        # truly distinct registries, not views of shared state
        self.assertIsNot(other._jobs, self.mod._jobs)
        self.assertIsNot(other._jobs_lock, self.mod._jobs_lock)
        self.assertIsNot(other._lineage_cache, self.mod._lineage_cache)
        # and the dirty instance's state did not leak into the new one
        self.assertIn(("default", "sess_fix"), self.mod._jobs)
        self.assertNotIn(("default", "sess_fix"), other._jobs)


if __name__ == "__main__":
    unittest.main()
