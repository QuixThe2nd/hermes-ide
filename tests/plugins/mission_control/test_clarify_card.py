#!/usr/bin/env python3
"""Focused tests for the narrow interactive clarify card.

Covers the whole slice at the HTTP layer against a stub core API
(never the real one): the per-profile key sourcing and /p/<profile>
URL prefix (proved without ever printing a key — the stub records
booleans, paths and parsed bodies, never header values), the initial
page and feed carrying {active, id, html} with null removing the card
and any API error leaving the page untouched (the feed omits the
field), the local POST /s/<profile>/<id>/clarify validating the
session/body and proxying the exact payload while answering only the
safe 200/400/404/409/503 set — never /reply, never a user-message
write — and the shipped card/client contract: escaped Discord-style
markup, single-select instant submit, multi-select toggling with a
UI-only Other whose label is never sent, the open-text input, the
same-id-preserves/new-id-resets rule, the composer freeze while
sending and its "Answer the question above" disable, and the safe
flash/re-poll on failure.

The stub core API binds an ephemeral loopback port (read back after
the bind, so parallel runs never collide) and mirrors the reviewed
core contract: Bearer API_SERVER_KEY per profile, GET returning
{object, session_id, pending_clarify}, POST validating clarify_id and
the response shape with 200/400/404/409. Every web-UI POST here also
carries the served CSRF token, exactly like the shipped client's
postJson helper — the clarify proxy sits behind the forgery gate.

Run:  python3 tests/plugins/mission_control/test_clarify_card.py
(unittest, stdlib only)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
SERVER_PY = os.path.join(REPO, "plugins", "mission_control", "server.py")

_MODULE_SEQ = itertools.count()

# Synthetic per-profile keys for the fixture .env files. They are
# deliberately different so a swapped/wrong key can never authorize.
DEFAULT_KEY = "test-api-key-default-4f1a0c2b"
HELPER_KEY = "test-api-key-helper-77d0e9aa"

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

CLARIFY_PATH_RE = re.compile(
    r"^/p/([^/]+)/api/sessions/([^/]+)/clarify$")


class StubCoreHandler(BaseHTTPRequestHandler):
    """The reviewed core clarify contract, as a tiny stub.

    Class attributes hold the per-test state (reset by the test case):
    KEYS maps profile -> the one Bearer key that authorizes it, CARDS
    maps (profile, session_id) -> pending card dict or None, MODE
    selects scripted failures. LOG records every request as
    {method, path, profile, session_id, authorized, body} — authorized
    is a boolean and body the parsed JSON, so no key value is ever
    recorded, printed or echoed.
    """

    KEYS = {}
    CARDS = {}
    MODE = "ok"          # ok | fail | unauth | ambiguous
    LOG = []

    def log_message(self, fmt, *args):
        # Request line only; the Authorization header never reaches here.
        pass

    def _authorized(self, profile):
        header = self.headers.get("Authorization") or ""
        expected = self.KEYS.get(profile)
        return bool(expected) and header == "Bearer " + expected

    def _answer(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        m = CLARIFY_PATH_RE.match(self.path)
        if m is None:
            self._answer(404, {"error": {"message": "no such route"}})
            return
        profile, session_id = m.group(1), m.group(2)
        authorized = self._authorized(profile)
        self.LOG.append({"method": "GET", "path": self.path,
                         "profile": profile, "session_id": session_id,
                         "authorized": authorized, "body": None})
        if self.MODE == "fail":
            self._answer(500, {"error": {"message": "exploded",
                                         "secret": "upstream-secret"}})
            return
        if self.MODE == "ambiguous":
            self._answer(409, {"error": {"code": "clarify_ambiguous",
                                          "message": "too many"}})
            return
        if self.MODE == "unauth" or not authorized:
            self._answer(401, {"error": {"code": "gateway_auth_failed",
                                          "message": "bad key"}})
            return
        card = self.CARDS.get((profile, session_id))
        self._answer(200, {"object": "hermes.session.clarify",
                           "session_id": session_id,
                           "pending_clarify": card})

    def do_POST(self):
        m = CLARIFY_PATH_RE.match(self.path)
        if m is None:
            self._answer(404, {"error": {"message": "no such route"}})
            return
        profile, session_id = m.group(1), m.group(2)
        authorized = self._authorized(profile)
        body = self._read_json()
        self.LOG.append({"method": "POST", "path": self.path,
                         "profile": profile, "session_id": session_id,
                         "authorized": authorized, "body": body})
        if self.MODE == "fail":
            self._answer(500, {"error": {"message": "exploded"}})
            return
        if self.MODE == "ambiguous":
            self._answer(409, {"error": {"code": "clarify_ambiguous",
                                          "message": "too many"}})
            return
        if self.MODE == "unauth" or not authorized:
            self._answer(401, {"error": {"code": "gateway_auth_failed",
                                          "message": "bad key"}})
            return
        if not isinstance(body, dict) \
                or not isinstance(body.get("clarify_id"), str):
            self._answer(400, {"error": {"code": "invalid_clarify_id"}})
            return
        card = self.CARDS.get((profile, session_id))
        if not isinstance(card, dict) \
                or card.get("clarify_id") != body["clarify_id"].strip():
            self._answer(404, {"error": {"code": "clarify_not_found"}})
            return
        resp = body.get("response")
        multi = bool(card.get("multi_select"))
        if isinstance(resp, list) and resp and not multi:
            self._answer(400, {"error": {
                "code": "invalid_clarify_response",
                "message": "single-select wants a string"}})
            return
        ok_text = isinstance(resp, str) and resp.strip()
        ok_list = (isinstance(resp, list) and resp
                   and all(isinstance(r, str) and r.strip()
                           for r in resp))
        if not (ok_text or ok_list):
            self._answer(400, {"error": {
                "code": "invalid_clarify_response"}})
            return
        # Resolved: exactly this clarify_id is consumed.
        self.CARDS[(profile, session_id)] = None
        self._answer(200, {"object": "hermes.session.clarify.response",
                           "session_id": session_id,
                           "clarify_id": body["clarify_id"].strip(),
                           "resolved": True})


class ClarifyCase(unittest.TestCase):
    """A stub core API on an ephemeral port, plus the web UI over a
    synthetic default+helper+scout profile tree (scout deliberately has
    NO .env of its own)."""

    POLL_TIMEOUT = 10.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clarify-test-")
        # default profile: main state.db + .env beside it
        self.db = os.path.join(self.tmp, "state.db")
        self._make_db(self.db)
        self._write_env(os.path.join(self.tmp, ".env"),
                        "API_SERVER_KEY=" + DEFAULT_KEY)
        # named profiles: their own state.db, helper with its own .env,
        # scout with none (its key must never be inherited)
        self.helper_db = os.path.join(self.tmp, "profiles", "helper",
                                      "state.db")
        self._make_db(self.helper_db)
        self._write_env(os.path.join(self.tmp, "profiles", "helper",
                                     ".env"),
                        "API_SERVER_KEY=" + HELPER_KEY)
        self.scout_db = os.path.join(self.tmp, "profiles", "scout",
                                     "state.db")
        self._make_db(self.scout_db)

        StubCoreHandler.KEYS = {"default": DEFAULT_KEY,
                                "helper": HELPER_KEY}
        StubCoreHandler.CARDS = {}
        StubCoreHandler.MODE = "ok"
        StubCoreHandler.LOG = []
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0),
                                        StubCoreHandler)
        self.stub_port = self.stub.server_address[1]
        threading.Thread(target=self.stub.serve_forever,
                         daemon=True).start()

        self.mod = self.load_server()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        # every response body the UI produced, for the no-key-leak check
        self.responses = []

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.stub.shutdown()
        self.stub.server_close()
        StubCoreHandler.KEYS = {}
        StubCoreHandler.CARDS = {}
        StubCoreHandler.LOG = []
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture helpers -------------------------------------------

    def load_server(self):
        """One isolated server.py module per test: its MAIN_DB and
        profile glob point at the fixture tree, and its clarify client
        points at the stub core API's ephemeral port."""
        spec = importlib.util.spec_from_file_location(
            "mc_server_clarify_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.MAIN_DB = os.path.join(self.tmp, "state.db")
        mod.PROFILE_GLOB = os.path.join(self.tmp, "profiles", "*",
                                        "state.db")
        mod.CLARIFY_API_BASE = "http://127.0.0.1:%d" % self.stub_port
        return mod

    def _make_db(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()

    def _write_env(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def add_session(self, db, sid, source="mission-control",
                    title="fixture", archived=0):
        con = sqlite3.connect(db)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title,"
            " started_at, last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,?,0)",
            (sid, source, title, now, now, archived))
        con.commit()
        con.close()

    def message_count(self, db, sid):
        con = sqlite3.connect(db)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (sid,)).fetchone()[0]
        finally:
            con.close()

    def set_card(self, profile, sid, card):
        StubCoreHandler.CARDS[(profile, sid)] = card

    def stub_entries(self, method=None, profile=None):
        return [e for e in StubCoreHandler.LOG
                if (method is None or e["method"] == method)
                and (profile is None or e["profile"] == profile)]

    # ---- HTTP helpers ----------------------------------------------

    def request(self, method, path, obj=None, token=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = {}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = application_json = \
                "application/json"
        if token:
            headers["X-CSRF-Token"] = token
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                self.responses.append(body)
                return resp.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            self.responses.append(body)
            return exc.code, body

    def request_json(self, method, path, obj=None, token=None):
        status, body = self.request(method, path, obj, token)
        return status, json.loads(body)

    def post(self, path, obj):
        """A same-origin state-changing POST: JSON body plus the served
        CSRF token in the non-simple header, like the real client."""
        status, page = self.request("GET", "/new")
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="mission-control-csrf"'
                      r' content="([^"]*)"', page)
        self.assertIsNotNone(m, "served page carries the CSRF meta tag")
        return self.request_json("POST", path, obj, m.group(1))

    def feed(self, sid, profile="default"):
        return self.request_json(
            "GET", "/s/%s/%s/feed?after=0" % (profile, sid))


class TestFeedAndPage(ClarifyCase):
    """Contract 1-2: the authenticated GET, the {active,id,html} feed
    shape, the initial page render, null removal and error immunity."""

    HOSTILE_CARD = {
        "clarify_id": "card-1",
        "question": "Ship <script>alert(1)</script> now?",
        "choices": ["Ship it (Recommended)", "Hold"],
        "multi_select": False,
        "source": "api_run",
    }

    def test_feed_card_active_escaped_and_prefixed(self):
        sid = "sess_default_a"
        self.add_session(self.db, sid)
        self.set_card("default", sid, dict(self.HOSTILE_CARD))
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        cl = payload["clarify"]
        self.assertTrue(cl["active"])
        self.assertEqual(cl["id"], "card-1")
        # escaped Discord-style card, never raw markup
        self.assertIn('id="clarify-card"', cl["html"])
        self.assertIn("data-clarify-id=\"card-1\"", cl["html"])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;",
                      cl["html"])
        self.assertNotIn("<script>alert(1)</script>", cl["html"])
        self.assertIn("Ship it (Recommended)", cl["html"])
        # UI-only Other rides along, never as a response value
        self.assertIn("clarify-other-toggle", cl["html"])
        self.assertNotIn('data-value="Other"', cl["html"])
        # the GET went through the /p/<profile> prefix, authenticated
        # with the default profile's own key (stub 401s otherwise)
        gets = self.stub_entries("GET", "default")
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["path"],
                         "/p/default/api/sessions/%s/clarify" % sid)
        self.assertTrue(gets[0]["authorized"],
                        "default requests must carry the default "
                        "profile's own key")

    def test_initial_page_renders_card_and_disables_composer(self):
        sid = "sess_default_b"
        self.add_session(self.db, sid)
        self.set_card("default", sid, dict(self.HOSTILE_CARD))
        status, body = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertIn('id="clarify-card"', body)
        self.assertIn("&lt;script&gt;", body)
        # the composer is born disabled with the clarify placeholder
        self.assertIn("Answer the question above", body)
        self.assertRegex(
            body, r'<textarea id="composer-text"[^>]*disabled')

    def test_null_removes_card_and_error_paths_never_break_page(self):
        sid = "sess_default_c"
        self.add_session(self.db, sid)

        # no pending clarify: active false, empty card
        self.set_card("default", sid, None)
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        self.assertEqual(payload["clarify"],
                         {"active": False, "id": "", "html": ""})

        # upstream 500: the feed omits the field, the page still works
        # (the shipped client script manages any card, so the honest
        # target is the card element, never the script's own strings)
        StubCoreHandler.MODE = "fail"
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        self.assertNotIn("clarify", payload)
        status, body = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertNotIn('id="clarify-card"', body)

        # bad key (401): same immunity
        StubCoreHandler.MODE = "unauth"
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        self.assertNotIn("clarify", payload)

        # ambiguous (409): same immunity
        StubCoreHandler.MODE = "ambiguous"
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        self.assertNotIn("clarify", payload)

        # and no key-sourced error text ever reached the client
        self.assertFalse(any("upstream-secret" in b
                             for b in self.responses))

    def test_sequential_clarify_ids_replace_the_card(self):
        sid = "sess_default_d"
        self.add_session(self.db, sid)
        self.set_card("default", sid, {
            "clarify_id": "id-one", "question": "First question?",
            "choices": ["A", "B"], "multi_select": False,
            "source": "native"})
        _s, first = self.feed(sid)
        self.assertEqual(first["clarify"]["id"], "id-one")
        self.assertIn("First question?", first["clarify"]["html"])

        self.set_card("default", sid, {
            "clarify_id": "id-two", "question": "Second question?",
            "choices": None, "multi_select": False,
            "source": "native"})
        _s, second = self.feed(sid)
        self.assertEqual(second["clarify"]["id"], "id-two")
        self.assertIn("Second question?", second["clarify"]["html"])
        self.assertNotIn("First question?", second["clarify"]["html"])

    def test_archived_session_skips_upstream_and_reports_none(self):
        sid = "sess_default_arch"
        self.add_session(self.db, sid, archived=1)
        self.set_card("default", sid, dict(self.HOSTILE_CARD))
        status, payload = self.feed(sid)
        self.assertEqual(status, 200)
        self.assertEqual(payload["clarify"]["active"], False)
        # the upstream was never asked (the stub log stays empty)
        self.assertEqual(self.stub_entries(), [])


