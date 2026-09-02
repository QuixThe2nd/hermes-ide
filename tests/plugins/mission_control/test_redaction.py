#!/usr/bin/env python3
"""Secrets never reach activity or transcript surfaces.

One redaction boundary covers every UI-exposed tool argument summary
and tool-result detail. These tests seed a session whose pending tool
call arguments and completed tool results carry recognizable sentinel
credentials — a Bearer token, api_key/password assignments (bare and
quoted), a credential-bearing URL, a DB URI, a Basic header — then
assert the raw sentinels are absent from the rendered page HTML, the
feed JSON and the live-activity strip, while useful non-secret text
survives. The matcher itself is unit-tested for value-completeness:
masking only the scheme word ("Bearer") while leaving the credential
is exactly the failure mode these tests forbid.
"""

import importlib.util
import itertools
import json
import os
import shutil
import sqlite3
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

SESSION_SCHEMA = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  display_name TEXT,
  started_at REAL NOT NULL,
  ended_at REAL,
  end_reason TEXT,
  last_activity_at REAL,
  archived INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  cwd TEXT,
  thread_id TEXT,
  parent_session_id TEXT
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_name TEXT,
  tool_call_id TEXT,
  tool_calls TEXT,
  codex_message_items TEXT,
  timestamp REAL NOT NULL,
  finish_reason TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  display_kind TEXT
);
"""

# Every sentinel credential shape one tool exchange can carry. Each is
# recognizable and unique; the assertions are "absent from everything
# the server returns".
BEARER = "sk-live-9f8e7d6c5b4a3210SENTINEL"
PLAIN_KEY = "SENTINEL-plain-key-556677"
QUOTED_PASS = "hunter two sentinel words"
URI_PASS = "SENTINEL-uri-password-99"
BASIC_B64 = "c2VudGluZWxJc2FvbWU6cGFzcw=="

TOOL_RESULT = (
    "HTTP/1.1 200 OK\n"
    "Authorization: Bearer %s\n"
    "x-api-key: %s\n"
    "api_key=%s\n"
    'password: "%s"\n'
    "postgres://alice:%s@db.internal:5432/prod\n"
    "Authorization: Basic %s\n"
    "GET /v1/tasks?status=open returned 3 rows\n"   # useful, non-secret
    "see /var/log/hermes/tasks.log for details\n"    # useful, non-secret
    % (BEARER, PLAIN_KEY, PLAIN_KEY, QUOTED_PASS, URI_PASS, BASIC_B64))

# The pending call's arguments: parsed JSON with nested credential
# keys and textual forms — what summarize_arguments renders.
PENDING_ARGS = json.dumps({
    "workdir": "/repo",
    "headers": {"Authorization": "Bearer %s" % BEARER,
                "x-api-key": PLAIN_KEY},
    "url": "postgres://bob:%s@db.internal:5432/prod" % URI_PASS,
    "note": "fetch the open tasks list",
})

TOOL_CALLS = json.dumps([
    {"id": "call_resolved", "function": {"name": "http",
                                         "arguments": "{}"}},
    {"id": "call_pending", "function": {"name": "http",
                                        "arguments": PENDING_ARGS}},
])


def load_server(tmp, db_path):
    """One isolated server.py module instance per test."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_redact_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class RedactionCase(unittest.TestCase):
    """One session holding the sentinel-laden tool exchange, served
    over real HTTP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redact-test-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES ('sess_sec','cli','secret fixture',?,?,0,0)",
            (now - 120, now - 5))
        rows = [
            ("user", "please fetch the tasks list", now - 120,
             None, None, None),
            ("assistant", "", now - 110, None, TOOL_CALLS, None),
            ("tool", TOOL_RESULT, now - 100, "http", None,
             "call_resolved"),
            ("assistant", "fetched 3 tasks; see the log", now - 5,
             None, None, None),
        ]
        for role, content, ts, tname, tcalls, tcid in rows:
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " tool_name, tool_calls, tool_call_id, timestamp)"
                " VALUES (?,?,?,?,?,?,?)",
                ("sess_sec", role, content, tname, tcalls, tcid, ts))
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def fetch(self, path):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path),
                    timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    SENTINELS = (BEARER, PLAIN_KEY, QUOTED_PASS, URI_PASS, BASIC_B64)


class TestSurfacesStayClean(RedactionCase):
    """Page HTML, feed JSON, activity strip: no sentinel anywhere."""

    def test_page_and_feed_hide_every_sentinel(self):
        status, page = self.fetch("/s/default/sess_sec")
        self.assertEqual(status, 200)
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, page, sentinel)
        status, body = self.fetch("/s/default/sess_sec/feed?after=0")
        self.assertEqual(status, 200)
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, body, sentinel)

    def test_redaction_marker_present_and_useful_text_kept(self):
        _status, page = self.fetch("/s/default/sess_sec")
        self.assertIn("[REDACTED]", page)
        # the surrounding structure of each redaction survives: the
        # keyword and separator stay, the whole value (scheme word and
        # quoted phrases included) is consumed
        self.assertIn("Authorization: [REDACTED]", page)
        self.assertIn("x-api-key: [REDACTED]", page)
        self.assertIn("api_key=[REDACTED]", page)
        self.assertIn("password: [REDACTED]", page)
        self.assertIn("postgres://[REDACTED]@db.internal:5432", page)
        # useful non-secret detail is preserved
        self.assertIn("GET /v1/tasks?status=open returned 3 rows", page)
        self.assertIn("/var/log/hermes/tasks.log", page)
        self.assertIn("fetch the open tasks list", page)
        self.assertIn("fetched 3 tasks; see the log", page)

    def test_pending_call_argument_summary_is_redacted(self):
        """The unresolved call's argument summary — the live-activity
        strip's args preview — carries the redacted forms only."""
        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        try:
            act = self.mod.compute_activity(con, "sess_sec",
                                            time.time(), True)
        finally:
            con.close()
        pending = [p for p in act.get("pending") or []
                   if p.get("name") == "http"]
        self.assertTrue(pending, "the pending call is visible")
        rendered = self.mod.render_activity(act)
        for surface in [json.dumps(act), rendered,
                        pending[0].get("args", "")]:
            for sentinel in self.SENTINELS:
                self.assertNotIn(sentinel, surface, sentinel)
        self.assertIn("[REDACTED]", rendered)
        # the non-secret argument fields survive the summary
        self.assertIn("workdir=/repo", pending[0]["args"])


