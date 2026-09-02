#!/usr/bin/env python3
"""Browser-origin request forgery cannot mutate anything.

Every state-changing route (/s/new, reply, close, reopen) is gated
before its body is parsed: a same-origin Origin, an exactly-
application/json content type, and the per-process CSRF token in the
non-simple X-CSRF-Token header. These tests drive the real HTTP
handler with browser-simple shapes — a urlencoded HTML form, a
text/plain body, JSON with a missing or wrong token — and prove each
is refused (403/415) with no Hermes launch, no SQLite write and no
Discord call, while the real UI shape still succeeds. The token itself
must never ride a response body or the request log, and each server
process mints its own.

Also here: the bind classifier. An empty bind host is a wildcard, not
loopback; only the explicit loopback names count.
"""

import importlib.util
import io
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

_MODULE_SEQ = itertools.count()
PROMPT = "csrf-probe-marker-77e1"

# The production schema, imported from core: the listing is now served
# by the core projection (list_sessions_rich), so fixture DBs must
# answer exactly the SQL the live ones do — the synthetic subset below
# predated that and lacks the columns the projection reads.
sys.path.insert(0, REPO)

from hermes_state_common import SCHEMA_SQL  # noqa: E402

SESSION_SCHEMA = SCHEMA_SQL

# A stub hermes that only records that it ran (never its arguments):
# the forgery tests assert it is never launched at all.
STUB = '''#!/usr/bin/env python3
import os, sys
with open(os.path.join(%(dir)r, "calls"), "a") as fh:
    fh.write("CALL\\n")
sys.exit(0)
'''


def load_server(tmp, db_path):
    """One isolated server.py module instance per test."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_csrf_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class ForgeryCase(unittest.TestCase):
    """A real ThreadingHTTPServer on an ephemeral port over a synthetic
    state.db, with the hermes binary stubbed and Discord patched to a
    recorder that fails the test if the server ever calls it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csrf-test-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden, thread_id)"
            " VALUES ('sess_local','cli','local fixture',?,?,0,0,NULL)",
            (now - 60, now - 5))
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden, thread_id)"
            " VALUES ('sess_discord','discord','discord fixture',?,?,0,0,"
            " '1234567890123456789')",
            (now - 60, now - 5))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('sess_local','user','hi',?)", (now - 60,))
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)

        stub = os.path.join(self.tmp, "hermes-stub")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB % {"dir": self.tmp})
        os.chmod(stub, 0o755)
        self.mod.HERMES_BIN = stub

        self.discord_calls = []
        self._discord = unittest.mock.patch.object(
            self.mod, "discord_request",
            side_effect=self._record_discord)
        self._discord.start()
        self.addCleanup(self._discord.stop)
        self._token_src = unittest.mock.patch.object(
            self.mod, "load_discord_token", return_value="fixture-token")
        self._token_src.start()
        self.addCleanup(self._token_src.stop)

        self._csrf = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.mod.terminate_children)

    def _record_discord(self, method, path, token, payload=None):
        self.discord_calls.append((method, path))
        return 200, {"thread_metadata": {"archived": True}}, None

    # ---- fixture helpers -------------------------------------------

    def call_count(self):
        try:
            with open(os.path.join(self.tmp, "calls")) as fh:
                return len([ln for ln in fh if ln.startswith("CALL")])
        except OSError:
            return 0

    def db_snapshot(self):
        """Full ordered dump of both tables — the no-SQLite-write
        proof compares these exactly."""
        out = []
        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        try:
            for table in ("sessions", "messages"):
                out.append(con.execute(
                    "SELECT * FROM %s ORDER BY 1, 2" % table).fetchall())
        finally:
            con.close()
        return out

    # ---- HTTP helpers ----------------------------------------------

    def csrf_token(self):
        """The token mined the way the shipped client mines it: from a
        page this process served."""
        if self._csrf is None:
            _status, page = self.get("/new")
            m = re.search(
                r'<meta name="mission-control-csrf" content="([^"]*)"',
                page)
            self.assertIsNotNone(m, "served page carries the CSRF meta")
            self._csrf = m.group(1)
            self.assertGreaterEqual(len(self._csrf), 32)
        return self._csrf

    def get(self, path):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path),
                    timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def post(self, path, body, content_type, token="__unset__",
             origin="same"):
        """One raw POST with full control of the forgery-relevant
        headers (origin="same" sends this server's own Origin)."""
        headers = {"Content-Type": content_type}
        if origin == "same":
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
        elif origin is not None:
            headers["Origin"] = origin
        if token != "__unset__":
            headers["X-CSRF-Token"] = token
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=body.encode("utf-8") if isinstance(body, str) else body,
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


