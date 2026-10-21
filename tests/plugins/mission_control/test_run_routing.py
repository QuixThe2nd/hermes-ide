#!/usr/bin/env python3
"""Every composer turn is admitted as a run on the exact profile-scoped
core API surface the request was validated against.

A named-profile reply once inherited the server's own HERMES_HOME and
silently wrote the default profile (the oneshot-era bug this file was
born for). The transport is different now — no child is ever spawned —
but the routing contract is the same shape and these tests pin it at
the network seam: the admission request the module builds must carry
the discovered profile's URL prefix (/p/<profile>/v1/runs) and
authorize with the API_SERVER_KEY from that profile's own .env (the
default profile's from the .env beside the main DB, a named profile's
from the .env beside its own state.db — never the other way round),
and the prompt must travel only inside the one request's JSON body:
never argv, never a URL, never a header.

No filesystem path is ever derived from raw URL input: profile names
resolve through discover_dbs() or not at all.
"""

import http.client
import importlib.util
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()
TEXT = "run-routing probe 51ac"

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL


def load_server(tmp, main_db):
    spec = importlib.util.spec_from_file_location(
        "mc_server_route_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = main_db
    mod.PROFILE_GLOB = os.path.join(tmp, "profiles", "*", "state.db")
    return mod


class _FakeResponse:
    def __init__(self, status, obj):
        self.status = status
        self._body = json.dumps(obj).encode("utf-8")

    def getcode(self):
        return self.status

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCoreTransport:
    """Stands in for the core API server at the urllib urlopen seam.

    Records every request the module built — method, full URL,
    Authorization header, and JSON body — and answers the shapes the
    transport expects: a 202 naming the run and its canonical session
    id (creating the session row the admitted agent would write, for a
    fresh run), completed statuses for the poller. Recording lives in
    the test process only; nothing is logged or printed.
    """

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.requests = []
        self.fail_admission = False

    # -- recording -----------------------------------------------------

    def admissions(self):
        with self.lock:
            return [r for r in self.requests
                    if r["method"] == "POST"
                    and r["url"].endswith("/v1/runs")]

    def urls(self):
        with self.lock:
            return [r["url"] for r in self.requests]

    # -- the seam ------------------------------------------------------

    def __call__(self, req, timeout=None):
        body = req.data.decode("utf-8") if req.data else ""
        payload = json.loads(body) if body else {}
        with self.lock:
            self.requests.append({
                "method": req.get_method(),
                "url": req.full_url,
                "auth": req.headers.get("Authorization", ""),
                "payload": payload,
            })
            count = len([r for r in self.requests
                         if r["method"] == "POST"
                         and r["url"].endswith("/v1/runs")])
        if self.fail_admission:
            raise OSError("core unreachable")
        path = req.full_url.split("/p/", 1)[-1]
        if req.get_method() == "POST" and path.endswith("/v1/runs"):
            sid = payload.get("session_id") or ""
            if not sid:
                sid = "20260902_route_fresh_%d" % count
                con = sqlite3.connect(self.db, timeout=10)
                now = time.time()
                con.execute(
                    "INSERT OR REPLACE INTO sessions (id, source, title,"
                    " started_at, last_activity_at, archived, hidden)"
                    " VALUES (?,?,?,?,?,0,0)",
                    (sid, "mission-control", "stub session", now, now))
                con.execute(
                    "INSERT INTO messages (session_id, role, content,"
                    " timestamp) VALUES (?,?,?,?)",
                    (sid, "user", payload.get("input", ""), now))
                con.commit()
                con.close()
            return _FakeResponse(202, {
                "run_id": "run_route_%d" % count, "status": "started",
                "session_id": sid, "replayed": False})
        if "/stop" in path:
            return _FakeResponse(200, {"run_id": "x", "status": "stopping"})
        return _FakeResponse(200, {
            "run_id": "run_route_x", "status": "completed",
            "session_id": payload.get("session_id") or ""})


class RunRoutingCase(unittest.TestCase):
    """A default home plus one named profile, both with real DBs and
    their own .env API keys, and the core API faked at the urlopen
    seam — plus a tripwire that fails the test if anything ever tries
    to spawn a CLI child."""

    MAIN_KEY = "sk-main-fixture-key-000000000001"
    NAMED_KEY = "sk-researcher-fixture-key-0001"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="route-test-")
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
        # each profile's API_SERVER_KEY lives beside its own DB
        with open(os.path.join(self.tmp, ".env"), "w") as fh:
            fh.write("API_SERVER_KEY=%s\n" % self.MAIN_KEY)
        with open(os.path.join(self.tmp, "profiles", "researcher",
                               ".env"), "w") as fh:
            fh.write("API_SERVER_KEY=%s\n" % self.NAMED_KEY)

        self.mod = load_server(self.tmp, self.main_db)
        self.core = FakeCoreTransport(self.main_db)
        self._urlopen = unittest.mock.patch(
            "urllib.request.urlopen", side_effect=self.core)
        self._urlopen.start()
        self.addCleanup(self._urlopen.stop)
        # The transport contract under test: a composer turn may talk
        # to the core API, but it may never fall back to a child.
        self._spawns = []
        for name in ("Popen", "run"):
            patcher = unittest.mock.patch.object(
                subprocess, name,
                side_effect=lambda *a, **k: self._spawns.append((name, a)))
            patcher.start()
            self.addCleanup(patcher.stop)

        self._csrf = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.drain_jobs)
        self.addCleanup(self.assert_no_children)

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

    def assert_no_children(self):
        self.assertEqual(self._spawns, [],
                         "a composer turn spawned a CLI child")

    def csrf_token(self):
        if self._csrf is None:
            _status, page = self.get("/new")
            m = re.search(
                r'<meta name="mission-control-csrf" content="([^"]*)"',
                page)
            self.assertIsNotNone(m)
            self._csrf = m.group(1)
        return self._csrf

    # http.client (not urllib) so the urlopen seam stays fake-only
    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=15)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def get_json(self, path):
        status, body = self.get(path)
        return status, json.loads(body)

    def post_json(self, path, obj):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=15)
        try:
            conn.request(
                "POST", path, body=json.dumps(obj),
                headers={"Content-Type": "application/json",
                         "Origin": "http://127.0.0.1:%d" % self.port,
                         "X-CSRF-Token": self.csrf_token()})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()