class TestMatcherShapes(unittest.TestCase):
    """The boundary itself: whole values, not first tokens."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redact-unit-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mod = load_server(self.tmp, os.path.join(self.tmp,
                                                      "state.db"))

    def r(self, text):
        return self.mod.redact_secret_text(text)

    def test_bearer_credential_is_fully_masked(self):
        out = self.r("Authorization: Bearer %s" % BEARER)
        self.assertNotIn(BEARER, out)
        self.assertNotIn("Bearer sk", out)  # not merely the scheme+prefix
        self.assertIn("[REDACTED]", out)

    def test_key_value_forms_mask_whole_values(self):
        for text, needle in (
            ("api_key = %s" % PLAIN_KEY, PLAIN_KEY),
            ('token: "%s"' % PLAIN_KEY, PLAIN_KEY),
            ("password=%s," % PLAIN_KEY, PLAIN_KEY),
            ("X-Secret: %s" % PLAIN_KEY, PLAIN_KEY),
            ("github_token: %s" % PLAIN_KEY, PLAIN_KEY),
            ('password: "%s"' % QUOTED_PASS, QUOTED_PASS),
        ):
            out = self.r(text)
            self.assertNotIn(needle, out, text)
            self.assertIn("[REDACTED]", out, text)
        # a quoted phrase is one match: no leaked remainder words
        out = self.r('password: "%s"' % QUOTED_PASS)
        self.assertNotIn("hunter", out)
        self.assertNotIn("words", out)

    def test_credential_urls_and_db_uris_mask_userinfo(self):
        for uri in ("postgres://alice:%s@db.internal:5432/prod"
                    % URI_PASS,
                    "mysql://root:%s@127.0.0.1/prod" % URI_PASS,
                    "https://bob:%s@example.com/path" % URI_PASS):
            out = self.r(uri)
            self.assertNotIn(URI_PASS, out, uri)
            self.assertIn("[REDACTED]@", out, uri)
        # credential-free URLs pass whole
        clean = "https://example.com/path?query=1"
        self.assertEqual(self.r(clean), clean)

    def test_hyphenated_key_forms_mask_whole_values(self):
        for text in ("x-api-key: %s" % PLAIN_KEY,
                     "api-key=%s" % PLAIN_KEY,
                     "session-secret: %s" % PLAIN_KEY,
                     "GITHUB_TOKEN=%s" % PLAIN_KEY):
            out = self.r(text)
            self.assertNotIn(PLAIN_KEY, out, text)
            self.assertIn("[REDACTED]", out, text)

    def test_basic_and_bare_bearer_forms(self):
        out = self.r("Authorization: Basic %s" % BASIC_B64)
        self.assertNotIn(BASIC_B64, out)
        out = self.r("bearer %s" % BEARER)   # bare, no keyword
        self.assertNotIn(BEARER, out)
        self.assertIn("[REDACTED]", out)

    def test_non_secret_text_passes_untouched(self):
        for text in ("read the file /etc/hosts and report",
                     "tokenization of the corpus worked",
                     "authorized personnel only",
                     "the proxy-authorization layer is not in play"):
            self.assertEqual(self.r(text), text)

    def test_parsed_argument_objects_redact_by_key(self):
        sentinels = (BEARER, PLAIN_KEY, QUOTED_PASS, URI_PASS)
        obj = {"headers": {"Authorization": "Bearer %s" % BEARER,
                           "x-api-key": PLAIN_KEY},
               "db_url": "postgres://u:%s@h/db" % URI_PASS,
               "path": "/etc/hosts",
               "nested": [{"password": QUOTED_PASS}]}
        out = json.dumps(self.mod.redact_secrets(obj))
        for sentinel in sentinels:
            self.assertNotIn(sentinel, out)
        self.assertIn("/etc/hosts", out)
        summary = self.mod.summarize_arguments(json.dumps(obj))
        for sentinel in sentinels:
            self.assertNotIn(sentinel, summary)


if __name__ == "__main__":
    unittest.main()