class TestForgeryRejected(ForgeryCase):
    """Browser-simple requests to every POST route change nothing."""

    ROUTES = (
        ("/s/new", '{"text": "%s"}' % PROMPT),
        ("/s/default/sess_local/reply", '{"text": "%s"}' % PROMPT),
        ("/s/default/sess_local/close", "{}"),
        ("/s/default/sess_local/reopen", "{}"),
        # a Discord-thread session: the close path would patch Discord
        ("/s/default/sess_discord/close", "{}"),
        ("/s/default/sess_discord/reopen", "{}"),
    )

    FORM_BODY = "text=" + PROMPT

    def forged_shapes(self):
        """(label, kwargs) for every browser-simple request shape."""
        return [
            ("html-form+foreign-origin", dict(
                body=self.FORM_BODY,
                content_type="application/x-www-form-urlencoded",
                origin="http://evil.example")),
            ("html-form+no-origin", dict(
                body=self.FORM_BODY,
                content_type="application/x-www-form-urlencoded",
                origin=None)),
            ("multipart-form+foreign-origin", dict(
                body="--x\r\nContent-Disposition: form-data; name="
                     "\"text\"\r\n\r\n%s\r\n--x--\r\n" % PROMPT,
                content_type="multipart/form-data; boundary=x",
                origin="http://evil.example:81")),
            ("text-plain+same-origin", dict(
                body=self.FORM_BODY, content_type="text/plain")),
            ("json+foreign-origin", dict(
                body='{"text": "%s"}' % PROMPT,
                content_type="application/json",
                origin="https://attacker.invalid")),
            ("json+same-origin+no-token", dict(
                body='{"text": "%s"}' % PROMPT,
                content_type="application/json")),
            ("json+same-origin+wrong-token", dict(
                body='{"text": "%s"}' % PROMPT,
                content_type="application/json",
                token="guess" * 12)),
        ]

    def test_every_forged_shape_on_every_route_is_refused(self):
        for path, body in self.ROUTES:
            for label, kwargs in self.forged_shapes():
                status, resp = self.post(path, **kwargs)
                self.assertIn(status, (403, 415),
                              "%s on %s: %d %r" % (label, path, status,
                                                   resp))
                self.assertIn('"ok":false', resp.replace(" ", ""))
                # the token never rides a refusal
                self.assertNotIn(self.csrf_token(), resp)
                self.assertNotIn(PROMPT, resp)

    def test_forged_requests_never_launch_write_or_call(self):
        before = self.db_snapshot()
        for path, body in self.ROUTES:
            for _label, kwargs in self.forged_shapes():
                self.post(path, **kwargs)
        self.assertEqual(self.call_count(), 0,
                         "a forged request launched hermes")
        self.assertEqual(self.discord_calls, [],
                         "a forged request reached Discord")
        self.assertEqual(self.db_snapshot(), before,
                         "a forged request mutated SQLite")

    def test_real_ui_requests_still_succeed(self):
        """The same routes accept the real UI shape: same-origin JSON
        with the mined token (the stub answers instantly; the local
        close/reopen flip is real)."""
        token = self.csrf_token()
        status, _ = self.post("/s/default/sess_local/close", "{}",
                              "application/json", token=token)
        self.assertEqual(status, 200)
        status, _ = self.post("/s/default/sess_discord/close", "{}",
                              "application/json", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(self.discord_calls,
                         [("PATCH", "/channels/1234567890123456789")])
        status, _ = self.post("/s/new", '{"text": "%s"}' % PROMPT,
                              "application/json", token=token)
        self.assertIn(status, (202, 409))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.call_count() < 1:
            time.sleep(0.05)
        self.assertEqual(self.call_count(), 1)
        status, _ = self.post("/s/default/sess_local/reply",
                              '{"text": "%s"}' % PROMPT,
                              "application/json", token=token)
        self.assertIn(status, (202, 409))
        self.assertGreaterEqual(self.call_count(), 1)

    def test_refusals_never_log_the_token(self):
        """log_message writes the request line only; the token is not a
        query parameter anywhere, and the log stream stays clean."""
        buf = io.StringIO()
        with unittest.mock.patch("sys.stderr", buf):
            for path, _body in self.ROUTES:
                self.post(path, '{"text": "x"}', "application/json",
                          token="wrong-token-entirely-0123456789")
        self.assertNotIn(self.csrf_token(), buf.getvalue())

    def test_token_is_per_server_process(self):
        other = load_server(self.tmp, self.db)
        self.assertNotEqual(self.csrf_token(), other.csrf_token())


class TestBindClassification(unittest.TestCase):
    """The loopback classifier behind the unauthenticated-bind warning."""

    def test_empty_host_is_not_loopback(self):
        mod = load_server(tempfile.mkdtemp(prefix="csrf-bind-"),
                          "unused.db")
        self.addCleanup(shutil.rmtree, mod.PROFILE_GLOB.split(
            os.sep + "no-such-profile")[0], ignore_errors=True)
        self.assertFalse(mod._is_loopback(""))
        self.assertFalse(mod._is_loopback(None))
        self.assertFalse(mod._is_loopback("   "))

    def test_explicit_loopback_names_are_loopback(self):
        mod = load_server(tempfile.mkdtemp(prefix="csrf-bind-"),
                          "unused.db")
        self.addCleanup(shutil.rmtree, mod.PROFILE_GLOB.split(
            os.sep + "no-such-profile")[0], ignore_errors=True)
        for host in ("127.0.0.1", "127.0.0.0", "127.255.255.255",
                     "::1", "[::1]", "localhost"):
            self.assertTrue(mod._is_loopback(host), host)

    def test_wildcard_and_other_hosts_are_not_loopback(self):
        mod = load_server(tempfile.mkdtemp(prefix="csrf-bind-"),
                          "unused.db")
        self.addCleanup(shutil.rmtree, mod.PROFILE_GLOB.split(
            os.sep + "no-such-profile")[0], ignore_errors=True)
        for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.2",
                     "example.com", "255.255.255.255"):
            self.assertFalse(mod._is_loopback(host), host)


if __name__ == "__main__":
    unittest.main()
