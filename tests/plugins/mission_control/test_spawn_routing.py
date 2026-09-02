#!/usr/bin/env python3
"""Every new/reply subprocess runs in the exact trusted home of the
profile the request was validated against.

A named-profile reply once inherited the server's own HERMES_HOME and
silently wrote the default profile. The fix under test: profile_home()
derives the child home only from the discovered DB mapping (main home
for "default", the profile's own directory for a named one), run_hermes
pins it as the child's HERMES_HOME, and argv is a plain list — never
shell=True. The fake executable here records its exact argv and its
own environment's HERMES_HOME, and writes a marker row into whatever
DB that home selects — proving both the exact spawn contract and that
writes land only in the selected temp DB.

No filesystem path is ever derived from raw URL input: profile names
resolve through discover_dbs() or not at all.
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
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()
TEXT = "spawn-routing probe 51ac"

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

# The fake executable at the real/fake boundary: it records the exact
# argv it received and the HERMES_HOME its environment carried, and
# writes one marker row into the state DB that home selects — the
# write lands wherever the server actually routed the child.
STUB = '''#!/usr/bin/env python3
import json, os, sqlite3, sys, time
home = os.environ.get("HERMES_HOME", "")
rec = {"argv": sys.argv[1:], "home": home, "pid": os.getpid()}
with open(os.path.join(%(dir)r, "spawn.log"), "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
db = os.path.join(home, "state.db") if home else ""
if db:
    con = sqlite3.connect(db, timeout=10)
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp)"
        " VALUES ('spawn-marker','tool',?,?)",
        (json.dumps(rec), time.time()))
    con.commit()
    con.close()
sys.exit(0)
'''


def load_server(tmp, main_db):
    spec = importlib.util.spec_from_file_location(
        "mc_server_spawn_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = os.path.join(tmp, "profiles", "*", "state.db")
    return mod


class SpawnCase(unittest.TestCase):
    """A default home plus one named profile, both with real DBs, and
    the hermes binary replaced by the recording stub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spawn-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main_db = os.path.join(self.tmp, "state.db")
        self.researcher_db = os.path.join(self.tmp, "profiles",
                                          "researcher", "state.db")
        for path in (self.main_db, self.researcher_db):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            con = sqlite3.connect(path)
            con.executescript(SESSION_SCHEMA)
            now = time.time()
            con.execute(
                "INSERT INTO sessions (id, source, title, started_at,"
                " last_activity_at, archived, hidden)"
                " VALUES ('sess_work','cli','fixture',?,?,0,0)",
                (now - 120, now - 5))
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES ('sess_work','user','hi',?)",
                (now - 120,))
            con.commit()
            con.close()

        self.mod = load_server(self.tmp, self.main_db)
        stub = os.path.join(self.tmp, "hermes-stub")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB % {"dir": self.tmp})
        os.chmod(stub, 0o755)
        self.mod.HERMES_BIN = stub

        self._csrf = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.drain_jobs)
        self.addCleanup(self.mod.terminate_children)

    # ---- helpers ---------------------------------------------------

    def drain_jobs(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self.mod._jobs_lock:
                reply_live = len(self.mod._jobs)
            with self.mod._new_jobs_lock:
                new_live = [j for j in self.mod._new_jobs.values()
                            if j["state"] in self.mod.NEW_JOB_LIVE_STATES]
            if not reply_live and not new_live:
                return
            time.sleep(0.05)

    def spawns(self):
        try:
            with open(os.path.join(self.tmp, "spawn.log")) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def wait_spawn(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.spawns():
                return self.spawns()[0]
            time.sleep(0.05)
        self.fail("the stub hermes was never launched")

    def marker_rows(self, db):
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id ="
                " 'spawn-marker'").fetchone()[0]
        finally:
            con.close()

    def csrf_token(self):
        if self._csrf is None:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/new" % self.port)
            with urllib.request.urlopen(req, timeout=10) as resp:
                page = resp.read().decode("utf-8")
            m = re.search(
                r'<meta name="mission-control-csrf" content="([^"]*)"',
                page)
            self.assertIsNotNone(m)
            self._csrf = m.group(1)
        return self._csrf

    def post_json(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Origin": "http://127.0.0.1:%d" % self.port,
                     "X-CSRF-Token": self.csrf_token()},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class TestReplyRouting(SpawnCase):
    """A named-profile reply spawns in the named profile's own home."""

    def test_named_profile_reply_runs_in_profile_home(self):
        status, _body = self.post_json(
            "/s/researcher/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 202)
        rec = self.wait_spawn()

        self.assertEqual(
            rec["argv"],
            ["--resume", "sess_work", "chat", "--oneshot", "-q", TEXT])
        self.assertEqual(os.path.realpath(rec["home"]),
                         os.path.realpath(os.path.join(
                             self.tmp, "profiles", "researcher")))
        self.assertNotEqual(os.path.realpath(rec["home"]),
                            os.path.realpath(self.tmp))
        # the marker row landed ONLY in the selected profile DB
        self.drain_jobs()
        self.assertEqual(self.marker_rows(self.researcher_db), 1)
        self.assertEqual(self.marker_rows(self.main_db), 0)

    def test_default_profile_reply_runs_in_main_home(self):
        status, _body = self.post_json(
            "/s/default/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 202)
        rec = self.wait_spawn()
        self.assertEqual(os.path.realpath(rec["home"]),
                         os.path.realpath(self.tmp))
        self.drain_jobs()
        self.assertEqual(self.marker_rows(self.main_db), 1)
        self.assertEqual(self.marker_rows(self.researcher_db), 0)


class TestNewSessionRouting(SpawnCase):
    """/s/new pins the discovered main home, and the argv is the exact
    oneshot contract."""

    def test_new_session_runs_in_main_home_with_exact_argv(self):
        status, _body = self.post_json("/s/new", {"text": TEXT})
        self.assertEqual(status, 202)
        rec = self.wait_spawn()
        self.assertEqual(
            rec["argv"],
            ["chat", "--oneshot", "--source",
             self.mod.NEW_SESSION_SOURCE, "-q", TEXT])
        self.assertEqual(os.path.realpath(rec["home"]),
                         os.path.realpath(self.tmp))
        self.drain_jobs()
        self.assertEqual(self.marker_rows(self.main_db), 1)
        self.assertEqual(self.marker_rows(self.researcher_db), 0)


class TestProfileHomeContract(unittest.TestCase):
    """The home mapping itself: discovered DBs or nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spawn-home-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main_db = os.path.join(self.tmp, "state.db")
        for path in (self.main_db,
                     os.path.join(self.tmp, "profiles", "bot", "state.db")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        self.mod = load_server(self.tmp, self.main_db)

    def test_homes_come_from_the_discovered_mapping(self):
        self.assertEqual(
            os.path.realpath(self.mod.profile_home("default")),
            os.path.realpath(self.tmp))
        self.assertEqual(
            os.path.realpath(self.mod.profile_home("bot")),
            os.path.realpath(os.path.join(self.tmp, "profiles", "bot")))

    def test_raw_url_input_resolves_to_no_home(self):
        for probe in ("../../etc", "..", "default/../../..",
                      "profiles/bot", "no-such-profile", "", None,
                      ".", "/absolute/path", "bot\x00"):
            self.assertIsNone(self.mod.profile_home(probe), repr(probe))


class TestRunHermesContract(unittest.TestCase):
    """run_hermes itself: list argv, pinned env, no shell."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spawn-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main_db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.main_db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.main_db)
        self.stub = os.path.join(self.tmp, "hermes-stub")
        with open(self.stub, "w", encoding="utf-8") as fh:
            fh.write(STUB % {"dir": self.tmp})
        os.chmod(self.stub, 0o755)
        self.mod.HERMES_BIN = self.stub

    def test_shell_metacharacters_stay_one_argument(self):
        # A shell would split or execute this; a list argv delivers it
        # verbatim as one token.
        sneaky = "x; rm -rf /; $(reboot) `id` && echo pwned"
        code, _out, _err = self.mod.run_hermes(
            ["chat", "--oneshot", "-q", sneaky], home=self.tmp)
        self.assertEqual(code, 0)
        recs = [json.loads(ln) for ln in
                open(os.path.join(self.tmp, "spawn.log"))]
        self.assertEqual(recs[0]["argv"][-1], sneaky)
        self.assertEqual(len(recs[0]["argv"]), 4)

    def test_home_pins_child_env_without_touching_ours(self):
        before = dict(os.environ)
        code, _out, _err = self.mod.run_hermes(["chat"], home=self.tmp)
        self.assertEqual(code, 0)
        rec = json.loads(
            open(os.path.join(self.tmp, "spawn.log")).read().splitlines()[0])
        self.assertEqual(os.path.realpath(rec["home"]),
                         os.path.realpath(self.tmp))
        self.assertEqual(os.environ, before)

    def test_vanished_profile_fails_the_job_without_a_run(self):
        # A profile that disappears between validation and spawn has
        # no home: reply_worker must fail the job, never launch a
        # child in some fallback home.
        self.mod.reply_worker("ghost-profile", "sess_work", None, TEXT)
        with self.mod._jobs_lock:
            note = self.mod._job_notes.get(("ghost-profile", "sess_work"))
        self.assertIsNotNone(note)
        self.assertIn("failed", note)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "spawn.log")),
            "no child may launch for a homeless profile")


if __name__ == "__main__":
    unittest.main()