class TestReplyRouting(RunRoutingCase):
    """A named-profile reply admits its run on the named profile's own
    URL prefix and key."""

    def test_named_profile_reply_admits_under_its_own_scope(self):
        status, _body = self.post_json(
            "/s/researcher/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 202)
        self.drain_jobs()

        admissions = self.core.admissions()
        self.assertEqual(len(admissions), 1)
        admission = admissions[0]
        # the profile-scoped core surface for the named profile
        self.assertTrue(admission["url"].endswith(
            "/p/researcher/v1/runs"), admission["url"])
        # authorized with the named profile's own key — never the main
        self.assertEqual(admission["auth"],
                         "Bearer %s" % self.NAMED_KEY)
        # the prompt and the exact session id travel only in the body
        self.assertEqual(admission["payload"],
                         {"input": TEXT, "session_id": "sess_work"})
        for url in self.core.urls():
            self.assertNotIn(TEXT, url)

    def test_default_profile_reply_admits_under_the_main_scope(self):
        status, _body = self.post_json(
            "/s/default/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 202)
        self.drain_jobs()

        admissions = self.core.admissions()
        self.assertEqual(len(admissions), 1)
        self.assertTrue(admissions[0]["url"].endswith(
            "/p/default/v1/runs"), admissions[0]["url"])
        self.assertEqual(admissions[0]["auth"],
                         "Bearer %s" % self.MAIN_KEY)
        self.assertEqual(admissions[0]["payload"],
                         {"input": TEXT, "session_id": "sess_work"})


class TestNewSessionRouting(RunRoutingCase):
    """/s/new admits under the default profile's scope, prompt in the
    body only, exactly once, and publishes the deterministic id."""

    def test_new_session_admits_under_default_scope(self):
        status, body = self.post_json("/s/new", {"text": TEXT})
        self.assertEqual(status, 202)
        self.drain_jobs()

        admissions = self.core.admissions()
        self.assertEqual(len(admissions), 1)
        self.assertTrue(admissions[0]["url"].endswith("/p/default/v1/runs"),
                        admissions[0]["url"])
        self.assertEqual(admissions[0]["auth"],
                         "Bearer %s" % self.MAIN_KEY)
        # no session id in the body: the core assigns the
        # deterministic one and the 202 echoes it
        self.assertEqual(admissions[0]["payload"], {"input": TEXT})
        for url in self.core.urls():
            self.assertNotIn(TEXT, url)

        # the job settled done on exactly the fresh row the faked
        # agent wrote — the deterministic id, not a diff
        _status, settled = self.get_json(body["status_url"])
        self.assertEqual(settled["status"], "done")
        con = sqlite3.connect("file:%s?mode=ro" % self.main_db, uri=True)
        try:
            fresh = con.execute(
                "SELECT id FROM sessions WHERE source ="
                " 'mission-control'").fetchall()
        finally:
            con.close()
        self.assertEqual([settled["session_id"]], [row[0] for row in fresh])


class TestAdmissionFailClosed(RunRoutingCase):
    """Every way an admission can go wrong is an explicit failed send:
    nothing runs, nothing is written, the lease is released, and there
    is never a second attempt or a CLI fallback."""

    def test_unreachable_core_is_a_503_not_a_fallback(self):
        self.core.fail_admission = True
        status, body = self.post_json(
            "/s/default/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.drain_jobs()
        with self.mod._jobs_lock:
            self.assertNotIn(("default", "sess_work"), self.mod._jobs)

    def test_session_id_mismatch_is_a_503(self):
        # The core must run the exact session the composer addressed;
        # a 202 echoing any other id is a refusal, never a fork.
        real_call = self.core.__call__

        def _mismatched(req, timeout=None):
            resp = real_call(req, timeout=timeout)
            if resp.status == 202:
                obj = json.loads(resp.read())
                obj["session_id"] = "sess_somebody_else"
                return _FakeResponse(202, obj)
            return resp

        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=_mismatched):
            status, _body = self.post_json(
                "/s/default/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 503)
        self.drain_jobs()
        with self.mod._jobs_lock:
            self.assertNotIn(("default", "sess_work"), self.mod._jobs)

    def test_malformed_admission_is_a_503(self):
        def _bodyless(req, timeout=None):
            return _FakeResponse(202, {"status": "started"})  # no ids

        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=_bodyless):
            status, _body = self.post_json(
                "/s/default/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 503)
        self.drain_jobs()
        with self.mod._jobs_lock:
            self.assertNotIn(("default", "sess_work"), self.mod._jobs)

    def test_unknown_profile_and_bad_session_ids_admit_nothing(self):
        status, _ = self.post_json(
            "/s/nosuchprofile/sess_work/reply", {"text": TEXT})
        self.assertEqual(status, 404)
        status, _ = self.post_json(
            "/s/default/../../etc/reply", {"text": TEXT})
        self.assertIn(status, (404, 400))
        self.drain_jobs()
        self.assertEqual(self.core.admissions(), [])

    def test_shell_metacharacters_stay_inside_the_json_body(self):
        # A shell would split or execute this; the JSON body delivers
        # it verbatim as one string — and no child ever exists to
        # interpret it at all.
        sneaky = "x; rm -rf /; $(reboot) `id` && echo pwned"
        status, _body = self.post_json(
            "/s/default/sess_work/reply", {"text": sneaky})
        self.assertEqual(status, 202)
        self.drain_jobs()
        self.assertEqual(self.core.admissions()[0]["payload"]["input"],
                         sneaky)
        for url in self.core.urls():
            self.assertNotIn("rm -rf", url)


class TestProfileHomeContract(unittest.TestCase):
    """The home mapping itself: discovered DBs or nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="route-home-")
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


if __name__ == "__main__":
    unittest.main()