class TestPostProxy(ClarifyCase):
    """Contract 3: local validation, the exact proxied payload, the
    safe status set, and never /reply nor a user-message write."""

    def setUp(self):
        super().setUp()
        self.sid = "sess_default_p"
        self.add_session(self.db, self.sid)
        self.set_card("default", self.sid, {
            "clarify_id": "cid-7", "question": "Which deployment?",
            "choices": ["Blue", "Green"], "multi_select": False,
            "source": "api_run"})

    def post_clarify(self, obj, sid=None):
        return self.post("/s/default/%s/clarify" % (sid or self.sid),
                         obj)

    def test_success_proxies_exact_payload_and_resolves(self):
        status, payload = self.post_clarify(
            {"clarify_id": "cid-7", "response": "Blue"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "resolved": True,
                                   "clarify_id": "cid-7"})
        posts = self.stub_entries("POST", "default")
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["authorized"])
        self.assertEqual(posts[0]["path"],
                         "/p/default/api/sessions/%s/clarify" % self.sid)
        self.assertEqual(posts[0]["body"],
                         {"clarify_id": "cid-7", "response": "Blue"})
        # the card is consumed: the next feed poll reports none, and a
        # replayed POST can never resolve twice
        _s, feed = self.feed(self.sid)
        self.assertFalse(feed["clarify"]["active"])
        status, _p = self.post_clarify(
            {"clarify_id": "cid-7", "response": "Blue"})
        self.assertEqual(status, 404)

    def test_never_writes_a_user_message(self):
        status, _p = self.post_clarify(
            {"clarify_id": "cid-7", "response": "Green"})
        self.assertEqual(status, 200)
        self.assertEqual(self.message_count(self.db, self.sid), 0)

    def test_local_validation_is_400_and_never_proxied(self):
        before = len(self.stub_entries("POST"))
        bad_bodies = [
            {"clarify_id": "cid-7", "response": 123},
            {"clarify_id": "cid-7", "response": []},
            {"clarify_id": "cid-7", "response": ["ok", ""]},
            {"clarify_id": "cid-7", "response": None},
            {"clarify_id": "", "response": "Blue"},
            {"clarify_id": "x" * 129, "response": "Blue"},
            {"clarify_id": 7, "response": "Blue"},
            {"response": "Blue"},
            {"clarify_id": "cid-7"},
        ]
        for obj in bad_bodies:
            status, payload = self.post_clarify(obj)
            self.assertEqual(status, 400, "body %r" % (obj,))
            self.assertEqual(payload["error"], "bad request body")
        # malformed JSON / wrong shape
        status, _b = self.request("POST",
                                  "/s/default/%s/clarify" % self.sid,
                                  "not-json-object",
                                  token=self._token())
        self.assertEqual(status, 400)
        self.assertEqual(len(self.stub_entries("POST")), before)

    def _token(self):
        status, page = self.request("GET", "/new")
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="mission-control-csrf"'
                      r' content="([^"]*)"', page)
        self.assertIsNotNone(m)
        return m.group(1)

    def test_unknown_session_and_unknown_profile(self):
        status, payload = self.post_clarify(
            {"clarify_id": "cid-7", "response": "Blue"},
            sid="no_such_session")
        self.assertEqual(status, 404)
        status, _b = self.post(
            "/s/nosuch/%s/clarify" % self.sid,
            {"clarify_id": "cid-7", "response": "Blue"})
        self.assertEqual(status, 404)
        self.assertEqual(self.stub_entries("POST"), [])

    def test_archived_session_is_409_without_proxying(self):
        self.add_session(self.db, "sess_closed", archived=1)
        self.set_card("default", "sess_closed", {
            "clarify_id": "cid-9", "question": "?", "choices": None,
            "multi_select": False, "source": "native"})
        status, payload = self.post(
            "/s/default/sess_closed/clarify",
            {"clarify_id": "cid-9", "response": "yes"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "the session is closed")
        self.assertEqual(self.stub_entries("POST"), [])

    def test_missing_csrf_token_is_refused_before_any_proxy(self):
        status, payload = self.request_json(
            "POST", "/s/default/%s/clarify" % self.sid,
            {"clarify_id": "cid-7", "response": "Blue"})
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.stub_entries(), [])

    def test_upstream_verdicts_map_to_the_safe_set(self):
        def rearm():
            # every phase starts from a live card again: a resolved
            # card is consumed upstream, and each verdict below must
            # be exercised against a genuinely pending clarify
            self.set_card("default", self.sid, {
                "clarify_id": "cid-7", "question": "?", "choices": None,
                "multi_select": False, "source": "native"})

        # a stale clarify_id: the core's 404 maps to a local 404
        rearm()
        status, payload = self.post_clarify(
            {"clarify_id": "wrong-id", "response": "Blue"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "no pending clarify")

        # a single-select list passes local shape checks but the core
        # rejects it: upstream 400 -> local 400
        rearm()
        status, payload = self.post_clarify(
            {"clarify_id": "cid-7", "response": ["Blue", "Green"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid clarify response")

        # auth failure and upstream 5xx are availability problems: 503
        for mode in ("unauth", "fail"):
            StubCoreHandler.MODE = mode
            rearm()
            status, payload = self.post_clarify(
                {"clarify_id": "cid-7", "response": "Blue"})
            self.assertEqual(status, 503, mode)
            self.assertEqual(payload["error"],
                             "clarify upstream unavailable")
        StubCoreHandler.MODE = "ok"

        # ambiguity: the core's fail-closed 409 maps to a local 409
        StubCoreHandler.MODE = "ambiguous"
        rearm()
        status, payload = self.post_clarify(
            {"clarify_id": "cid-7", "response": "Blue"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "clarify not pending")

        # and no upstream error body text ever crossed over
        self.assertFalse(any("upstream-secret" in b
                             for b in self.responses))


class TestProfileKeySourcing(ClarifyCase):
    """Contract 1: the default key from the main home's .env, a named
    profile's key ONLY from its own .env — proved by the stub's
    per-profile Bearer check, without printing any key."""

    def test_named_profile_uses_its_own_key_and_prefix(self):
        sid = "sess_helper_a"
        self.add_session(self.helper_db, sid)
        self.set_card("helper", sid, {
            "clarify_id": "hcard", "question": "Which snack?",
            "choices": ["Honey", "Berries"], "multi_select": False,
            "source": "native"})
        status, payload = self.feed(sid, profile="helper")
        self.assertEqual(status, 200)
        self.assertTrue(payload["clarify"]["active"])
        gets = self.stub_entries("GET", "helper")
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["path"],
                         "/p/helper/api/sessions/%s/clarify" % sid)
        # the stub only authorizes helper's own key here — a default
        # (or missing) key would have yielded authorized=False + 401
        self.assertTrue(gets[0]["authorized"],
                        "helper requests must carry helper's own key")

    def test_profile_without_own_env_has_no_key_and_no_request(self):
        sid = "sess_scout_a"
        self.add_session(self.scout_db, sid)
        self.set_card("scout", sid, {
            "clarify_id": "rcard", "question": "?", "choices": None,
            "multi_select": False, "source": "native"})
        # the feed works, omits the clarify field, and never asked the
        # core (no key -> no call; the default key must NOT be used)
        status, payload = self.feed(sid, profile="scout")
        self.assertEqual(status, 200)
        self.assertNotIn("clarify", payload)
        self.assertEqual(self.stub_entries(), [])
        # the POST is an availability problem, also without a call
        status, _p = self.post(
            "/s/scout/%s/clarify" % sid,
            {"clarify_id": "rcard", "response": "yes"})
        self.assertEqual(status, 503)
        self.assertEqual(self.stub_entries(), [])

    def test_no_key_value_ever_reaches_a_client(self):
        sid = "sess_default_k"
        self.add_session(self.db, sid)
        self.set_card("default", sid, {
            "clarify_id": "kcard", "question": "?", "choices": ["Y"],
            "multi_select": False, "source": "native"})
        self.request("GET", "/s/default/" + sid)
        self.feed(sid)
        self.post("/s/default/%s/clarify" % sid,
                  {"clarify_id": "kcard", "response": "Y"})
        for body in self.responses:
            self.assertNotIn(DEFAULT_KEY, body)
            self.assertNotIn(HELPER_KEY, body)


class TestCardContract(ClarifyCase):
    """Contract 4-5: the shipped markup and client behavior — single,
    multi + UI-only Other, open text, the sequential-id rule, the
    composer state machine and the failure re-poll."""

    def page(self):
        sid = "sess_default_ui"
        self.add_session(self.db, sid)
        status, body = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        return body

    def test_single_card_markup(self):
        html = self.mod.render_clarify_card({
            "clarify_id": "s1", "question": "One choice?",
            "choices": ["A (Recommended)", "B"], "multi_select": False})
        self.assertIn('data-multi="0"', html)
        self.assertIn('data-value="A (Recommended)"', html)
        # single-select has no card-level Submit (choices self-submit)
        self.assertNotIn("clarify-submit", html)
        # Other is present but marked UI-only (no data-value)
        self.assertIn("clarify-other-toggle", html)
        self.assertNotIn('data-value="Other"', html)
        # the open-text box starts hidden behind the Other toggle
        self.assertIn('<div class="clarify-other-box" hidden>', html)
        self.assertIn("clarify-other-send", html)

    def test_multi_card_markup(self):
        html = self.mod.render_clarify_card({
            "clarify_id": "m1", "question": "Pick many?",
            "choices": ["A", "B"], "multi_select": True})
        self.assertIn('data-multi="1"', html)
        self.assertIn("clarify-submit", html)
        # multi-select answers go through Submit: the other box holds
        # just the input, no second submit button
        self.assertNotIn("clarify-other-send", html)

    def test_open_text_card_markup(self):
        html = self.mod.render_clarify_card({
            "clarify_id": "o1", "question": "Say anything:",
            "choices": None, "multi_select": False})
        self.assertNotIn("clarify-choice", html)
        self.assertNotIn("clarify-submit", html)
        # the input is visible from the start (nothing to reveal)
        self.assertIn('<div class="clarify-other-box">', html)
        self.assertIn("clarify-other-send", html)

    def test_client_source_carries_the_behavior_contract(self):
        body = self.page()
        # single regular choice submits immediately
        self.assertIn(
            'submitClarify(btn.getAttribute("data-value"));', body)
        # multi gathers ONLY data-value toggles plus the typed Other
        # text — the label "Other" has no data-value and never sends
        self.assertIn(
            '.clarify-choice[data-value][aria-pressed="true"]', body)
        self.assertIn("vals.push(text);", body)
        # same clarify id preserves the card, a new one replaces it
        self.assertIn(
            'getAttribute("data-clarify-id") === cl.id', body)
        # the card freezes while its answer is on the wire
        self.assertIn("setClarifyDisabled(cardEl, true);", body)
        # failures flash safely and re-poll
        self.assertIn("window.setTimeout(pollOnce, 400);", body)
        # the composer disables behind the card with the fixed phrase
        self.assertIn('CLARIFY_PLACEHOLDER = "Answer the question above"',
                      body)
        self.assertIn("box.disabled = archived || sending || clarifyActive",
                      body)
        # answers go to the clarify proxy through the CSRF-bearing
        # postJson helper, never a bare fetch and never /reply
        self.assertIn('sessionUrl("/clarify")', body)
        self.assertIn("postJson(sessionUrl(\"/clarify\")", body)

    def test_composer_restores_when_card_disappears(self):
        body = self.page()
        # applyClarify's inactive branch hands the composer back via
        # the session state (which keeps archived/sending authority)
        self.assertIn("clarifyActive = false;", body)
        self.assertIn("applySessionState({ archived: archived });",
                      body)
        # and the normal composer's own placeholder logic survives
        self.assertIn("openPlaceholder", body)


class TestLoadEnvValue(ClarifyCase):
    """The .env reader that feeds both the Discord token and the
    per-profile clarify keys: line-wise KEY=VALUE parsing with quote
    stripping, never any interpretation beyond the asked-for name. An
    empty value reads as None (absent), exactly like a missing key."""

    def test_plain_and_quoted_values(self):
        env = os.path.join(self.tmp, "env-a", ".env")
        os.makedirs(os.path.dirname(env), exist_ok=True)
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("# comment line\n"
                     "PLAIN=abc123\n"
                     'DOUBLE="quoted value"\n'
                     "SINGLE='single value'\n"
                     "SPACED = padded \n"
                     "URL=https://example.test/a?b=c\n"
                     "\n"
                     "NOVALUE=\n")
        load = self.mod.load_env_value
        self.assertEqual(load(env, "PLAIN"), "abc123")
        self.assertEqual(load(env, "DOUBLE"), "quoted value")
        self.assertEqual(load(env, "SINGLE"), "single value")
        self.assertEqual(load(env, "SPACED"), "padded")
        # '=' inside the value survives (split on the first only)
        self.assertEqual(load(env, "URL"),
                         "https://example.test/a?b=c")
        self.assertIsNone(load(env, "NOVALUE"))
        self.assertIsNone(load(env, "MISSING"))
        self.assertIsNone(load(env, "comment"))

    def test_missing_file_is_none_without_raising(self):
        self.assertIsNone(self.mod.load_env_value(
            os.path.join(self.tmp, "nope", ".env"), "API_SERVER_KEY"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
