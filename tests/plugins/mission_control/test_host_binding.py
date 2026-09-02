#!/usr/bin/env python3
"""Trusted-Host binding and CSRF origin rules, against a live server.

The server binds one address and answers only for the Host names that
address implies (plus explicit --trusted-host entries). Everything
token-bearing or state-changing is gated on that Host BEFORE any page,
token or route runs, so a DNS-rebinding origin — whose Host AND Origin
both name the attacker, the one shape an origin==host comparison could
never catch — can neither read a CSRF token nor use one. Origin and
Referer must additionally name exactly this server: http scheme,
trusted host, and the port actually bound. Forwarded headers are
client-controlled and never consulted.

Every POST route (/s/new, reply, clarify, close, reopen) is covered for
each refusal class; the allowed class is proven with requests that stop
at the route's own validation (unknown session, empty text) so nothing
launches, plus a real close/reopen round-trip on the fixture DB.
"""

import importlib.util
import ipaddress
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

EVIL = "rebind.example"

POST_ROUTES = (
    "/s/new",
    "/s/default/20260902_120000_deadbeef/reply",
    "/s/default/20260902_120000_deadbeef/clarify",
    "/s/default/20260902_120000_deadbeef/close",
    "/s/default/20260902_120000_deadbeef/reopen",
)


