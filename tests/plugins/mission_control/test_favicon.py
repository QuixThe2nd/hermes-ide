#!/usr/bin/env python3
"""The favicon declaration every Mission Control HTML shell carries.

A browser that loads an HTML page with no icon declaration
auto-requests /favicon.ico; this plugin serves none, so the request
404s and Chrome records it as an error-level Log.entryAdded in the
browser console. The fix is the empty inline data URI

    <link rel="icon" href="data:,">

declared in the <head> of every shell — inbox, transcript (and its
/new blank variant) and the error/404 chrome. The declaration must
hold the plugin's no-external-assets, domain-independent contract: no
shipped file, no request, and an href that names no scheme, host or
path, so the same served bytes work on any host and port.

These tests drive the real HTTP handler over a synthetic state.db and
prove each served page carries the declaration in its head and no
icon-related URL that a browser could turn into a network request.

Run:  python3 tests/plugins/mission_control/test_favicon.py
(unittest, stdlib only)
"""

import importlib.util
import itertools
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

# The exact declaration the shells ship: an empty data URI — no media
# type, no bytes, so the browser resolves the icon from the document
# itself and never opens a socket for one.
FAVICON_LINK = '<link rel="icon" href="data:,">'

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


class FaviconCase(unittest.TestCase):
    """A synthetic home behind a real HTTP server on an ephemeral port,
    plus helpers that read only what a served page actually declares."""

    SID = "20260903_fav_sess"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-favicon-")
        self.db = os.path.join(self.tmp, "state.db")
        con = sqlite3.connect(self.db)
        con.executescript(SESSION_SCHEMA)
        now = time.time()
        con.execute(
            "INSERT INTO sessions (id, source, title, started_at,"
            " last_activity_at, archived, hidden)"
            " VALUES (?,?,?,?,?,0,0)", (self.SID, "cli", "declaration probe",
                                        now, now))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)", (self.SID, "user", "the question", now))
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?,?,?,?)", (self.SID, "assistant", "the answer", now))
        con.commit()
        con.close()

        spec = importlib.util.spec_from_file_location(
            "mc_server_favicon_%d" % _MODULE_SEQ.__next__(), SERVER_PY)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.MAIN_DB = self.db
        self.mod.PROFILE_GLOB = os.path.join(self.tmp, "profiles", "*",
                                             "state.db")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         self.mod.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ---- HTTP helpers ----------------------------------------------

    def get(self, path):
        """(status, body) for one GET, HTTPError included."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    # ---- page readers ----------------------------------------------

    def head_of(self, page):
        """The served <head> — where a favicon link must live for the
        browser to honor it before painting anything."""
        self.assertIn("<head>", page)
        return page.split("</head>", 1)[0]

    def icon_links(self, page):
        """Every link element whose rel mentions an icon."""
        return [tag for tag in re.findall(r"<link\b[^>]*>", page)
                if re.search(r'\brel="[^"]*icon[^"]*"', tag)]


class TestInlineFaviconDeclared(FaviconCase):
    """Every shell a browser can land on carries the declaration."""

    SHELLS = (
        ("inbox", "/"),
        ("transcript", "/s/default/%s" % FaviconCase.SID),
        ("new-chat", "/new"),
        ("not-found", "/definitely/not/a/route"),
    )

    def test_every_shell_carries_the_inline_declaration_in_head(self):
        for label, path in self.SHELLS:
            status, page = self.get(path)
            self.assertIn(FAVICON_LINK, self.head_of(page),
                          "%s page (%s, %d) lacks the inline favicon"
                          % (label, path, status))

    def test_error_shell_carries_the_inline_declaration_in_head(self):
        """The 500 chrome too: a profile DB that cannot be read serves
        the themed error page, and a browser loading it must not be
        sent hunting for /favicon.ico either."""
        broken_home = os.path.join(self.tmp, "profiles", "broken")
        os.makedirs(broken_home, exist_ok=True)
        with open(os.path.join(broken_home, "state.db"), "wb"):
            pass  # discoverable, but no tables -> sqlite3.Error -> 500
        status, page = self.get("/s/broken/%s" % self.SID)
        self.assertEqual(status, 500)
        self.assertIn(FAVICON_LINK, self.head_of(page))

    def test_the_declaration_is_solely_the_empty_data_uri(self):
        """Exactly one icon link per page, and its href is the empty
        data URI — no scheme, host or path a request could target."""
        for label, path in self.SHELLS:
            _status, page = self.get(path)
            links = self.icon_links(self.head_of(page))
            self.assertEqual(
                links, [FAVICON_LINK],
                "%s page (%s) declares unexpected icon links: %r"
                % (label, path, links))


class TestNoExternalFaviconURL(FaviconCase):
    """The declaration names nothing a browser could request."""

    def test_icon_hrefs_are_request_free_data_uris(self):
        """Every icon href is an empty data URI: it names no network
        location (no scheme-and-authority, no protocol-relative origin,
        no absolute path), it is a data URI at all, and it carries no
        payload — so no request happens, no asset ships and the page
        stays valid on whatever host and port served it."""
        for label, path in TestInlineFaviconDeclared.SHELLS:
            _status, page = self.get(path)
            for tag in self.icon_links(page):
                href = re.search(r'href="([^"]*)"', tag)
                self.assertIsNotNone(href, "icon link without href: %r" % tag)
                value = href.group(1)
                self.assertFalse(
                    re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//|^/",
                             value),
                    "%s page (%s) icon href names a network location: %r"
                    % (label, path, value))
                self.assertTrue(
                    value.startswith("data:"),
                    "%s page (%s) icon href is not an inline data URI: %r"
                    % (label, path, value))
                self.assertEqual(
                    len(value), len("data:,"),
                    "%s page (%s) icon href ships payload bytes: %r"
                    % (label, path, value))

    def test_no_page_references_the_favicon_ico_url(self):
        """No served page points at favicon.ico — the URL Chrome would
        auto-request (and 404 on) when a page lacks the declaration."""
        for label, path in TestInlineFaviconDeclared.SHELLS:
            _status, page = self.get(path)
            self.assertNotIn(
                "favicon.ico", page.lower(),
                "%s page (%s) references the favicon.ico URL"
                % (label, path))


if __name__ == "__main__":
    unittest.main()
