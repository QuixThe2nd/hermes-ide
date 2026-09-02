#!/usr/bin/env python3
"""The sidebar ordering contract: every open session before every
closed one, on every render.

The inbox and every transcript page render their conversation rows in
honest sections — Active, Open · completed, Open · unfinished, Closed
— and the load itself sorts every row globally newest-first by last
activity. The contract under test is the partition that wins over that
sort: a closed session NEVER precedes an open one, however much newer
its last activity is, while each section keeps its own newest-first
order. The partition happens before anything is emitted — there is no
pagination or windowing on this surface to hide behind (the 24 h load
window is a time filter, applied before the sections exist), so the
regression here exercises the exact disagreeing-timestamps trap: the
newest session overall is closed, the oldest open session must still
outrank it.

The last test closes the newest open session through the real route
(with the served CSRF token) and proves the no-reload refresh target —
the very same URLs — already renders it after every remaining open
row.

Run:  python3 tests/plugins/mission_control/test_sidebar_order.py
(unittest, stdlib only)
"""

import importlib.util
import itertools
import json
import os
import re
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

# The disagreeing-timestamps fixture: last_activity descends in id
# order, and the two closed sessions are interleaved by recency with
# the open ones — closed_newest is the newest session overall.
FIXTURE = [
    # (id, minutes_ago, archived, has_answer)
    ("closed_newest", 2, 1, True),
    ("open_new_completed", 30, 0, True),
    ("closed_older", 45, 1, True),
    ("open_unfinished", 90, 0, False),
    ("open_old_completed", 120, 0, True),
]


def load_server(tmp, db_path):
    """One isolated server.py module per test, pointed at the fixture
    home. Nothing here launches children or talks to Discord."""
    spec = importlib.util.spec_from_file_location(
        "mc_server_order_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.MAIN_DB = db_path
    mod.PROFILE_GLOB = os.path.join(tmp, "no-such-profile", "*", "state.db")
    return mod


class OrderingCase(unittest.TestCase):
    """The disagreeing-timestamps fixture behind a real HTTP server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-order-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        for sid, mins_ago, archived, has_answer in FIXTURE:
            last = now - mins_ago * 60
            con.execute(
                "INSERT INTO sessions (id, source, title, started_at,"
                " last_activity_at, archived, hidden)"
                " VALUES (?,?,?,?,?,?,0)",
                (sid, "cli", sid, last, last, archived))
            con.execute(
                "INSERT INTO messages (session_id, role, content,"
                " timestamp) VALUES (?,?,?,?)",
                (sid, "user", "question for " + sid, last - 60))
            if has_answer:
                con.execute(
                    "INSERT INTO messages (session_id, role, content,"
                    " timestamp) VALUES (?,?,?,?)",
                    (sid, "assistant", "answer for " + sid, last))
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

    # ---- HTTP helpers ----------------------------------------------

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

    def csrf_token(self):
        status, body = self.request("GET", "/new")
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="mission-control-csrf"'
                      r' content="([^"]*)"', body)
        self.assertIsNotNone(m)
        return m.group(1)

    def post(self, path, obj):
        status, body = self.request("POST", path, obj, self.csrf_token())
        return status, json.loads(body)

    # ---- ordering probes -------------------------------------------

    def section_positions(self, page):
        """data-section attribute -> position of its section opening
        tag, in document order (the rendered sidebar only)."""
        return {m.group(2): m.start() for m in re.finditer(
            r'<(section|details) class="convsec"[^>]*'
            r'data-section="([a-z]+)"', page)}

    def row_position(self, page, sid):
        """Position of one session's conversation row (matched on its
        data-q blob, which is led by the session id)."""
        m = re.search(r'<article class="conv[^>]*data-q="%s[ "]' % sid,
                      page)
        self.assertIsNotNone(m, "no sidebar row for %s" % sid)
        return m.start()

    OPEN_IDS = ("open_new_completed", "open_old_completed",
                "open_unfinished")
    CLOSED_IDS = ("closed_newest", "closed_older")

    def assert_open_before_closed(self, page):
        """The contract itself: every open row precedes every closed
        row, whatever their timestamps say."""
        for open_id in self.OPEN_IDS:
            for closed_id in self.CLOSED_IDS:
                self.assertLess(
                    self.row_position(page, open_id),
                    self.row_position(page, closed_id),
                    "%s (open) must render before %s (closed)"
                    % (open_id, closed_id))


class TestOpenBeforeClosed(OrderingCase):
    """The partition wins over the global newest-first sort."""

    def test_inbox_renders_all_open_sections_before_closed(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        pos = self.section_positions(page)
        # every open section opens before the closed disclosure
        self.assertLess(pos["completed"], pos["closed"])
        self.assertLess(pos["incomplete"], pos["closed"])
        # and the rows follow the same rule across section boundaries
        self.assert_open_before_closed(page)

    def test_newest_closed_session_still_outranked_by_older_open(self):
        """The disagreeing-timestamps regression: closed_newest is the
        most recently active session in the fixture and the global sort
        puts it first — the partition must still demote it behind every
        open row, including the oldest one."""
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        oldest_open = self.row_position(page, "open_old_completed")
        newest_closed = self.row_position(page, "closed_newest")
        self.assertLess(oldest_open, newest_closed)

    def test_intra_group_newest_first_order_is_kept(self):
        """Demotion is not flattening: inside each section the rows keep
        their last-activity order."""
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        # open completed: newer before older
        self.assertLess(self.row_position(page, "open_new_completed"),
                        self.row_position(page, "open_old_completed"))
        # closed: newer before older too
        self.assertLess(self.row_position(page, "closed_newest"),
                        self.row_position(page, "closed_older"))

    def test_chat_page_sidebar_follows_the_same_contract(self):
        """The sidebar is shared by every page: an open session's own
        transcript renders the identical partition."""
        status, page = self.request("GET", "/s/default/open_new_completed")
        self.assertEqual(status, 200)
        pos = self.section_positions(page)
        self.assertLess(pos["completed"], pos["closed"])
        self.assertLess(pos["incomplete"], pos["closed"])
        self.assert_open_before_closed(page)

    def test_closing_the_newest_open_session_demotes_it_live(self):
        """Behavioral end state: the newest OPEN session is closed
        through the real route; the refreshed pages (the exact URLs the
        no-reload client re-fetches) must move it behind every
        remaining open row — behind open_old_completed, whose timestamp
        is far older."""
        # before: newest open row leads the open rows
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertLess(
            self.row_position(page, "open_new_completed"),
            self.row_position(page, "open_old_completed"))

        status, payload = self.post(
            "/s/default/open_new_completed/close", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["archived"])

        # after: it renders after every remaining open row
        self.OPEN_IDS = ("open_old_completed", "open_unfinished")
        self.CLOSED_IDS = ("closed_newest", "closed_older",
                           "open_new_completed")
        for path in ("/", "/s/default/open_unfinished"):
            status, page = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assert_open_before_closed(page)
        # and it keeps newest-first inside the closed section
        status, page = self.request("GET", "/")
        self.assertLess(
            self.row_position(page, "open_new_completed"),
            self.row_position(page, "closed_older"))
        self.assertGreater(
            self.row_position(page, "open_new_completed"),
            self.row_position(page, "closed_newest"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
