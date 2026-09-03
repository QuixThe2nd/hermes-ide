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
import http.client
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
    state.db, with the core API client patched to a recorder that
    answers like the real gateway and fails the test if a forged
    request ever reaches it. Discord is patched to a recorder too."""

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

        self.core_calls = []
        self._core = unittest.mock.patch.object(
            self.mod, "core_api_request", side_effect=self._record_core)
        self._core.start()
        self.addCleanup(self._core.stop)

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

    def _record_core(self, method, path, profile, dbs, payload=None,
                     timeout=None):
        """The fake core gateway: records the call (method and path
        only — never a payload that could carry the prompt) and answers
        the shapes the transport expects."""
        self.core_calls.append((method, path))
        if method == "POST" and path == "/v1/runs":
            sid = (payload or {}).get("session_id") or "run_fake0x"
            return 202, {"run_id": "run_fake0x", "status": "started",
                         "session_id": sid, "replayed": False}, None
        if method == "GET" and path.startswith("/v1/runs/"):
            return 200, {"run_id": "run_fake0x", "status": "completed",
                         "session_id": "run_fake0x"}, None
        return 200, {"pending_clarify": None}, None

    def _record_discord(self, method, path, token, payload=None):
        self.discord_calls.append((method, path))
        return 200, {"thread_metadata": {"archived": True}}, None

    # ---- fixture helpers -------------------------------------------

    def call_count(self):
        return len(self.core_calls)

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
        with the mined token. A composer turn admits exactly one core
        run — never a duplicate — and the local close/reopen flip is
        real."""
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
        # exactly one admission for the launch (status polls aside)
        self.assertEqual(
            self.core_calls.count(("POST", "/v1/runs")), 1,
            self.core_calls)
        # sess_local was closed above; reopen it so the reply is judged
        # on its own merits (a closed session would 409 before any
        # admission, which is a different assertion).
        status, _ = self.post("/s/default/sess_local/reopen", "{}",
                              "application/json", token=token)
        self.assertEqual(status, 200)
        status, _ = self.post("/s/default/sess_local/reply",
                              '{"text": "%s"}' % PROMPT,
                              "application/json", token=token)
        self.assertIn(status, (202, 409))
        self.assertEqual(
            self.core_calls.count(("POST", "/v1/runs")), 2,
            self.core_calls)

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


class TrustedProxyCase(ForgeryCase):
    """Origin validation behind an operator-trusted HTTPS reverse proxy.

    The server below is plain HTTP on an ephemeral loopback port, with
    one extra trusted host standing in for the proxy's public name. A
    browser loaded from the proxy sends Origin https://<that host> (no
    port — the TLS default); the direct-access rule would refuse it
    because the scheme is not http and 443 is not the bound port. The
    trusted-proxy shape must accept exactly that pairing and refuse
    every neighbouring shape an attacker can actually produce.
    """

    PROXY_HOST = "mission-control.internal"

    def setUp(self):
        super().setUp()
        # The operator listed the proxy's public host with
        # --trusted-host; pin the same set onto the test server.
        self.httpd.trusted_hosts = (
            set(self.mod._default_trusted_hosts("127.0.0.1"))
            | {self.PROXY_HOST})

    def gate_post(self, host, origin=None, referer=None, extra=None):
        """One POST /s/new with full header control -> status.

        Token and content type are always the genuine UI shape, so the
        only thing under test is the Host/Origin/Referer decision: a
        403 or 421 answer means the gate refused, anything else means
        the request reached the route (which then answers on its own
        merits)."""
        headers = {"Content-Type": "application/json",
                   "X-CSRF-Token": self.csrf_token()}
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        for key, value in (extra or {}).items():
            headers[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        try:
            conn.request("POST", "/s/new",
                         body=json.dumps({"text": "proxy-probe"}),
                         headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def test_trusted_https_public_origin_passes(self):
        for origin in ("https://%s" % self.PROXY_HOST,
                       "https://%s:443" % self.PROXY_HOST):
            status, body = self.gate_post(self.PROXY_HOST, origin=origin)
            self.assertNotIn(status, (403, 415, 421),
                             "%s -> %d %r" % (origin, status, body))

    def test_trusted_https_referer_passes(self):
        status, body = self.gate_post(
            self.PROXY_HOST, referer="https://%s/s/default/abc" %
            self.PROXY_HOST)
        self.assertNotIn(status, (403, 415, 421),
                         "referer -> %d %r" % (status, body))

    def test_non_default_https_port_is_refused(self):
        status, _ = self.gate_post(self.PROXY_HOST,
                                   origin="https://%s:8443" %
                                   self.PROXY_HOST)
        self.assertEqual(status, 403)

    def test_http_origin_on_proxy_host_is_refused(self):
        # The direct-access shape needs the bound port; a plain-http
        # origin under the proxy's name is not that shape and not the
        # trusted-proxy shape either.
        for origin in ("http://%s" % self.PROXY_HOST,
                       "http://%s:80" % self.PROXY_HOST):
            status, _ = self.gate_post(self.PROXY_HOST, origin=origin)
            self.assertEqual(status, 403, origin)

    def test_mismatched_and_unknown_hosts_are_refused(self):
        # Origin names some other host than the (trusted) Host.
        status, _ = self.gate_post(self.PROXY_HOST,
                                   origin="https://evil.example")
        self.assertEqual(status, 403)
        # Host itself untrusted: refused before Origin is even read,
        # whatever the Origin and whatever a forwarded header claims.
        status, _ = self.gate_post("evil.example",
                                   origin="https://evil.example",
                                   extra={"X-Forwarded-Host":
                                          self.PROXY_HOST})
        self.assertEqual(status, 421)
        status, _ = self.gate_post("evil.example",
                                   origin="https://%s" % self.PROXY_HOST,
                                   extra={"X-Forwarded-Proto": "https",
                                          "Forwarded":
                                          "host=%s;proto=https" %
                                          self.PROXY_HOST})
        self.assertEqual(status, 421)

    def test_forwarded_headers_cannot_smuggle_an_origin_through(self):
        # A request that arrives with the backend's own http origin but
        # claims to be forwarded for the proxy must not be upgraded by
        # that claim alone: the Origin itself decides, and an https
        # origin for an unlisted host stays refused.
        status, _ = self.gate_post(
            self.PROXY_HOST, origin="https://other.internal",
            extra={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 403)

    def test_malformed_and_credential_bearing_origins_are_refused(self):
        for origin in ("https://user:pass@%s" % self.PROXY_HOST,
                       "ftp://%s" % self.PROXY_HOST,
                       "https://%s:port" % self.PROXY_HOST,
                       "https://",
                       "javascript://test"):
            status, _ = self.gate_post(self.PROXY_HOST, origin=origin)
            self.assertEqual(status, 403, origin)


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
