#!/usr/bin/env python3
"""Focused tests for the archive (Close/Reopen) sidebar refresh.

Closing or reopening a session must move its conversation row into or
out of the Closed disclosure immediately, without a page reload. The
behavior, covered end to end at the HTTP layer over a synthetic
state.db:

- the toggle POST commits the local flip before it answers ok (a local
  or threadless session never touches Discord in these tests), so the
  very same chat URL — the one the client's no-reload refreshRows swap
  re-fetches — already server-renders the row under its new section,
  with the selected Closed row kept visible by the open disclosure;
- the feed's session_state carries the fresh archived flag, so an open
  page learns a flip that happened elsewhere (another tab, Discord);
- a refused toggle never reports ok, so the client applies no state
  and the real session's row stays exactly where it was;
- the shipped client script re-renders the sidebar exactly on an
  archive-state transition (never per poll), matching the busy-state
  transition rule.

Every POST here is a real same-origin JSON POST carrying the served
CSRF token, exactly like the shipped client's postJson helper — the
toggle routes sit behind the forgery gate. The client-side wiring
itself is deliberately untested beyond its source contract: these
suites assert real HTTP behavior, not the shape of the shipped
JavaScript, and no JS runtime is on hand to exercise the shipped
client for real.

Run:  python3 tests/plugins/mission_control/test_archive_sidebar.py
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


def load_server(tmp, db_path):
    """One isolated server.py module instance per test: its MAIN_DB and
    profile glob point at the test fixture. No hermes stub is installed:
    nothing here ever launches a child (the toggle routes are pure DB
    reads and one committed write)."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_archsb_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class ServerCase(unittest.TestCase):
    """A real ThreadingHTTPServer on an ephemeral port over a synthetic
    state.db."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archsb-test-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        con.commit()
        con.close()
        self.mod = load_server(self.tmp, self.db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture helpers -------------------------------------------

    def add_session(self, sid, source="cli", title="fixture",
                    archived=0, last=None):
        con = sqlite3.connect(self.db)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title,"
            " started_at, last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,?,0)",
            (sid, source, title, now, last if last is not None else now,
             archived))
        con.commit()
        con.close()

    def add_message(self, sid, role, content, ts=None):
        con = sqlite3.connect(self.db, timeout=10)
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)",
            (sid, role, content, ts if ts is not None else time.time()))
        con.commit()
        con.close()

    # ---- HTTP helpers ----------------------------------------------

    def csrf_token(self):
        """The token this process served — mined from any page, exactly
        the way the shipped client reads its meta tag."""
        status, body = self.request("GET", "/new")
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="mission-control-csrf"'
                      r' content="([^"]*)"', body)
        self.assertIsNotNone(m, "served page carries the CSRF meta tag")
        return m.group(1)

    def request(self, method, path, obj=None, token=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        headers = {}
        if obj is not None:
            data = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-CSRF-Token"] = token
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def request_json(self, method, path, obj=None, token=None):
        status, body = self.request(method, path, obj, token)
        return status, json.loads(body)

    def post(self, path, obj):
        """A same-origin state-changing POST: JSON body plus the served
        CSRF token in the non-simple header, like the real client."""
        return self.request_json("POST", path, obj, self.csrf_token())

    def selected_section(self, page):
        """data-section of the sidebar section holding the selected
        conversation row (fails the test when nothing is selected)."""
        for m in re.finditer(
                r'<(section|details) class="convsec"[^>]*'
                r'data-section="([a-z]+)"', page):
            close = page.find("</%s>" % m.group(1), m.end())
            block = page[m.end():close]
            if 'class="conv is-selected' in block:
                return m.group(2)
        self.fail("no selected conversation row found in the sidebar")


class TestArchiveSidebarRefresh(ServerCase):
    """Closing or reopening moves the selected conversation row between
    its resting section and Closed — visible on the very same chat URL
    the no-reload client refresh fetches, with the feed telling any open
    page about the flip."""

    def test_close_then_reopen_moves_selected_row_without_reload(self):
        sid = "20260903_arch_move"
        self.add_session(sid, title="moving row")
        self.add_message(sid, "user", "the question")
        self.add_message(sid, "assistant", "the answer")

        # resting state: a settled assistant answer classifies the row
        # Open · completed, selected on its own chat page
        status, page = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(page), "completed")

        # close: the POST commits the flip before answering ok
        status, payload = self.post("/s/default/%s/close" % sid, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["archived"])
        self.assertFalse(payload["discord"])
        self.assertIsNone(payload["thread_id"])

        # the SAME chat URL — what refreshRows re-fetches without any
        # reload — already renders the row under Closed, still selected,
        # and the disclosure ships open (data-keep) so the selected
        # channel is never hidden; the page chrome matches the flip too
        status, closed_page = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(closed_page), "closed")
        self.assertIn('data-section="closed" open data-keep="1"',
                      closed_page)
        self.assertIn('data-archived="1"', closed_page)
        self.assertIn(">Reopen</button>", closed_page)

        # reopen: the row returns to its resting section and the Closed
        # disclosure disappears entirely (it renders only with members;
        # the bare data-section string also lives in the inline script,
        # so the rendered <details> element is the honest target)
        status, payload = self.post("/s/default/%s/reopen" % sid, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["archived"])
        status, open_page = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(open_page), "completed")
        self.assertNotIn('<details class="convsec" id="sec-closed"',
                         open_page)
        self.assertIn('data-archived="0"', open_page)
        self.assertIn(">Close</button>", open_page)

    def test_feed_carries_the_archive_state_for_open_pages(self):
        """The poll an open page runs is the channel through which a
        flip made elsewhere lands without a reload: its session_state
        must always report the freshly committed archived flag."""
        sid = "20260903_arch_feed"
        self.add_session(sid, title="feed state")
        self.add_message(sid, "user", "hello")
        self.add_message(sid, "assistant", "hi")

        _s, feed = self.request_json("GET", "/s/default/%s/feed?after=0"
                                     % sid)
        self.assertIn("session_state", feed)
        self.assertFalse(feed["session_state"]["archived"])

        _s, _p = self.post("/s/default/%s/close" % sid, {})
        _s, feed = self.request_json("GET", "/s/default/%s/feed?after=0"
                                     % sid)
        self.assertTrue(feed["session_state"]["archived"])

        _s, _p = self.post("/s/default/%s/reopen" % sid, {})
        _s, feed = self.request_json("GET", "/s/default/%s/feed?after=0"
                                     % sid)
        self.assertFalse(feed["session_state"]["archived"])

    def test_refused_toggle_changes_nothing(self):
        """A refused toggle (unknown session) never reports ok, so the
        client applies no state and refreshes nothing — and the real
        session's row stays exactly where it was."""
        sid = "20260903_arch_keep"
        self.add_session(sid, title="untouched")
        self.add_message(sid, "user", "the question")
        self.add_message(sid, "assistant", "the answer")

        status, payload = self.post(
            "/s/default/no-such-session/close", {})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

        status, payload = self.post("/s/nope/%s/close" % sid, {})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

        status, page = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertEqual(self.selected_section(page), "completed")
        self.assertNotIn('<details class="convsec" id="sec-closed"', page)

    def test_toggle_without_csrf_token_is_refused(self):
        """The archive routes sit behind the forgery gate: a JSON POST
        carrying no served token is refused before anything flips."""
        sid = "20260903_arch_csrf"
        self.add_session(sid, title="gated")
        self.add_message(sid, "user", "the question")
        self.add_message(sid, "assistant", "the answer")

        status, payload = self.request_json(
            "POST", "/s/default/%s/close" % sid, {})
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        status, page = self.request("GET", "/s/default/" + sid)
        self.assertEqual(self.selected_section(page), "completed")

    def test_client_source_refreshes_sidebar_on_archive_transition(self):
        """The shipped script re-renders the sidebar exactly on an
        archive-state transition (wasArchived vs archived), never per
        poll — the no-reload half of the behavior above."""
        sid = "20260903_arch_src"
        self.add_session(sid, title="source contract")
        self.add_message(sid, "user", "q")
        self.add_message(sid, "assistant", "a")
        status, body = self.request("GET", "/s/default/" + sid)
        self.assertEqual(status, 200)
        self.assertIn("if (wasArchived !== archived) refreshSidebar();",
                      body)
        self.assertIn("var wasArchived = archived;", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