def load_server(tmp, db_path):
    """One isolated server.py module per test, pointed at the fixture
    home. Nothing here launches children or talks to Discord."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_host_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class HostBindingCase(unittest.TestCase):
    """A real HTTP server on an ephemeral loopback port."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-host-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at) VALUES"
            " ('20260902_120000_deadbeef','cli','host fixture', ?, ?)",
            (now - 60, now))
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        # A stub hermes binary: the allowed-class POSTs below must be
        # able to launch their background job without running the real
        # CLI, and the Discord surfaces must stay off the network.
        stub = os.path.join(self.tmp, "hermes-stub")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!%s\nimport sys\nsys.exit(0)\n" % sys.executable)
        os.chmod(stub, 0o755)
        self.mod.HERMES_BIN = stub
        for patcher in (unittest.mock.patch.object(
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

    # ---- HTTP helpers ----------------------------------------------

    def request(self, method, path, obj=None, token=None, host=None,
                origin=None, referer=None, ctype=True, extra=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = dict(extra or {})
        if obj is not None:
            data = (obj if isinstance(obj, bytes)
                    else json.dumps(obj).encode("utf-8"))
            if ctype:
                headers["Content-Type"] = "application/json"
        if token:
            headers[self.mod.CSRF_HEADER] = token
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def csrf_token(self, host=None):
        status, body = self.request("GET", "/new", host=host)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="mission-control-csrf"'
                      r' content="([^"]*)"', body)
        self.assertIsNotNone(m, "no csrf token in /new")
        return m.group(1)

    def good_origin(self):
        return "http://127.0.0.1:%d" % self.port

    def archived_flag(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT archived FROM sessions"
                " WHERE id = '20260902_120000_deadbeef'").fetchone()[0]
        finally:
            con.close()


class TestTokenCannotBeReceivedOrUsed(HostBindingCase):
    """The DNS-rebinding shape: Host and Origin both name the attacker."""

    def test_attacker_host_gets_no_token_bearing_html(self):
        for path in ("/", "/new", "/s/default/20260902_120000_deadbeef"):
            status, body = self.request("GET", path,
                                        host="%s:%d" % (EVIL, self.port))
            self.assertEqual(status, 421, path)
            self.assertNotIn("mission-control-csrf", body, path)

    def test_attacker_host_is_refused_even_with_matching_origin(self):
        # The exact forgery an origin==host comparison used to accept.
        for path in POST_ROUTES:
            status, _body = self.request(
                "POST", path, obj={"text": "x"}, token="anything",
                host="%s:%d" % (EVIL, self.port),
                origin="http://%s:%d" % (EVIL, self.port))
            self.assertEqual(status, 421, path)

    def test_real_token_is_useless_under_attacker_host(self):
        # A token legitimately mined over loopback does not unlock a
        # request whose Host names the attacker.
        token = self.csrf_token()
        self.assertTrue(token)
        for path in POST_ROUTES:
            status, _body = self.request(
                "POST", path, obj={"text": "x"}, token=token,
                host="%s:%d" % (EVIL, self.port),
                origin="http://%s:%d" % (EVIL, self.port))
            self.assertEqual(status, 421, path)
        # Nothing was mutated along the way.
        self.assertEqual(self.archived_flag(), 0)

    def test_forwarded_headers_are_never_trusted(self):
        # X-Forwarded-Host / Forwarded claiming loopback do not rescue
        # an attacker Host, and cannot demote a trusted one either.
        status, _body = self.request(
            "GET", "/new", host="%s:%d" % (EVIL, self.port),
            extra={"X-Forwarded-Host": "127.0.0.1",
                   "Forwarded": "host=localhost"})
        self.assertEqual(status, 421)
        status, _body = self.request(
            "GET", "/new",
            extra={"X-Forwarded-Host": EVIL, "Forwarded": "host=" + EVIL})
        self.assertEqual(status, 200)


class TestOriginRules(HostBindingCase):
    """Origin/Referer must name exactly this server, not the request."""

    def setUp(self):
        super().setUp()
        self.token = self.csrf_token()

    def test_good_origin_passes_the_gate(self):
        # Unknown session: the route's own 404 proves the gate passed,
        # with no side effects to clean up.
        status, _body = self.request(
            "POST", "/s/default/20260902_120000_nosuch/reply",
            obj={"text": "hi"}, token=self.token,
            origin=self.good_origin())
        self.assertEqual(status, 404)

    def test_evil_origin_is_refused_on_every_post_route(self):
        for path in POST_ROUTES:
            status, _body = self.request(
                "POST", path, obj={"text": "x"}, token=self.token,
                origin="http://%s:%d" % (EVIL, self.port))
            self.assertEqual(status, 403, path)

    def test_wrong_port_origin_is_refused(self):
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token=self.token,
            origin="http://127.0.0.1:%d" % (self.port + 1))
        self.assertEqual(status, 403)

    def test_portless_origin_is_refused(self):
        # An absent port means port 80; this server is not on 80.
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token=self.token,
            origin="http://127.0.0.1")
        self.assertEqual(status, 403)

    def test_https_origin_is_refused(self):
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token=self.token,
            origin="https://127.0.0.1:%d" % self.port)
        self.assertEqual(status, 403)

    def test_origin_must_not_match_host_blindly(self):
        # Trusted Host + an Origin naming a DIFFERENT trusted host with
        # the right port is fine; the point of this pair is that the
        # Origin check is against the server, evaluated independently
        # of the (already trusted) Host. The stub binary makes the
        # accepted launch harmless.
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token=self.token,
            origin="http://localhost:%d" % self.port)
        self.assertEqual(status, 202)  # gate passed; launch accepted

    def test_referer_held_to_same_rule_when_origin_absent(self):
        status, _body = self.request(
            "POST", "/s/default/20260902_120000_nosuch/reply",
            obj={"text": "hi"}, token=self.token,
            referer="http://127.0.0.1:%d/s/default/x" % self.port)
        self.assertEqual(status, 404)
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token=self.token,
            referer="http://%s/attack" % EVIL)
        self.assertEqual(status, 403)

    def test_wrong_content_type_is_refused(self):
        status, _body = self.request(
            "POST", "/s/new", obj="text=x", token=self.token,
            origin=self.good_origin(), ctype=False,
            extra={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)

    def test_wrong_token_is_refused(self):
        status, _body = self.request(
            "POST", "/s/new", obj={"text": "x"}, token="forged",
            origin=self.good_origin())
        self.assertEqual(status, 403)


class TestEveryPostRouteGate(HostBindingCase):
    """One pass over the gate matrix for each state-changing route."""

    def setUp(self):
        super().setUp()
        self.token = self.csrf_token()

    def test_refusal_matrix_per_route(self):
        for path in POST_ROUTES:
            with self.subTest(route=path):
                # Attacker Host + matching attacker Origin -> 421.
                self.assertEqual(self.request(
                    "POST", path, obj={"text": "x"}, token=self.token,
                    host="%s:%d" % (EVIL, self.port),
                    origin="http://%s:%d" % (EVIL, self.port))[0], 421)
                # Trusted Host, attacker Origin -> 403.
                self.assertEqual(self.request(
                    "POST", path, obj={"text": "x"}, token=self.token,
                    origin="http://%s:%d" % (EVIL, self.port))[0], 403)
                # Trusted everything, forged token -> 403.
                self.assertEqual(self.request(
                    "POST", path, obj={"text": "x"}, token="forged",
                    origin=self.good_origin())[0], 403)
                # No token at all -> 403.
                self.assertEqual(self.request(
                    "POST", path, obj={"text": "x"},
                    origin=self.good_origin())[0], 403)
                # Non-JSON content type -> 415 (forms cannot post here).
                self.assertEqual(self.request(
                    "POST", path, obj="text=x", token=self.token,
                    origin=self.good_origin(), ctype=False,
                    extra={"Content-Type":
                           "application/x-www-form-urlencoded"})[0], 415)

    def test_allowed_requests_reach_the_route(self):
        # /s/new with empty text: the route's own validation answers
        # 400 and nothing is launched.
        self.assertEqual(self.request(
            "POST", "/s/new", obj={"text": ""},
            token=self.token, origin=self.good_origin())[0], 400)
        # reply/clarify on an unknown session: the route's own refusal
        # (404, or 400 where the body shape is validated first) — the
        # gate passed either way.
        for path in ("/s/default/20260902_120000_nosuch/reply",
                     "/s/default/20260902_120000_nosuch/clarify"):
            self.assertIn(self.request(
                "POST", path, obj={"text": "hi"},
                token=self.token, origin=self.good_origin())[0],
                (400, 404))
        # close/reopen really flip the fixture row.
        self.assertEqual(self.request(
            "POST", "/s/default/20260902_120000_deadbeef/close",
            obj={}, token=self.token, origin=self.good_origin())[0], 200)
        self.assertEqual(self.archived_flag(), 1)
        self.assertEqual(self.request(
            "POST", "/s/default/20260902_120000_deadbeef/reopen",
            obj={}, token=self.token, origin=self.good_origin())[0], 200)
        self.assertEqual(self.archived_flag(), 0)


class TestLoopbackAndExplicitBindsStillWork(HostBindingCase):
    """Refusing strangers must not refuse the legitimate spellings."""

    def test_all_loopback_host_spellings_are_served(self):
        for host in ("127.0.0.1:%d" % self.port,
                     "localhost:%d" % self.port,
                     "[::1]:%d" % self.port,
                     "127.0.0.1", "LOCALHOST."):
            with self.subTest(host=host):
                status, body = self.request("GET", "/new", host=host)
                self.assertEqual(status, 200, host)
                self.assertIn("mission-control-csrf", body)

    def test_explicit_non_loopback_bind_trusts_exactly_itself(self):
        # Simulated explicit LAN bind: the pinned trusted set is what
        # _default_trusted_hosts derives for that address, and the
        # server answers for exactly it.
        self.httpd.shutdown()  # re-pin the running server's set
        self.httpd.trusted_hosts = \
            self.mod._default_trusted_hosts("192.168.1.5")
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        status, _body = self.request("GET", "/new",
                                     host="192.168.1.5:%d" % self.port)
        self.assertEqual(status, 200)
        # Loopback no longer names this socket: binding a LAN address
        # is a deliberate act and every other name needs an explicit
        # --trusted-host entry.
        self.assertEqual(self.request(
            "GET", "/new", host="127.0.0.1:%d" % self.port)[0], 421)
        self.assertEqual(self.request(
            "GET", "/new", host="%s:%d" % (EVIL, self.port))[0], 421)

    def test_trusted_host_option_extends_the_set(self):
        self.httpd.shutdown()
        derived = self.mod._default_trusted_hosts("127.0.0.1")
        self.httpd.trusted_hosts = derived | {
            self.mod._normalize_host("mc.lan.example")}
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.assertEqual(self.request(
            "GET", "/new", host="mc.lan.example:%d" % self.port)[0], 200)
        self.assertEqual(self.request(
            "GET", "/new", host="127.0.0.1:%d" % self.port)[0], 200)
        self.assertEqual(self.request(
            "GET", "/new", host="%s:%d" % (EVIL, self.port))[0], 421)


class TestHostNormalizationAndDerivation(unittest.TestCase):
    """Unit level: normalization and the per-bind trusted sets."""

    def test_normalize_host_spellings(self):
        n = load_server_helper()
        cases = {
            "127.0.0.1:9136": "127.0.0.1",
            "[::1]:9136": "::1",
            "[::1]": "::1",
            "[2001:0db8:0000:0000:0000:0000:0000:0001]:80":
                str(ipaddress.ip_address("2001:db8::1")),
            "LOCALHOST.": "localhost",
            "EXAMPLE.com": "example.com",
        }
        for raw, want in cases.items():
            self.assertEqual(n._normalize_host(raw), want, raw)

    def test_normalize_host_rejects_garbage(self):
        n = load_server_helper()
        for raw in ("", "exa mple", "example/com", "a?b", "a#b",
                    "ex\\ample", "a%b", "a,b", "a\"b", "::1:80",
                    "2001:db8::1:80",
                    # Bare unbracketed IPv6 is refused whole — never
                    # truncated to its first segment.
                    "2001:0db8::1", "fe80::1%eth0", "example.com:notaport"):
            self.assertIsNone(n._normalize_host(raw), raw)

    def test_default_trusted_hosts_per_bind(self):
        n = load_server_helper()
        trio = set(n.LOOPBACK_HOSTS)
        self.assertEqual(n._default_trusted_hosts("localhost"), trio)
        self.assertGreaterEqual(n._default_trusted_hosts("127.0.0.1"),
                                trio | {"127.0.0.1"})
        self.assertGreaterEqual(n._default_trusted_hosts("::1"),
                                trio | {"::1"})
        for wildcard in ("", "*", "::", "0.0.0.0", None):
            self.assertGreaterEqual(n._default_trusted_hosts(wildcard),
                                    trio, wildcard)
        # An explicit non-loopback address trusts exactly itself.
        self.assertEqual(n._default_trusted_hosts("192.168.1.5"),
                         {"192.168.1.5"})

    def test_local_interface_ips_are_real_addresses(self):
        n = load_server_helper()
        for ip in n._local_interface_ips():
            # Must parse as an address, and must not smuggle in a
            # hostname spelling or a zone id.
            self.assertEqual(ip, str(ipaddress.ip_address(ip)), ip)


def load_server_helper():
    tmp = tempfile.mkdtemp(prefix="mc-hostunit-")
    try:
        db = os.path.join(tmp, "state.db")
        con = sqlite3.connect(db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        return load_server(tmp, db)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
